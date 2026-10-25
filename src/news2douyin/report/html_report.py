from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

try:
    import mistune  # type: ignore
except Exception:  # pragma: no cover
    mistune = None


# -----------------------------
# IO helpers
# -----------------------------
def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not path.exists():
        return items
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                logger.warning(f"[REPORT] Skipping bad jsonl line in {path}")
    return items


def _slug(s: str) -> str:
    keep = []
    for ch in str(s):
        if ch.isalnum() or ch in ("_", "-", "."):
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    out = "".join(keep).strip("_")
    return out[:120] or "item"


def _md_to_html(md: str) -> str:
    if mistune is None:
        esc = md.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<pre class='md-pre'>{esc}</pre>"
    renderer = mistune.HTMLRenderer(escape=True)
    markdown = mistune.create_markdown(renderer=renderer)
    return markdown(md)


def _domain_from_url(url: str) -> str:
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        u = urlparse(url)
        return u.netloc
    except Exception:
        return ""


def _pick_first(xs: Any) -> Optional[str]:
    if isinstance(xs, list) and xs:
        return xs[0]
    return None


# -----------------------------
# View models
# -----------------------------
@dataclass
class SummaryCounts:
    raw: int
    dedup: int
    scored: int
    selected: int
    events: int
    scriptpacks: int


@dataclass
class EventView:
    event_id: str
    topic: str
    n_items: int
    avg_value_score: float
    title: str
    tone: str
    style: str
    page_rel: str
    has_script: bool


