#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
md2timeline_html.py
Convert a "事件回顾时间线" markdown into a 9:16 multi-page long HTML.

Usage:
  python md2timeline_html.py 事件回顾时间线.md -o out.html
  python md2timeline_html.py timeline.md -o out.html --title "美伊局势回顾" --range "2025.1 - 2026.2"
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple


# -----------------------------
# Data model
# -----------------------------
@dataclass
class Event:
    tag: Optional[str]          # "军事/外交/经济/..." or None
    title: str                  # event title
    desc: str                   # event description (plain text)
    date_hint: Optional[str]    # resolved date string if available (e.g., "2月6日", "2026年2月6日")
    year: Optional[str] = None
    phase: Optional[str] = None  # H3
    subphase: Optional[str] = None  # H4


@dataclass
class Phase:
    title: str                  # H3 text
    subphases: List[Tuple[str, List[Event]]] = field(default_factory=list)  # (H4 title, events)
    events: List[Event] = field(default_factory=list)  # events directly under H3 (no H4)


@dataclass
class YearBlock:
    year: str                   # "2025年"
    phases: List[Phase] = field(default_factory=list)


@dataclass
class TableBlock:
    title: str                  # preceding heading (e.g., "军事事件")
    headers: List[str]
    rows: List[List[str]]


@dataclass
class Doc:
    title: str
    time_range: Optional[str]
    intro: Optional[str]
    years: List[YearBlock]
    tables: List[TableBlock]
    sources: Dict[str, List[str]]


# -----------------------------
# Markdown parsing (restricted, deterministic)
# -----------------------------
RE_H1 = re.compile(r"^#\s+(?P<t>.+?)\s*$")
RE_H2 = re.compile(r"^##\s+(?P<t>.+?)\s*$")
RE_H3 = re.compile(r"^###\s+(?P<t>.+?)\s*$")
RE_H4 = re.compile(r"^####\s+(?P<t>.+?)\s*$")
RE_HR = re.compile(r"^\s*---+\s*$")

# Event line:
# - **标题**：描述
# - [军事] **标题**：描述
RE_EVT = re.compile(
    r"^\-\s*(?:\[(?P<tag>[^\]]+)\]\s*)?\*\*(?P<title>.+?)\*\*：(?P<desc>.+?)\s*$"
)

# very loose date patterns to pull from headings/titles if needed
RE_DATE_HINT = re.compile(r"(?P<y>\d{4}年)?(?P<m>\d{1,2}月)(?P<d>\d{1,2}日)?")

