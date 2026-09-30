import argparse
import json
import logging
import os
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from tqdm import tqdm

log = logging.getLogger("news2video_prep")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    items = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def safe_filename(s: str, max_len: int = 120) -> str:
    s = re.sub(r"[^\w\-.]+", "_", s, flags=re.UNICODE).strip("_")
    return s[:max_len] if len(s) > max_len else s


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Return SHA256 of file content for de-duplication."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def dedupe_by_hash(path: Path, seen_hashes: set) -> bool:
    """Keep file if its hash is new; delete and return False if duplicate."""
    try:
        sig = file_sha256(path)
        if sig in seen_hashes:
            path.unlink(missing_ok=True)
            return False
        seen_hashes.add(sig)
        return True
    except Exception:
        # If hashing fails, keep the file to avoid losing assets.
        return True


def parse_dt(s: str):
    if not s:
        return None
    try:
        return dtparser.parse(s)
    except Exception:
        return None


@dataclass
class RawItem:
    id: str
    url: str
    title: str
    content: str
    published_at: str
    fetched_at: str
    language: str
    country: str
    image_urls: List[str]
    source_name: str


def build_raw_index(raw_items: List[Dict[str, Any]]) -> Dict[str, RawItem]:
    idx: Dict[str, RawItem] = {}
    for r in raw_items:
        idx[r["id"]] = RawItem(
            id=r.get("id", ""),
            url=r.get("url", ""),
            title=r.get("title", ""),
            content=r.get("content", ""),
            published_at=r.get("published_at", ""),
            fetched_at=r.get("fetched_at", ""),
            language=r.get("language", ""),
            country=r.get("country", ""),
            image_urls=r.get("image_urls") or [],
            source_name=((r.get("source") or {}).get("name") or ""),
        )
    return idx


def group_scored_by_event(scored_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in scored_items:
        eid = s.get("event_id") or ""
        if eid:
            groups.setdefault(eid, []).append(s)
    return groups


def choose_event_articles(event_id: str, scored_group: List[Dict[str, Any]], raw_index: Dict[str, RawItem], max_articles: int) -> List[RawItem]:
    def keyfun(s: Dict[str, Any]):
        vs = s.get("value_score", 0)
        rid = s.get("raw_id", "")
        dt = parse_dt(raw_index.get(rid, RawItem("", "", "", "", "", "", "", "", [], "")).published_at)
        return (vs, dt or datetime(1970, 1, 1))

    scored_sorted = sorted(scored_group, key=keyfun, reverse=True)
    out: List[RawItem] = []
    for s in scored_sorted:
        rid = s.get("raw_id")
        if rid and rid in raw_index:
            out.append(raw_index[rid])
        if len(out) >= max_articles:
            break
    return out


# -------------------------
# Image download
# -------------------------

def _abs_url(base_url: str, u: str) -> str:
    return requests.compat.urljoin(base_url, u)


def extract_image_candidates(html: str, page_url: str) -> List[str]:
    soup = BeautifulSoup(html, "lxml")
    cands: List[str] = []

    for prop in ["og:image", "og:image:url", "twitter:image", "twitter:image:src"]:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            cands.append(_abs_url(page_url, tag["content"]))

    for tag in soup.select("img"):
        src = tag.get("src") or tag.get("data-src") or tag.get("data-original") or ""
        if not src:
            continue
        src = _abs_url(page_url, src)
        if any(k in src.lower() for k in ["sprite", "logo", "icon", "avatar", "data:image"]):
            continue
        cands.append(src)

    seen = set()
    out = []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def fetch_html(url: str, timeout: int) -> Optional[str]:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception:
        return None


def download_file(url: str, out_path: Path, timeout: int) -> bool:
    try:
        with requests.get(url, headers={"User-Agent": UA, "Referer": url}, stream=True, timeout=timeout, allow_redirects=True) as r:
            if r.status_code != 200:
                return False
            ensure_dir(out_path.parent)
            with out_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 128):
                    if chunk:
                        f.write(chunk)
        if out_path.stat().st_size < 8 * 1024:
            out_path.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        return False


