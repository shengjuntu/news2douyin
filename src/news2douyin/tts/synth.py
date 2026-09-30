from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, Tuple
import re


class TTSError(RuntimeError):
    pass


def available_backend() -> str:
    try:
        import edge_tts  # noqa: F401
        return "edge-tts"
    except Exception:
        try:
            import pyttsx3  # noqa: F401
            return "pyttsx3"
        except Exception:
            return "none"




def markdown_to_tts_text(md: str, lang: str = "zh") -> str:
    """
    Convert Markdown script into TTS-friendly plain text.
    - Removes markdown tokens (#, ##, -, links, emphasis)
    - Keeps section structure in spoken form
    """
    s = md.strip().replace("\r\n", "\n")

    # 1) Extract link text: [text](url) -> text
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)

    # 2) Headings
    # # Title -> 标题：Title
    s = re.sub(r"^\s*#\s+(.+)$", r"标题：\1\n", s, flags=re.M)
    # ## Section -> 开场。 / 正文。 etc.
    def _sec(m):
        t = m.group(1).strip()
        # 常见段落名做口播化（你也可以扩展映射）
        zh_map = {
            "开场 Hook": "开场。",
            "开场": "开场。",
            "正文": "正文。",
            "收尾": "收尾。",
            "来源": "来源。"
        }
        en_map = {
            "Hook": "Hook.",
            "Body": "Body.",
            "Outro": "Outro.",
            "Source": "Source."
        }
        if lang.startswith("zh"):
            return (zh_map.get(t, f"{t}。")) + "\n"
        else:
            return (en_map.get(t, f"{t}.")) + "\n"
    s = re.sub(r"^\s*##\s+(.+)$", _sec, s, flags=re.M)

    # 3) Bullets: "- xxx" -> "此外，xxx。"（更自然也更稳）
    lines = []
    bullet_idx = 0
    for line in s.split("\n"):
        m = re.match(r"^\s*-\s+(.+)$", line)
        if m:
            bullet_idx += 1
            item = m.group(1).strip()
            if lang.startswith("zh"):
                prefix = "此外，" if bullet_idx > 1 else ""
                lines.append(f"{prefix}{item}。")
            else:
                prefix = "Additionally, " if bullet_idx > 1 else ""
                if not item.endswith((".", "!", "?")):
                    item += "."
                lines.append(prefix + item)
        else:
            bullet_idx = 0 if line.strip() == "" else bullet_idx
            lines.append(line)
    s = "\n".join(lines)

    # 4) Remove emphasis/code/blockquote tokens
    s = s.replace("**", "").replace("*", "")
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.M)

    # 5) Collapse extra blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)

    # 6) Cleanup “来源”部分：保留媒体名，去掉多余符号
    # 例如 "- Boston Globe" 这种已经在 bullets 处理中变成句子了
    # 你也可以选择：来源段落改成“来源：Boston Globe。”
    if lang.startswith("zh"):
        s = re.sub(r"^\s*来源。\s*$", "来源。\n", s, flags=re.M)

    # 7) Final trim
    return s.strip()


async def _edge_tts_to_file(text: str, out_path: Path, voice: str, rate: str = "+0%") -> None:
    import edge_tts

    out_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(str(out_path))


def _pyttsx3_to_wav(text: str, out_path: Path, voice_hint: Optional[str] = None, rate: int = 180) -> None:
    import pyttsx3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    engine = pyttsx3.init()
    try:
        engine.setProperty("rate", rate)
        if voice_hint:
            for v in engine.getProperty("voices") or []:
                if voice_hint.lower() in (v.name or "").lower() or voice_hint.lower() in (v.id or "").lower():
                    engine.setProperty("voice", v.id)
                    break
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
    finally:
        try:
            engine.stop()
        except Exception:
            pass


def synthesize(
    text: str,
    out_path: Path,
    *,
    backend: str = "auto",
    voice: Optional[str] = None,
    lang: str = "zh",
    preprocess_markdown: bool = True,
) -> Tuple[str, Path]:
    """Synthesize speech to file.

    Returns (backend_used, out_path).
    - edge-tts: out_path should be .mp3 or .wav (mp3 recommended)
    - pyttsx3: out_path should be .wav
    """
    # --- Normalize Markdown to TTS-friendly plain text ---
    if preprocess_markdown:
        # Only run when markdown-like tokens appear
        if ("#" in text) or ("```" in text) or ("\n-" in text) or ("[" in text and "](" in text):
            text = markdown_to_tts_text(text, lang=lang)

    if backend == "auto":
        backend = available_backend()

    if backend == "edge-tts":
        if not voice:
            raise TTSError("edge-tts requires a voice (e.g., zh-CN-XiaoxiaoNeural)")
        asyncio.run(_edge_tts_to_file(text, out_path, voice=voice))
        return backend, out_path

    if backend == "pyttsx3":
        _pyttsx3_to_wav(text, out_path, voice_hint=voice)
        return backend, out_path

    raise TTSError("No TTS backend available. Install 'edge-tts' (recommended) or 'pyttsx3'.")