# -----------------------------
# Main entry
# -----------------------------
def build_html_report(run_dir: str | Path, out_dir: str | Path | None = None) -> Path:
    """
    Build a static HTML report for a run directory.

    It tries to show the full pipeline lineage to reduce missed events:

      raw -> dedup -> scored -> selected -> storypacks -> scriptpacks

    Inputs expected (best-effort):
      - meta.json
      - raw.jsonl
      - dedup.jsonl
      - scored.jsonl
      - selected.json
      - storypacks.jsonl
      - scriptpacks/event_*/script.json + script.md

    Output:
      - <run_dir>/report/index.html
      - <run_dir>/report/raw.html
      - <run_dir>/report/scored.html
      - <run_dir>/report/selected.html
      - <run_dir>/report/events.html
      - <run_dir>/report/event_<event_id>.html
      - <run_dir>/report/gaps.html
    """
    run_path = Path(run_dir)
    if out_dir is None:
        out_path = run_path / "report"
    else:
        out_path = Path(out_dir)
        if not out_path.is_absolute():
            out_path = run_path / out_path
    out_path.mkdir(parents=True, exist_ok=True)

    meta = _read_json(run_path / "meta.json") if (run_path / "meta.json").exists() else {}

    raw_items = _read_jsonl(run_path / "raw.jsonl")
    dedup_items = _read_jsonl(run_path / "dedup.jsonl")
    scored_items = _read_jsonl(run_path / "scored.jsonl")
    selected_obj = _read_json(run_path / "selected.json") if (run_path / "selected.json").exists() else {}
    storypacks = _read_jsonl(run_path / "storypacks.jsonl")

    # Indexes
    raw_by_id: Dict[str, Dict[str, Any]] = {str(x.get("id")): x for x in raw_items if x.get("id")}
    dedup_by_id: Dict[str, Dict[str, Any]] = {str(x.get("id")): x for x in dedup_items if x.get("id")}

    scored_by_raw: Dict[str, Dict[str, Any]] = {}
    for s in scored_items:
        rid = str(s.get("raw_id") or s.get("id") or "")
        if rid:
            scored_by_raw[rid] = s

    selected_items: List[Dict[str, Any]] = []
    # selected.json schema varies; support {"items":[...]} or list
    if isinstance(selected_obj, list):
        selected_items = selected_obj
    elif isinstance(selected_obj, dict):
        if isinstance(selected_obj.get("items"), list):
            selected_items = selected_obj["items"]
        elif isinstance(selected_obj.get("selected"), list):
            selected_items = selected_obj["selected"]

    selected_raw_ids = set(str(x.get("raw_id") or x.get("id") or "") for x in selected_items if (x.get("raw_id") or x.get("id")))

    # Event grouping (from scored + storypacks)
    scored_by_event: Dict[str, List[Dict[str, Any]]] = {}
    for s in scored_items:
        eid = str(s.get("event_id") or "event_unknown")
        scored_by_event.setdefault(eid, []).append(s)

    story_by_event: Dict[str, Dict[str, Any]] = {}
    for sp in storypacks:
        eid = str(sp.get("event_id") or sp.get("id") or "event_unknown")
        story_by_event[eid] = sp

    # Scriptpacks
    scriptpacks_dir = run_path / "scriptpacks"
    script_by_event: Dict[str, Dict[str, Any]] = {}
    script_md_by_event: Dict[str, str] = {}
    if scriptpacks_dir.exists():
        for d in scriptpacks_dir.glob("event_*"):
            if not d.is_dir():
                continue
            sj = d / "script.json"
            smd = d / "script.md"
            if sj.exists():
                try:
                    pack = _read_json(sj)
                    eid = str(pack.get("event_id") or d.name.replace("event_", ""))
                    script_by_event[eid] = pack
                    if smd.exists():
                        script_md_by_event[eid] = smd.read_text(encoding="utf-8")
                except Exception:
                    logger.warning(f"[REPORT] Failed reading script pack: {d}")

    # Summary
    counts = SummaryCounts(
        raw=len(raw_items),
        dedup=len(dedup_items),
        scored=len(scored_items),
        selected=len(selected_items),
        events=len({*scored_by_event.keys(), *story_by_event.keys()} - {"event_unknown"}),
        scriptpacks=len(script_by_event),
    )

    # Events list view
    all_event_ids = sorted({*scored_by_event.keys(), *story_by_event.keys()} - set(), key=lambda x: x)
    event_views: List[EventView] = []
    for eid in all_event_ids:
        items = scored_by_event.get(eid, [])
        avg = 0.0
        if items:
            avg = sum(float(x.get("value_score") or 0.0) for x in items) / max(1, len(items))
        story = story_by_event.get(eid, {})
        topic = str(story.get("topic") or story.get("title") or eid)
        tone = str(story.get("tone") or (script_by_event.get(eid, {}) or {}).get("tone") or "")
        style = str(story.get("recommended_style") or (script_by_event.get(eid, {}) or {}).get("recommended_style") or "")
        title_candidates = (script_by_event.get(eid, {}) or {}).get("video_title_candidates") or story.get("video_title_candidates") or story.get("titles") or []
        title = ""
        if isinstance(title_candidates, list) and title_candidates:
            title = str(title_candidates[0])
        else:
            title = str((script_by_event.get(eid, {}) or {}).get("video_title") or topic)
        page_rel = f"event_{_slug(eid)}.html"
        event_views.append(
            EventView(
                event_id=eid,
                topic=topic,
                n_items=len(items),
                avg_value_score=avg,
                title=title,
                tone=tone,
                style=style,
                page_rel=page_rel,
                has_script=(eid in script_by_event),
            )
        )

    # Build pages
    nav = _nav_links()

    # index: dashboard
    (out_path / "index.html").write_text(_render_index(meta, counts, nav), encoding="utf-8")

    # stage pages
    (out_path / "raw.html").write_text(
        _render_items_table(
            meta=meta,
            title="RAW 新闻（抓取结果）",
            subtitle="来自 INGEST 阶段的原始条目。用于检查是否遗漏大事件、来源质量、图片可用性。",
            nav=nav,
            rows=_rows_raw(raw_items),
        ),
        encoding="utf-8",
    )
    (out_path / "dedup.html").write_text(
        _render_items_table(
            meta=meta,
            title="DEDUP 新闻（去重后）",
            subtitle="去重后的条目。用于检查去重是否过度（误杀重要新闻）。",
            nav=nav,
            rows=_rows_raw(dedup_items),
        ),
        encoding="utf-8",
    )

    scored_rows = _rows_scored(scored_items, raw_by_id, selected_raw_ids)
    (out_path / "scored.html").write_text(
        _render_items_table(
            meta=meta,
            title="SCORED（打分结果）",
            subtitle="每条新闻的 value_score / risk_flags / event_id。可按分数降序检查“高分但没选中”的潜在遗漏。",
            nav=nav,
            rows=scored_rows,
        ),
        encoding="utf-8",
    )

    selected_rows = _rows_selected(selected_items, raw_by_id, scored_by_raw)
    (out_path / "selected.html").write_text(
        _render_items_table(
            meta=meta,
            title="SELECTED（最终入选条目）",
            subtitle="最终进入事件聚类/故事线的条目。用于审查选题覆盖。",
            nav=nav,
            rows=selected_rows,
        ),
        encoding="utf-8",
    )

    # events list
    (out_path / "events.html").write_text(_render_events(meta, event_views, nav), encoding="utf-8")

    # gaps / misses
    (out_path / "gaps.html").write_text(
        _render_gaps(
            meta=meta,
            counts=counts,
            nav=nav,
            scored_rows=scored_rows,
            selected_raw_ids=selected_raw_ids,
            event_views=event_views,
            story_by_event=story_by_event,
            has_script=set(script_by_event.keys()),
        ),
        encoding="utf-8",
    )

    # event pages
    for ev in event_views:
        items = sorted(scored_by_event.get(ev.event_id, []), key=lambda x: float(x.get("value_score") or 0.0), reverse=True)

        # news articles for this event
        articles: List[Dict[str, Any]] = []
        for s in items:
            rid = str(s.get("raw_id") or "")
            raw = raw_by_id.get(rid) or dedup_by_id.get(rid)
            if not raw:
                continue
            src = raw.get("source") or {}
            articles.append(
                {
                    "raw_id": rid,
                    "title": raw.get("title") or "",
                    "url": raw.get("url") or "",
                    "domain": src.get("domain") or _domain_from_url(raw.get("url") or ""),
                    "published_at": raw.get("published_at") or "",
                    "image_url": _pick_first(raw.get("image_urls")),
                    "value_score": float(s.get("value_score") or 0.0),
                    "why": s.get("why_recommended") or "",
                    "risk_flags": s.get("risk_flags") or [],
                    "angles": s.get("angles") or [],
                    "selected": rid in selected_raw_ids,
                }
            )

        story = story_by_event.get(ev.event_id)
        story_json = json.dumps(story or {}, ensure_ascii=False, indent=2)

        script_md = script_md_by_event.get(ev.event_id, "")
        script_html = _md_to_html(script_md) if script_md else "<p class='muted'>No script.md for this event (yet).</p>"
        script_json = json.dumps(script_by_event.get(ev.event_id, {}) or {}, ensure_ascii=False, indent=2)

        (out_path / ev.page_rel).write_text(
            _render_event_detail(
                meta=meta,
                ev=ev,
                nav=nav,
                articles=articles,
                story_json=story_json,
                script_html=script_html,
                script_json=script_json,
            ),
            encoding="utf-8",
        )

    logger.success(f"[REPORT] HTML report created: {out_path / 'index.html'}")
    return out_path


