from __future__ import annotations

from loguru import logger


def _as_str_list(x) -> list[str]:
    if x is None:
        return []
    if isinstance(x, str):
        s = x.strip()
        return [s] if s else []
    if isinstance(x, list):
        out = []
        for it in x:
            if it is None:
                continue
            if isinstance(it, str):
                s = it.strip()
                if s:
                    out.append(s)
            elif isinstance(it, dict):
                # common keys
                for k in ("text", "point", "content", "spoken", "title"):
                    if k in it and it[k]:
                        s = str(it[k]).strip()
                        if s:
                            out.append(s)
                            break
            else:
                s = str(it).strip()
                if s:
                    out.append(s)
        return out
    return [str(x).strip()] if str(x).strip() else []


def _shorten(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def build_script_from_story(story: dict, hashtags_default: list[str], safe_disclaimer: bool = True) -> dict:
    """Build a template-ready script_pack dict for Jinja templates.

    Templates under templates/douyin/*.j2 expect at least:
      - script.video_title_candidates[0]
      - script.hook.spoken / script.hook.on_screen
      - script.segments: list of {duration_sec, spoken, on_screen, visual}
      - script.closing.spoken / script.closing.on_screen
      - script.disclaimer
      - script.hashtags
    """

    topic = story.get("topic") or "今日要闻"
    thesis = (story.get("thesis") or "").strip()
    tone = story.get("tone") or "冷静客观"
    style = story.get("recommended_style") or "fast_news"

    key_points = story.get("key_points") or []
    key_points = _as_str_list(key_points)
    supporting_facts = story.get("supporting_facts") or []
    open_questions = story.get("open_questions") or []
    open_questions = _as_str_list(open_questions)

    # Titles: allow upstream overrides
    titles = story.get("titles") or story.get("video_title_candidates") or [
        f"{topic}：发生了什么？",
        f"一分钟看懂：{topic}",
        f"{topic} 最新进展",
    ]
    titles = _as_str_list(titles)
    primary_title = titles[0] if titles else topic

    # Hook (3s)
    hook_spoken = ""
    if thesis:
        hook_spoken = f"{topic}有新进展：{_shorten(thesis, 44)}"
    elif key_points:
        hook_spoken = f"{topic}，先看一句话：{_shorten(key_points[0], 44)}"
    else:
        hook_spoken = f"{topic}发生了什么？30秒带你看懂。"

    hook = {
        "spoken": hook_spoken,
        "on_screen": _shorten(primary_title, 22) or _shorten(topic, 22),
    }

    # Segments
    if style == "explain_60s":
        default_dur = 12
        max_seg = 6
    elif style == "three_stories":
        default_dur = 10
        max_seg = 3
    else:  # fast_news / fallback
        default_dur = 8
        max_seg = 5

    seg_src = key_points[:max_seg] if key_points else (_as_str_list(supporting_facts)[:max_seg] if supporting_facts else [])
    segments = []
    for i, kp in enumerate(seg_src):
        spoken = kp
        on_screen = _shorten(kp, 26)
        visual = "相关新闻画面/资料图（配关键字幕）"
        segments.append(
            {
                "duration_sec": default_dur,
                "spoken": spoken,
                "on_screen": on_screen,
                "visual": visual,
            }
        )

    # If still empty, provide one segment so templates never crash
    if not segments:
        segments = [
            {
                "duration_sec": default_dur,
                "spoken": f"目前公开信息有限，我们将持续关注 {topic} 的后续进展。",
                "on_screen": _shorten(topic, 26),
                "visual": "新闻标题卡 + 资料画面",
            }
        ]

    closing_spoken = f"以上就是 {topic} 的要点梳理。想看后续更新，记得关注。"
    if open_questions:
        closing_spoken = f"以上就是 {topic}。接下来关注：{_shorten(open_questions[0], 40)} 记得关注获取更新。"

    closing = {
        "spoken": closing_spoken,
        "on_screen": "关注获取更新",
    }

    pack = {
        "event_id": story.get("event_id") or "event_unknown",
        "topic": topic,
        "thesis": thesis,
        "tone": tone,
        "recommended_style": style,

        # Titles + compatibility fields
        "titles": titles,
        "video_title_candidates": titles,
        "video_title": primary_title,
        "video_title_primary": primary_title,

        # Template-required narrative structure
        "hook": hook,
        "segments": segments,
        "closing": closing,

        # Other meta
        "key_points": key_points,
        "supporting_facts": supporting_facts,
        "open_questions": open_questions,
        "hashtags": (story.get("hashtags") or hashtags_default),
        "assets_plan": story.get("assets_plan") or {},
    }

    if safe_disclaimer:
        pack["disclaimer"] = "本内容为新闻信息梳理，不构成任何建议；请以权威来源为准。"

    # Schema guard: ensure all referenced fields exist with correct shape
    pack.setdefault("video_title_candidates", pack.get("titles", []) or [])
    if not isinstance(pack["video_title_candidates"], list):
        pack["video_title_candidates"] = _as_str_list(pack["video_title_candidates"])
    pack.setdefault("video_title", (pack["video_title_candidates"][0] if pack["video_title_candidates"] else pack.get("topic", "")))
    pack.setdefault("hook", {"spoken": "", "on_screen": ""})
    pack.setdefault("segments", [])
    pack.setdefault("closing", {"spoken": "", "on_screen": ""})
    pack.setdefault("hashtags", [])
    pack.setdefault("disclaimer", "")

    logger.debug(f"[EXPORT] built script_pack event_id={pack['event_id']} style={pack['recommended_style']}")
    return pack