def download_images_for_article(
    raw: RawItem,
    out_dir: Path,
    max_images: int,
    timeout: int,
    seen_hashes: Optional[set] = None,
    article_idx: int = 0,
) -> List[Dict[str, Any]]:
    """Download up to max_images images for an article into out_dir.

    De-duplication:
      - URL de-dupe within the article
      - Optional content-hash de-dupe across a broader scope (e.g., within an event)

    Files are named with the article index to keep them unique inside a single event folder.
    """
    ensure_dir(out_dir)
    records: List[Dict[str, Any]] = []

    seed_urls = list(raw.image_urls or [])
    html = fetch_html(raw.url, timeout=timeout)
    if html:
        seed_urls.extend(extract_image_candidates(html, raw.url))

    seen_urls = set()
    urls: List[str] = []
    for u in seed_urls:
        if u and u not in seen_urls:
            seen_urls.add(u)
            urls.append(u)

    if seen_hashes is None:
        seen_hashes = set()

    ok_count = 0
    for u in urls:
        if ok_count >= max_images:
            break

        ext = ".jpg"
        m = re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", u.lower())
        if m:
            ext = "." + m.group(1).replace("jpeg", "jpg")

        fname = f"a{article_idx:02d}_img{len(records):02d}{ext}"
        p = out_dir / fname

        ok = download_file(u, p, timeout=timeout)
        kept = False
        if ok:
            kept = dedupe_by_hash(p, seen_hashes)
            if kept:
                ok_count += 1
            else:
                ok = False  # treat duplicate as "not a new image"

        records.append({
            "article_id": raw.id,
            "article_idx": article_idx,
            "url": u,
            "path": str(p),
            "ok": ok,
            "kept": kept,
        })

    return records


# -------------------------
# Script generation
# -------------------------

def offline_outline(event_id: str, articles: List[RawItem], target_lang: str) -> str:
    lead = articles[0] if articles else None
    title = (lead.title if lead else event_id).strip()
    url = lead.url if lead else ""
    text = (lead.content if lead else "").strip().replace("\r", "\n")

    parts = re.split(r"(?<=[\.\!\?。！？])\s+", text)
    bullets = [p.strip() for p in parts if len(p.strip()) > 30][:6]
    bullets = bullets or [text[:220] + ("..." if len(text) > 220 else "")]

    if target_lang == "zh":
        return f"# {title}\n\n来源：{url}\n\n## 要点\n" + "\n".join([f"- {b}" for b in bullets]) + "\n"
    return f"# {title}\n\nSource: {url}\n\n## Key points\n" + "\n".join([f"- {b}" for b in bullets]) + "\n"


def build_llm_prompt(event_id: str, articles: List[RawItem], target_lang: str) -> str:
    blocks = []
    for a in articles[:5]:
        excerpt = re.sub(r"\s+", " ", (a.content or "").strip())[:900]
        blocks.append(
            f"- TITLE: {a.title}\n  URL: {a.url}\n  PUBLISHED_AT: {a.published_at}\n  EXCERPT: {excerpt}"
        )
    sources = "\n".join(blocks)

    if target_lang == "en":
        return (
            "You are a professional short-video news editor.\n\n"
            f"TASK:\nWrite an English narration script for a 30-45s vertical short video about this event: {event_id}.\n"
            "- Be factual. If a claim is not supported by sources, mark it as unconfirmed.\n"
            "- Use an engaging but neutral tone.\n"
            "- Output in Markdown with:\n"
            "  1) Title (1 line)\n"
            "  2) Hook (1-2 lines)\n"
            "  3) Body (6-10 short sentences)\n"
            "  4) Closing (1 line)\n"
            "  5) Sources (bullet list of URLs used)\n\n"
            f"SOURCES:\n{sources}\n"
        )

    return (
        "你是一名专业短视频新闻编辑。\n\n"
        f"任务：为事件 {event_id} 写一份中文口播脚本（适合 30-45 秒竖屏短视频）。\n"
        "- 只基于给定来源，不要编造细节；不确定信息要标注“尚未证实/信息不足”。\n"
        "- 语气：吸引人但克制，偏新闻速递/解说。\n"
        "- 用 Markdown 输出，包含：\n"
        "  1) 标题（1 行）\n"
        "  2) 开场 Hook（1-2 行）\n"
        "  3) 正文（6-10 句短句）\n"
        "  4) 收尾（1 行）\n"
        "  5) 来源（用到的 URL 列表）\n\n"
        f"来源材料：\n{sources}\n"
    )