# -----------------------------
# Rendering
# -----------------------------
def _nav_links() -> List[Tuple[str, str]]:
    return [
        ("Dashboard", "index.html"),
        ("RAW", "raw.html"),
        ("DEDUP", "dedup.html"),
        ("SCORED", "scored.html"),
        ("SELECTED", "selected.html"),
        ("EVENTS", "events.html"),
        ("GAPS", "gaps.html"),
    ]


def _common_css() -> str:
    return r"""
:root{
  --bg:#0b0f14; --card:#111827; --muted:#9ca3af; --text:#e5e7eb; --accent:#60a5fa; --border:#1f2937; --good:#34d399; --bad:#fb7185; --warn:#fbbf24;
}
*{box-sizing:border-box}
body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,"Apple Color Emoji","Segoe UI Emoji";
  background:var(--bg); color:var(--text); line-height:1.5;}
a{color:var(--accent); text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:1200px;margin:0 auto;padding:22px}
.header{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:14px}
.h1{font-size:26px;font-weight:900;margin:0}
.sub{color:var(--muted);font-size:13px}
.nav{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0 18px}
.nav a{padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,0.02)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:14px}
.kpi{display:flex;gap:12px;flex-wrap:wrap}
.kpi .box{padding:10px 12px;border:1px solid var(--border);border-radius:14px;background:rgba(255,255,255,0.02)}
.kpi .num{font-size:18px;font-weight:900}
.kpi .lbl{color:var(--muted);font-size:12px}
.table-wrap{overflow:auto;border-radius:14px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;min-width:960px}
th,td{padding:10px 10px;border-bottom:1px solid var(--border);vertical-align:top;font-size:13px}
th{position:sticky;top:0;background:rgba(17,24,39,0.95);text-align:left;z-index:1}
.badge{display:inline-block;padding:2px 8px;border:1px solid var(--border);border-radius:999px;font-size:12px;color:var(--muted)}
.badge.good{color:var(--good);border-color:rgba(52,211,153,0.3)}
.badge.bad{color:var(--bad);border-color:rgba(251,113,133,0.3)}
.badge.warn{color:var(--warn);border-color:rgba(251,191,36,0.3)}
.small{color:var(--muted);font-size:12px}
.muted{color:var(--muted)}
.search{display:flex;gap:8px;align-items:center;margin:10px 0 14px}
.search input{flex:1;min-width:240px;padding:10px 12px;border-radius:12px;border:1px solid var(--border);background:rgba(255,255,255,0.03);color:var(--text)}
.search select{padding:10px 12px;border-radius:12px;border:1px solid var(--border);background:rgba(255,255,255,0.03);color:var(--text)}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media (max-width:980px){.two-col{grid-template-columns:1fr}}
pre{white-space:pre-wrap;word-break:break-word;background:rgba(255,255,255,0.03);padding:12px;border-radius:14px;border:1px solid var(--border);margin:0}
.md-pre{white-space:pre-wrap}
img.thumb{width:96px;height:64px;object-fit:cover;border-radius:10px;border:1px solid var(--border)}
"""