def _strip_md_emphasis(s: str) -> str:
    # minimal cleanup: remove markdown bold/italics/backticks if they leak into desc
    s = re.sub(r"(\*\*|__)(.+?)(\*\*|__)", r"\2", s)
    s = re.sub(r"(\*|_)(.+?)(\*|_)", r"\2", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s.strip()

def _join_wrapped_lines(lines: List[str]) -> List[str]:
    """
    Join wrapped bullet descriptions:
    If a line starts with two spaces (or a tab), treat it as continuation of previous line.
    """
    out: List[str] = []
    for ln in lines:
        if (ln.startswith("  ") or ln.startswith("\t")) and out:
            out[-1] = out[-1].rstrip() + " " + ln.strip()
        else:
            out.append(ln.rstrip("\n"))
    return out

def _parse_table(lines: List[str], start: int) -> Tuple[Optional[TableBlock], int]:
    """
    Parse a markdown table starting at lines[start] if it looks like one.
    Returns (table_or_none, next_index).
    """
    if start >= len(lines):
        return None, start
    if "|" not in lines[start].strip():
        return None, start

    # Must have at least 2 lines: header and separator
    if start + 1 >= len(lines):
        return None, start
    header_ln = lines[start].strip()
    sep_ln = lines[start + 1].strip()
    if not (header_ln.startswith("|") and "|" in header_ln):
        return None, start
    if not re.search(r"\|\s*[-:]+\s*\|", sep_ln):
        return None, start

    def split_row(ln: str) -> List[str]:
        ln = ln.strip()
        if ln.startswith("|"):
            ln = ln[1:]
        if ln.endswith("|"):
            ln = ln[:-1]
        cells = [c.strip() for c in ln.split("|")]
        return [c for c in cells]

    headers = split_row(header_ln)
    i = start + 2
    rows: List[List[str]] = []
    while i < len(lines):
        ln = lines[i].strip()
        if not ln.startswith("|"):
            break
        rows.append(split_row(ln))
        i += 1

    # title is filled by caller (the previous heading)
    return TableBlock(title="", headers=headers, rows=rows), i

def parse_timeline_md(md_text: str) -> Doc:
    raw_lines = md_text.replace("\r\n", "\n").split("\n")
    lines = _join_wrapped_lines(raw_lines)

    title = "事件回顾时间线"
    time_range: Optional[str] = None
    intro: Optional[str] = None

    years: List[YearBlock] = []
    tables: List[TableBlock] = []
    sources: Dict[str, List[str]] = {}

    cur_year: Optional[YearBlock] = None
    cur_phase: Optional[Phase] = None
    cur_subphase_title: Optional[str] = None
    cur_subphase_events: Optional[List[Event]] = None

    # sources parsing state
    in_sources = False
    cur_source_group: Optional[str] = None

    # last heading for table title fallback
    last_heading_for_table: Optional[str] = None

    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()

        if not ln.strip():
            i += 1
            continue

        m1 = RE_H1.match(ln)
        if m1:
            title = m1.group("t").strip()
            last_heading_for_table = title
            i += 1
            continue

        m2 = RE_H2.match(ln)
        if m2:
            h2 = m2.group("t").strip()
            last_heading_for_table = h2

            # time range line often looks like: ## （2025年1月 - 2026年2月）
            if "（" in h2 and "）" in h2 and re.search(r"\d{4}年", h2):
                time_range = h2.strip("（）").strip()
                i += 1
                continue

            # sources section trigger
            if h2.startswith("原始资料来源") or h2.startswith("资料来源") or h2.startswith("来源"):
                in_sources = True
                cur_source_group = None
                i += 1
                continue
            else:
                in_sources = False
                cur_source_group = None

            # year block
            if re.match(r"^\d{4}年$", h2):
                cur_year = YearBlock(year=h2, phases=[])
                years.append(cur_year)
                cur_phase = None
                cur_subphase_title = None
                cur_subphase_events = None
            i += 1
            continue

        m3 = RE_H3.match(ln)
        if m3:
            h3 = m3.group("t").strip()
            last_heading_for_table = h3

            # If inside sources, treat H3 as a group title (官方声明/新闻报道/...)
            if in_sources:
                cur_source_group = h3
                sources.setdefault(cur_source_group, [])
                i += 1
                continue

            # regular phase
            if cur_year is None:
                # tolerate missing year by creating a dummy
                cur_year = YearBlock(year="未分组", phases=[])
                years.append(cur_year)

            cur_phase = Phase(title=h3)
            cur_year.phases.append(cur_phase)

            cur_subphase_title = None
            cur_subphase_events = None
            i += 1
            continue

        m4 = RE_H4.match(ln)
        if m4:
            h4 = m4.group("t").strip()
            last_heading_for_table = h4

            if cur_phase is None:
                # tolerate missing H3
                if cur_year is None:
                    cur_year = YearBlock(year="未分组", phases=[])
                    years.append(cur_year)
                cur_phase = Phase(title="未命名阶段")
                cur_year.phases.append(cur_phase)

            cur_subphase_title = h4
            cur_subphase_events = []
            cur_phase.subphases.append((cur_subphase_title, cur_subphase_events))
            i += 1
            continue

        if RE_HR.match(ln):
            i += 1
            continue

        # Table?
        tbl, next_i = _parse_table(lines, i)
        if tbl is not None:
            tbl.title = last_heading_for_table or "表格"
            # cleanup cells
            tbl.headers = [_strip_md_emphasis(c) for c in tbl.headers]
            tbl.rows = [[_strip_md_emphasis(c) for c in row] for row in tbl.rows]
            tables.append(tbl)
            i = next_i
            continue

        # Source bullet under sources group
        if in_sources and ln.lstrip().startswith("-"):
            item = ln.lstrip()[1:].strip()
            if cur_source_group is None:
                cur_source_group = "未分组来源"
                sources.setdefault(cur_source_group, [])
            sources[cur_source_group].append(_strip_md_emphasis(item))
            i += 1
            continue

        # Intro block (optional): a single blockquote line after header
        if ln.startswith(">") and intro is None:
            intro = ln[1:].strip()
            i += 1
            continue

        # Event line
        me = RE_EVT.match(ln)
        if me:
            tag = me.group("tag")
            etitle = _strip_md_emphasis(me.group("title"))
            edesc = _strip_md_emphasis(me.group("desc"))

            # date hint from current h4/h3 if it contains recognizable date
            date_hint = None
            for ctx in [cur_subphase_title, cur_phase.title if cur_phase else None, cur_year.year if cur_year else None]:
                if not ctx:
                    continue
                mdate = RE_DATE_HINT.search(ctx)
                if mdate:
                    # keep the matched text as hint
                    date_hint = mdate.group(0)
                    # if year exists in context, keep it
                    # (example: "2026年2月6日")
                    break

            ev = Event(
                tag=tag.strip() if tag else None,
                title=etitle,
                desc=edesc,
                date_hint=date_hint,
                year=cur_year.year if cur_year else None,
                phase=cur_phase.title if cur_phase else None,
                subphase=cur_subphase_title,
            )

            if cur_subphase_events is not None:
                cur_subphase_events.append(ev)
            elif cur_phase is not None:
                cur_phase.events.append(ev)
            else:
                # tolerate loose format
                if cur_year is None:
                    cur_year = YearBlock(year="未分组", phases=[])
                    years.append(cur_year)
                cur_phase = Phase(title="未命名阶段")
                cur_year.phases.append(cur_phase)
                cur_phase.events.append(ev)

            i += 1
            continue

        # Otherwise: ignore stray text, but keep last heading context
        i += 1

    return Doc(
        title=title,
        time_range=time_range,
        intro=intro,
        years=years,
        tables=tables,
        sources=sources,
    )


# -----------------------------
# HTML rendering
# -----------------------------
CSS = r"""
:root{
  --bg:#050505;
  --panel:#0a0a0a;
  --card:rgba(255,255,255,.04);
  --border:rgba(255,255,255,.10);
  --muted:#a1a1aa;
  --muted2:#71717a;
  --red:#dc2626;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  background:var(--bg); color:#fff; padding-bottom:100px;
}
.control-panel{
  position:fixed; top:0; left:0; right:0;
  background:rgba(20,20,20,.95);
  border-bottom:1px solid rgba(220,38,38,.30);
  padding:16px 20px; z-index:1000;
  display:flex; gap:12px; flex-wrap:wrap; justify-content:center; align-items:center;
}
.btn{
  padding:10px 18px; border:none; border-radius:10px;
  font-size:14px; font-weight:700; cursor:pointer;
  background:rgba(255,255,255,.08); color:#fff;
  border:1px solid rgba(255,255,255,.12);
}
.btn.primary{ background:var(--red); border-color:transparent; }
.btn:hover{ transform:translateY(-1px); }
.progress{
  position:fixed; top:70px; left:50%; transform:translateX(-50%);
  background:rgba(0,0,0,.9);
  padding:12px 18px; border-radius:10px; border:1px solid var(--red);
  display:none; z-index:1001; font-size:14px; color:var(--red); font-weight:800;
}
.pages{ margin-top:92px; display:flex; flex-direction:column; align-items:center; gap:40px; }
.page{
  width:1080px; height:1920px; position:relative; overflow:hidden;
  background:linear-gradient(180deg,#0a0a0a 0%, #151515 100%);
  box-shadow:0 20px 60px rgba(0,0,0,.8);
}
.bg-grid{
  position:absolute; inset:0;
  background-image:
    linear-gradient(rgba(220,38,38,.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(220,38,38,.03) 1px, transparent 1px);
  background-size:60px 60px; pointer-events:none;
}
.watermark{
  position:absolute; bottom:40px; right:40px;
  font-size:22px; color:rgba(255,255,255,.20); font-weight:600;
}
.cover{
  padding:90px 70px; display:flex; flex-direction:column;
  justify-content:center; align-items:flex-start; gap:26px;
}
.badge{
  background:rgba(220,38,38,.18);
  border:2px solid var(--red);
  color:var(--red); padding:14px 26px; border-radius:8px;
  font-size:28px; font-weight:900; letter-spacing:6px;
}
.cover h1{
  font-size:96px; line-height:1.08; font-weight:950;
  background:linear-gradient(180deg,#fff 0%, #a1a1aa 100%);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent;
}
.cover .sub{
  font-size:38px; color:var(--muted2); line-height:1.35; font-weight:600;
}
.cover .range{
  margin-top:10px; font-size:34px; color:var(--red); font-weight:900;
  border:3px solid rgba(220,38,38,.45); padding:18px 26px; border-radius:14px;
}
.content{
  padding:90px 70px; display:flex; flex-direction:column;
}
.header{
  border-left:10px solid var(--red);
  padding-left:28px; margin-bottom:52px;
}
.header .kicker{ font-size:34px; color:var(--red); font-weight:950; letter-spacing:4px; margin-bottom:10px; }
.header .title{ font-size:72px; font-weight:950; line-height:1.12; margin-bottom:16px; }
.header .subtitle{ font-size:34px; color:var(--muted2); line-height:1.25; }

.cards{ display:flex; flex-direction:column; gap:24px; flex:1; }
.card{
  background:var(--card);
  border:2px solid var(--border);
  border-radius:24px;
  padding:38px;
  position:relative;
}
.card::before{
  content:""; position:absolute; top:0; left:0; right:0; height:6px;
  background:linear-gradient(90deg,var(--red),transparent);
}
.card .row{ display:flex; align-items:center; gap:18px; margin-bottom:18px; }
.icon{
  width:72px; height:72px; border-radius:18px;
  background:rgba(220,38,38,.18);
  display:flex; align-items:center; justify-content:center;
  font-size:34px; flex-shrink:0;
}
.card .t{ font-size:40px; font-weight:950; }
.card .d{ font-size:30px; line-height:1.55; color:var(--muted); }
.meta{ margin-top:14px; font-size:24px; color:rgba(255,255,255,.35); font-weight:650; }

.table{
  width:100%;
  border-collapse:separate; border-spacing:0;
  overflow:hidden; border-radius:18px;
  border:2px solid rgba(255,255,255,.10);
  background:rgba(255,255,255,.03);
}
.table th, .table td{
  padding:18px 16px; font-size:26px; line-height:1.35;
  border-bottom:1px solid rgba(255,255,255,.08);
  vertical-align:top;
}
.table th{ color:#fff; font-weight:900; background:rgba(220,38,38,.10); }
.table tr:last-child td{ border-bottom:none; }

.sources ul{ margin-top:16px; padding-left:26px; }
.sources li{ font-size:28px; color:var(--muted); line-height:1.55; margin:10px 0; }

@media (max-width: 1200px){
  .page{ width:100%; height:auto; aspect-ratio:9/16; }
}
"""

JS = r"""
let currentPage = 0;
const pages = document.querySelectorAll('.page');
const totalPages = pages.length;

async function generateImage(index){
  const page = pages[index];
  const canvas = await html2canvas(page, {
    width: 1080,
    height: 1920,
    scale: 2,
    useCORS: true,
    allowTaint: true,
    backgroundColor: '#0a0a0a',
    logging: false
  });
  const link = document.createElement('a');
  link.download = `timeline_${String(index+1).padStart(2,'0')}.png`;
  link.href = canvas.toDataURL('image/png');
  link.click();
}

async function generateAllImages(){
  const progress = document.getElementById('progress');
  const progressText = document.getElementById('progress-text');
  progress.style.display = 'block';
  for(let i=0;i<totalPages;i++){
    progressText.textContent = `${i+1}/${totalPages}`;
    await generateImage(i);
    await new Promise(r=>setTimeout(r, 450));
  }
  progress.textContent = '✅ 已生成并下载完成';
  setTimeout(()=>{ progress.style.display='none'; }, 2000);
}

function scrollToPage(i){
  if(i<0||i>=totalPages) return;
  pages[i].scrollIntoView({behavior:'smooth'});
  currentPage = i;
}

document.addEventListener('keydown', (e)=>{
  if(e.key==='ArrowDown' || e.key===' '){ e.preventDefault(); scrollToPage(currentPage+1); }
  if(e.key==='ArrowUp'){ e.preventDefault(); scrollToPage(currentPage-1); }
});

const observer = new IntersectionObserver((entries)=>{
  entries.forEach(entry=>{
    if(entry.isIntersecting){
      currentPage = Array.from(pages).indexOf(entry.target);
    }
  });
},{threshold:0.5});
pages.forEach(p=>observer.observe(p));
"""

def esc(s: str) -> str:
    return html.escape(s, quote=True)

def icon_for(tag: Optional[str]) -> str:
    if not tag:
        return "🧾"
    t = tag.strip()
    m = {
        "军事": "🛡️",
        "外交": "🤝",
        "经济": "💹",
        "制裁": "⛽",
        "谈判": "🕊️",
        "舆论": "🗞️",
        "国内": "🏛️",
    }
    return m.get(t, "📌")

def render_html(doc: Doc, author_watermark: str = "@timeline") -> str:
    # Build pages:
    # 1) cover
    # 2) per year: one or more pages by phases (greedy pack)
    # 3) tables pages
    # 4) sources page

    pages_html: List[str] = []

    cover_sub = doc.intro or "事件线 · 关键节点 · 多维影响"
    cover_range = doc.time_range or ""
    pages_html.append(f"""
    <div class="page cover">
      <div class="bg-grid"></div>
      <div class="badge">事件回顾</div>
      <h1>{esc(doc.title)}</h1>
      <div class="sub">{esc(cover_sub)}</div>
      <div class="range">{esc(cover_range)}</div>
      <div class="watermark">{esc(author_watermark)}</div>
    </div>
    """)

    # content pages for phases
    for y in doc.years:
        for ph in y.phases:
            # Page title strategy: year + phase
            kicker = y.year
            title = ph.title
            subtitle = "关键事件摘录（按时间线整理）"
            cards: List[str] = []

            # events directly under H3
            for ev in ph.events:
                cards.append(f"""
                <div class="card">
                  <div class="row">
                    <div class="icon">{icon_for(ev.tag)}</div>
                    <div class="t">{esc(ev.title)}</div>
                  </div>
                  <div class="d">{esc(ev.desc)}</div>
                  <div class="meta">{esc(" · ".join([x for x in [ev.date_hint, ev.subphase] if x]))}</div>
                </div>
                """)

            # events under each H4
            for (subt, evs) in ph.subphases:
                # add a small separator card (acts like a subheading)
                if evs:
                    cards.append(f"""
                    <div class="card">
                      <div class="row">
                        <div class="icon">🕒</div>
                        <div class="t">{esc(subt)}</div>
                      </div>
                      <div class="d">该阶段的关键节点如下：</div>
                    </div>
                    """)
                for ev in evs:
                    cards.append(f"""
                    <div class="card">
                      <div class="row">
                        <div class="icon">{icon_for(ev.tag)}</div>
                        <div class="t">{esc(ev.title)}</div>
                      </div>
                      <div class="d">{esc(ev.desc)}</div>
                      <div class="meta">{esc(" · ".join([x for x in [ev.date_hint] if x]))}</div>
                    </div>
                    """)

            if not cards:
                continue

            pages_html.append(f"""
            <div class="page content">
              <div class="bg-grid"></div>
              <div class="header">
                <div class="kicker">{esc(kicker)}</div>
                <div class="title">{esc(title)}</div>
                <div class="subtitle">{esc(subtitle)}</div>
              </div>
              <div class="cards">
                {''.join(cards[:6])}
              </div>
              <div class="watermark">{esc(author_watermark)}</div>
            </div>
            """)

            # If too many cards, spill to more pages
            remaining = cards[6:]
            while remaining:
                chunk = remaining[:7]
                remaining = remaining[7:]
                pages_html.append(f"""
                <div class="page content">
                  <div class="bg-grid"></div>
                  <div class="header">
                    <div class="kicker">{esc(kicker)}</div>
                    <div class="title">{esc(title)}（续）</div>
                    <div class="subtitle">{esc(subtitle)}</div>
                  </div>
                  <div class="cards">
                    {''.join(chunk)}
                  </div>
                  <div class="watermark">{esc(author_watermark)}</div>
                </div>
                """)

    # tables pages
    for tb in doc.tables:
        # title comes from last heading context (we keep it)
        hdr = f"表格总结：{tb.title}"
        table_html = ["<table class='table'><thead><tr>"]
        for h in tb.headers:
            table_html.append(f"<th>{esc(h)}</th>")
        table_html.append("</tr></thead><tbody>")
        for row in tb.rows[:14]:
            table_html.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
        table_html.append("</tbody></table>")

        pages_html.append(f"""
        <div class="page content">
          <div class="bg-grid"></div>
          <div class="header">
            <div class="kicker">态势总结</div>
            <div class="title">{esc(hdr)}</div>
            <div class="subtitle">将 Markdown 表格渲染为便于长图阅读的卡片式表格</div>
          </div>
          {''.join(table_html)}
          <div class="watermark">{esc(author_watermark)}</div>
        </div>
        """)

    # sources page
    if doc.sources:
        blocks = []
        for grp, items in doc.sources.items():
            li = "".join(f"<li>{esc(x)}</li>" for x in items[:20])
            blocks.append(f"<div class='card sources'><div class='t'>{esc(grp)}</div><ul>{li}</ul></div>")
        pages_html.append(f"""
        <div class="page content">
          <div class="bg-grid"></div>
          <div class="header">
            <div class="kicker">资料来源</div>
            <div class="title">原始资料来源</div>
            <div class="subtitle">可在发布时保留或删去（看平台要求）</div>
          </div>
          <div class="cards">
            {''.join(blocks[:5])}
          </div>
          <div class="watermark">{esc(author_watermark)}</div>
        </div>
        """)

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{esc(doc.title)}</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
  <div class="control-panel">
    <button class="btn primary" onclick="generateAllImages()">📸 生成所有图片</button>
    <button class="btn" onclick="scrollToPage(0)">⬆️ 回到顶部</button>
    <span style="color:rgba(255,255,255,.45);font-weight:700;">
      提示：将自动下载多张 1080×1920 PNG，可直接导入剪映
    </span>
  </div>

  <div class="progress" id="progress">正在生成图片... <span id="progress-text">0/0</span></div>

  <div class="pages" id="pages">
    {''.join(pages_html)}
  </div>

  <script>{JS}</script>
</body>
</html>
"""
    return html_doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md", type=str, help="input markdown file")
    ap.add_argument("-o", "--out", type=str, default="out.html", help="output html file")
    ap.add_argument("--title", type=str, default=None, help="override title")
    ap.add_argument("--range", type=str, default=None, help="override time range (cover)")
    ap.add_argument("--intro", type=str, default=None, help="override intro line (cover subtitle)")
    ap.add_argument("--watermark", type=str, default="@timeline", help="watermark text")
    args = ap.parse_args()

    md_path = Path(args.md)
    md_text = md_path.read_text(encoding="utf-8")
    doc = parse_timeline_md(md_text)

    if args.title:
        doc.title = args.title
    if args.range:
        doc.time_range = args.range
    if args.intro:
        doc.intro = args.intro

    out_html = render_html(doc, author_watermark=args.watermark)
    Path(args.out).write_text(out_html, encoding="utf-8")
    print(f"[OK] wrote: {args.out}")

if __name__ == "__main__":
    main()