def call_openai(prompt: str, model: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    # Prefer official SDK if installed
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=model,
            input=prompt,
            temperature=0.4,
        )
        return resp.output_text
    except Exception:
        # REST fallback
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "input": prompt, "temperature": 0.4}
        r = requests.post("https://api.openai.com/v1/responses", headers=headers, json=payload, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text}")
        j = r.json()
        if "output_text" in j and j["output_text"]:
            return j["output_text"]
        texts = []
        for o in j.get("output", []):
            for c in o.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))
        return "\n".join(texts).strip()

def call_vllm_chat(prompt: str, model: str, base_url: str, api_key: str) -> str:
    """Call a local vLLM server via OpenAI-compatible Chat Completions API."""
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("openai python package is required for --llm vllm") from e

    client = OpenAI(base_url=base_url, api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a professional short-video news editor."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )
    return (resp.choices[0].message.content or "").strip()



def generate_script(
    event_id: str,
    articles: List[RawItem],
    target_lang: str,
    llm: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
) -> str:
    if llm == "none":
        return offline_outline(event_id, articles, target_lang)

    prompt = build_llm_prompt(event_id, articles, target_lang)

    if llm == "openai":
        return call_openai(prompt, model=model)

    if llm == "vllm":
        if not base_url:
            raise ValueError("--base-url is required when --llm vllm")
        if not api_key:
            api_key = "EMPTY"
        return call_vllm_chat(prompt, model=model, base_url=base_url, api_key=api_key)

    raise ValueError(f"Unknown llm: {llm}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm", default="none", choices=["none", "openai", "vllm"])
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8000/v1"))
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY", "EMPTY"))
    ap.add_argument("--max-articles", type=int, default=5)
    ap.add_argument("--max-images", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = ap.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    run_dir = Path(args.run_dir).resolve()
    out_dir = Path(args.out).resolve()
    ensure_dir(out_dir)

    selected_path = run_dir / "selected.json"
    raw_path = run_dir / "raw.jsonl"
    scored_path = run_dir / "scored.jsonl"

    for p in [selected_path, raw_path, scored_path]:
        if not p.exists():
            raise FileNotFoundError(f"Missing required file: {p}")

    selected = json.loads(selected_path.read_text(encoding="utf-8"))
    raw_items = read_jsonl(raw_path)
    scored_items = read_jsonl(scored_path)

    raw_index = build_raw_index(raw_items)
    by_event = group_scored_by_event(scored_items)

    items = selected.get("items", [])
    if not isinstance(items, list):
        raise ValueError("selected.json must contain a top-level key 'items' as a list")

    for i, it in enumerate(tqdm(items, desc="events")):
        event_id = it.get("event_id") or it.get("id") or f"event_{i:03d}"
        event_slug = safe_filename(str(event_id), max_len=80)
        event_dir = out_dir / event_slug
        ensure_dir(event_dir)

        # event-level de-dupe across all articles within this event
        event_seen_hashes = set()

        scored_group = by_event.get(str(event_id), []) or by_event.get(event_id, [])
        articles = choose_event_articles(str(event_id), scored_group, raw_index, max_articles=args.max_articles)

        # Persist article metadata for manual review / debugging
        (event_dir / "articles.json").write_text(
            json.dumps([{
                "id": a.id,
                "title": a.title,
                "url": a.url,
                "published_at": a.published_at,
                "source_name": a.source_name,
                "language": a.language,
                "country": a.country,
            } for a in articles], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        all_img_records: List[Dict[str, Any]] = []
        for ai, a in enumerate(articles):
            imgs = download_images_for_article(
                a,
                event_dir,  # <--- images go directly into the event folder
                max_images=args.max_images,
                timeout=args.timeout,
                seen_hashes=event_seen_hashes,
                article_idx=ai,
            )
            all_img_records.extend(imgs)

        (event_dir / "images.json").write_text(
            json.dumps(all_img_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Generate scripts into the same event folder
        try:
            en_text = generate_script(
                str(event_id),
                articles,
                "en",
                llm=args.llm,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
            )
            (event_dir / "script_en.txt").write_text(en_text, encoding="utf-8")
        except Exception as e:
            log.exception("Failed to generate EN script for %s: %s", event_id, e)

        try:
            zh_text = generate_script(
                str(event_id),
                articles,
                "zh",
                llm=args.llm,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
            )
            (event_dir / "script_zh.txt").write_text(zh_text, encoding="utf-8")
        except Exception as e:
            log.exception("Failed to generate ZH script for %s: %s", event_id, e)


if __name__ == "__main__":
    main()