def _common_js() -> str:
    return r"""
function q(sel){ return document.querySelector(sel); }
function qa(sel){ return Array.from(document.querySelectorAll(sel)); }
function setCount(n){ const el=q('#rowCount'); if(el) el.textContent = n.toString(); }

function applyFilter(){
  const term = (q('#search')?.value || '').toLowerCase().trim();
  const col = q('#col')?.value || 'all';
  const flag = q('#flag')?.value || 'all';

  let shown = 0;
  qa('tbody tr').forEach(tr=>{
    const text = (tr.getAttribute('data-text')||'').toLowerCase();
    const flags = (tr.getAttribute('data-flags')||'').toLowerCase();
    let ok = true;
    if(term){
      if(col==='all'){ ok = text.includes(term); }
      else{
        const cell = tr.querySelector(`[data-col="${col}"]`);
        ok = (cell?.textContent||'').toLowerCase().includes(term);
      }
    }
    if(ok && flag!=='all'){
      ok = flags.includes(flag.toLowerCase());
    }
    tr.style.display = ok ? '' : 'none';
    if(ok) shown++;
  });
  setCount(shown);
}
window.addEventListener('DOMContentLoaded', ()=>{
  const s=q('#search'); if(s) s.addEventListener('input', applyFilter);
  const c=q('#col'); if(c) c.addEventListener('change', applyFilter);
  const f=q('#flag'); if(f) f.addEventListener('change', applyFilter);
  applyFilter();
});
"""


def _layout(meta: Dict[str, Any], nav: List[Tuple[str, str]], title: str, subtitle: str, body: str) -> str:
    run_id = (meta.get("run_id") or "")
    date = (meta.get("date") or "")
    hdr = f"{title}"
    sub = f"run_id={run_id}  date={date}"
    nav_html = " ".join([f"<a href='{href}'>{name}</a>" for name, href in nav])

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{_esc(hdr)}</title>
<style>{_common_css()}</style>
<script>{_common_js()}</script>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <div class="h1">{_esc(hdr)}</div>
      <div class="sub">{_esc(sub)} · <span class="muted">{_esc(subtitle)}</span></div>
    </div>
  </div>
  <div class="nav">{nav_html}</div>
  {body}
</div>
</body>
</html>
"""


def _esc(s: Any) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )




def _join_items(xs: Any) -> str:
    """Join a list-like field into a readable string (robust to dict items)."""
    if xs is None:
        return ""
    if not isinstance(xs, list):
        xs = [xs]
    out: List[str] = []
    for x in xs:
        if x is None:
            continue
        if isinstance(x, str):
            s = x.strip()
            if s:
                out.append(s)
            continue
        if isinstance(x, (int, float, bool)):
            out.append(str(x))
            continue
        if isinstance(x, dict):
            # common patterns: {"name": "..."} or {"label": "..."}
            picked = None
            for k in ("name", "label", "tag", "id", "value", "text"):
                v = x.get(k)
                if isinstance(v, (str, int, float, bool)) and str(v).strip():
                    picked = str(v).strip()
                    break
            out.append(picked if picked is not None else json.dumps(x, ensure_ascii=False))
            continue
        out.append(str(x))
    return ", ".join(out)


def _render_index(meta: Dict[str, Any], counts: SummaryCounts, nav: List[Tuple[str, str]]) -> str:
    body = f"""
<div class="card">
  <div class="kpi">
    <div class="box"><div class="num">{counts.raw}</div><div class="lbl">RAW</div></div>
    <div class="box"><div class="num">{counts.dedup}</div><div class="lbl">DEDUP</div></div>
    <div class="box"><div class="num">{counts.scored}</div><div class="lbl">SCORED</div></div>
    <div class="box"><div class="num">{counts.selected}</div><div class="lbl">SELECTED</div></div>
    <div class="box"><div class="num">{counts.events}</div><div class="lbl">EVENTS</div></div>
    <div class="box"><div class="num">{counts.scriptpacks}</div><div class="lbl">SCRIPT PACKS</div></div>
  </div>
</div>

<div class="grid" style="margin-top:14px">
  <div class="card" style="grid-column:span 12">
    <div style="font-weight:800;margin-bottom:6px">如何用这份报告避免遗漏事件</div>
    <ul class="small" style="margin:8px 0 0 18px">
      <li>先看 <b>SCORED</b>：按 value_score 降序，检查“高分但未入选”的新闻是否代表重要事件。</li>
      <li>再看 <b>EVENTS</b>：按事件聚合查看每个 event 的来源数量与平均分，确认主要事件都有脚本。</li>
      <li>最后看 <b>GAPS</b>：系统自动列出潜在遗漏（高分未选、无脚本事件等）。</li>
    </ul>
  </div>
</div>
"""
    return _layout(meta, nav, "Report Dashboard", "一眼看清整条流水线覆盖情况", body)


def _render_items_table(meta: Dict[str, Any], title: str, subtitle: str, nav: List[Tuple[str, str]], rows: List[Dict[str, Any]]) -> str:
    # gather flag options
    flag_set = set()
    for r in rows:
        for f in r.get("_flags_list", []):
            flag_set.add(str(f))
    flag_opts = "".join([f"<option value='{_esc(x)}'>{_esc(x)}</option>" for x in sorted(flag_set)])

    cols = rows[0]["_cols"] if rows else []
    col_opts = "<option value='all'>全部字段</option>" + "".join([f"<option value='{_esc(c)}'>{_esc(c)}</option>" for c in cols])

    thead = "".join([f"<th>{_esc(c)}</th>" for c in cols])
    body_rows = []
    for r in rows:
        tds = []
        for c in cols:
            tds.append(f"<td data-col='{_esc(c)}'>{r.get(c,'')}</td>")
        data_text = " | ".join([_strip_html(str(r.get(c,''))) for c in cols])
        data_flags = " ".join([str(x) for x in r.get("_flags_list", [])])
        body_rows.append(f"<tr data-text='{_esc(data_text)}' data-flags='{_esc(data_flags)}'>{''.join(tds)}</tr>")

    body = f"""
<div class="card">
  <div class="search">
    <input id="search" placeholder="搜索（支持全字段/按字段）…"/>
    <select id="col">{col_opts}</select>
    <select id="flag"><option value="all">全部 flags</option>{flag_opts}</select>
    <span class="badge">显示 <span id="rowCount">0</span> 条</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>{thead}</tr></thead>
      <tbody>{''.join(body_rows)}</tbody>
    </table>
  </div>
</div>
"""
    return _layout(meta, nav, title, subtitle, body)


def _strip_html(s: str) -> str:
    return s.replace("\n", " ").replace("\r", " ").replace("\t", " ")


def _rows_raw(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for x in items:
        src = x.get("source") or {}
        url = x.get("url") or ""
        rid = str(x.get("id") or "")
        rows.append(
            {
                "_cols": ["id", "published_at", "domain", "title", "url", "image"],
                "id": _esc(rid),
                "published_at": _esc(x.get("published_at") or ""),
                "domain": _esc(src.get("domain") or _domain_from_url(url)),
                "title": _esc(x.get("title") or ""),
                "url": f"<a href='{_esc(url)}' target='_blank'>open</a>" if url else "",
                "image": f"<img class='thumb' src='{_esc(_pick_first(x.get('image_urls')) or '')}'/>" if _pick_first(x.get("image_urls")) else "",
                "_flags_list": [],
            }
        )
    return rows


def _rows_scored(scored: List[Dict[str, Any]], raw_by_id: Dict[str, Dict[str, Any]], selected_raw_ids: set) -> List[Dict[str, Any]]:
    rows = []
    for s in sorted(scored, key=lambda x: float(x.get("value_score") or 0.0), reverse=True):
        rid = str(s.get("raw_id") or "")
        raw = raw_by_id.get(rid) or {}
        url = raw.get("url") or ""
        flags = s.get("risk_flags") or []
        if not isinstance(flags, list):
            flags = [flags]
        rows.append(
            {
                "_cols": ["selected", "value_score", "event_id", "domain", "title", "risk_flags", "why", "url"],
                "selected": "<span class='badge good'>YES</span>" if rid in selected_raw_ids else "<span class='badge'>NO</span>",
                "value_score": _esc(round(float(s.get("value_score") or 0.0), 4)),
                "event_id": _esc(s.get("event_id") or ""),
                "domain": _esc((raw.get("source") or {}).get("domain") or _domain_from_url(url)),
                "title": _esc(raw.get("title") or ""),
                "risk_flags": _esc(", ".join([str(x) for x in flags])) if flags else "",
                "why": _esc(s.get("why_recommended") or ""),
                "url": f"<a href='{_esc(url)}' target='_blank'>open</a>" if url else "",
                "_flags_list": [str(x) for x in flags],
            }
        )
    return rows


def _rows_selected(selected: List[Dict[str, Any]], raw_by_id: Dict[str, Dict[str, Any]], scored_by_raw: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for it in selected:
        rid = str(it.get("raw_id") or it.get("id") or "")
        raw = raw_by_id.get(rid) or {}
        sc = scored_by_raw.get(rid) or {}
        url = raw.get("url") or ""
        flags = sc.get("risk_flags") or []
        if not isinstance(flags, list):
            flags = [flags]
        rows.append(
            {
                "_cols": ["value_score", "event_id", "domain", "title", "why", "risk_flags", "url"],
                "value_score": _esc(round(float(sc.get("value_score") or it.get("value_score") or 0.0), 4)),
                "event_id": _esc(sc.get("event_id") or it.get("event_id") or ""),
                "domain": _esc((raw.get("source") or {}).get("domain") or _domain_from_url(url)),
                "title": _esc(raw.get("title") or ""),
                "why": _esc(it.get("why_selected") or it.get("why_recommended") or sc.get("why_recommended") or ""),
                "risk_flags": _esc(", ".join([str(x) for x in flags])) if flags else "",
                "url": f"<a href='{_esc(url)}' target='_blank'>open</a>" if url else "",
                "_flags_list": [str(x) for x in flags],
            }
        )
    return rows


def _render_events(meta: Dict[str, Any], events: List[EventView], nav: List[Tuple[str, str]]) -> str:
    rows = []
    for e in sorted(events, key=lambda x: (x.has_script, x.avg_value_score, x.n_items), reverse=True):
        badge = "<span class='badge good'>script</span>" if e.has_script else "<span class='badge warn'>no script</span>"
        rows.append(
            f"<tr data-text='{_esc(e.event_id+' '+e.topic+' '+e.title)}' data-flags=''>"
            f"<td><a href='{_esc(e.page_rel)}'>{_esc(e.event_id)}</a></td>"
            f"<td>{_esc(round(e.avg_value_score,4))}</td>"
            f"<td>{_esc(e.n_items)}</td>"
            f"<td>{badge}</td>"
            f"<td>{_esc(e.title)}</td>"
            f"<td class='small'>{_esc(e.topic)}</td>"
            f"</tr>"
        )

    body = f"""
<div class="card">
  <div class="search">
    <input id="search" placeholder="搜索事件（event_id / title / topic）…"/>
    <select id="col"><option value="all">全部字段</option></select>
    <select id="flag"><option value="all">全部 flags</option></select>
    <span class="badge">显示 <span id="rowCount">0</span> 条</span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>event_id</th><th>avg_score</th><th>items</th><th>script</th><th>title</th><th>topic</th>
      </tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
</div>
"""
    return _layout(meta, nav, "EVENTS（事件聚合）", "用事件视角检查覆盖与遗漏", body)


def _render_gaps(
    meta: Dict[str, Any],
    counts: SummaryCounts,
    nav: List[Tuple[str, str]],
    scored_rows: List[Dict[str, Any]],
    selected_raw_ids: set,
    event_views: List[EventView],
    story_by_event: Dict[str, Dict[str, Any]],
    has_script: set,
) -> str:
    # 1) top high-score but not selected
    top_missed = []
    for r in scored_rows:
        # selected col contains YES/NO badge
        if "YES" in r.get("selected", ""):
            continue
        try:
            vs = float(str(r.get("value_score")).strip())
        except Exception:
            continue
        if vs <= 0:
            continue
        top_missed.append((vs, r))
    top_missed.sort(key=lambda x: x[0], reverse=True)
    top_missed = top_missed[:50]

    missed_rows = "".join(
        [
            "<tr>"
            f"<td>{m[1].get('value_score','')}</td>"
            f"<td>{m[1].get('event_id','')}</td>"
            f"<td>{m[1].get('domain','')}</td>"
            f"<td>{m[1].get('title','')}</td>"
            f"<td class='small'>{m[1].get('why','')}</td>"
            f"<td class='small'>{m[1].get('risk_flags','')}</td>"
            f"<td>{m[1].get('url','')}</td>"
            "</tr>"
            for m in top_missed
        ]
    )

    # 2) events with no script but decent score
    no_script = [e for e in event_views if not e.has_script and e.n_items > 0]
    no_script.sort(key=lambda e: (e.avg_value_score, e.n_items), reverse=True)
    no_script = no_script[:30]
    ns_rows = "".join(
        [
            "<tr>"
            f"<td><a href='{_esc(e.page_rel)}'>{_esc(e.event_id)}</a></td>"
            f"<td>{_esc(round(e.avg_value_score,4))}</td>"
            f"<td>{_esc(e.n_items)}</td>"
            f"<td>{_esc(e.title)}</td>"
            f"<td class='small'>{_esc(e.topic)}</td>"
            "</tr>"
            for e in no_script
        ]
    )

    body = f"""
<div class="grid">
  <div class="card" style="grid-column:span 12">
    <div style="font-weight:900">覆盖概览</div>
    <div class="small muted" style="margin-top:6px">
      RAW={counts.raw} → DEDUP={counts.dedup} → SCORED={counts.scored} → SELECTED={counts.selected} → EVENTS={counts.events} → SCRIPT={counts.scriptpacks}
    </div>
  </div>

  <div class="card" style="grid-column:span 12">
    <div style="font-weight:900;margin-bottom:8px">潜在遗漏 ①：高分但未入选（Top 50）</div>
    <div class="small muted" style="margin-bottom:10px">先从这里抽查：如果这些条目对应的事件很重要，说明 selection 阈值/去重/聚类需要调整。</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>score</th><th>event_id</th><th>domain</th><th>title</th><th>why</th><th>flags</th><th>url</th></tr></thead>
        <tbody>{missed_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="card" style="grid-column:span 12">
    <div style="font-weight:900;margin-bottom:8px">潜在遗漏 ②：有事件但没生成脚本（Top 30）</div>
    <div class="small muted" style="margin-bottom:10px">可能是 storypacks/scriptpacks 生成失败、或 event_id 不一致、或模板导出被跳过。</div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>event_id</th><th>avg_score</th><th>items</th><th>title</th><th>topic</th></tr></thead>
        <tbody>{ns_rows}</tbody>
      </table>
    </div>
  </div>
</div>
"""
    return _layout(meta, nav, "GAPS（潜在遗漏检查）", "自动列出你最应该人工复核的区域", body)


def _render_event_detail(
    meta: Dict[str, Any],
    ev: EventView,
    nav: List[Tuple[str, str]],
    articles: List[Dict[str, Any]],
    story_json: str,
    script_html: str,
    script_json: str,
) -> str:
    art_rows = []
    for a in articles:
        badge_sel = "<span class='badge good'>selected</span>" if a.get("selected") else "<span class='badge'>not selected</span>"
        img = f"<img class='thumb' src='{_esc(a.get('image_url') or '')}'/>" if a.get("image_url") else ""
        art_rows.append(
            "<div class='card'>"
            f"<div style='display:flex;gap:12px'>"
            f"<div>{img}</div>"
            f"<div style='flex:1'>"
            f"<div style='font-weight:900'>{_esc(a.get('title') or '')}</div>"
            f"<div class='small muted' style='margin-top:4px'>{_esc(a.get('domain') or '')} · {_esc(a.get('published_at') or '')} · score={_esc(round(float(a.get('value_score') or 0.0),4))} {badge_sel}</div>"
            f"<div class='small' style='margin-top:6px'>{_esc(a.get('why') or '')}</div>"
            f"<div class='small muted' style='margin-top:6px'>flags: {_esc(_join_items(a.get('risk_flags') or []))} · angles: {_esc(_join_items(a.get('angles') or []))}</div>"
            f"<div style='margin-top:8px'><a href='{_esc(a.get('url') or '')}' target='_blank'>open source</a></div>"
            f"</div></div>"
            "</div>"
        )

    body = f"""
<div class="card">
  <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-end">
    <div>
      <div class="h1" style="font-size:20px;margin:0">{_esc(ev.title)}</div>
      <div class="small muted">event_id={_esc(ev.event_id)} · avg_score={_esc(round(ev.avg_value_score,4))} · items={_esc(ev.n_items)} · script={'YES' if ev.has_script else 'NO'}</div>
    </div>
    <div><a class="badge" href="events.html">← back</a></div>
  </div>
</div>

<div class="two-col" style="margin-top:14px">
  <div>
    <div class="card">
      <div style="font-weight:900;margin-bottom:8px">原始新闻（此事件）</div>
      <div class="small muted" style="margin-bottom:10px">这里用于核查：脚本是否覆盖了关键事实、是否存在重要来源被漏掉。</div>
      {''.join(art_rows) if art_rows else "<p class='muted'>No articles matched for this event.</p>"}
    </div>
  </div>

  <div>
    <div class="card">
      <div style="font-weight:900;margin-bottom:8px">故事线（storypack）</div>
      <pre>{_esc(story_json)}</pre>
    </div>

    <div class="card" style="margin-top:14px">
      <div style="font-weight:900;margin-bottom:8px">脚本预览（script.md）</div>
      <div>{script_html}</div>
    </div>

    <div class="card" style="margin-top:14px">
      <div style="font-weight:900;margin-bottom:8px">脚本结构（script.json）</div>
      <pre>{_esc(script_json)}</pre>
    </div>
  </div>
</div>
"""
    return _layout(meta, nav, f"EVENT {ev.event_id}", "单事件全链路：新闻→打分→故事线→脚本", body)
