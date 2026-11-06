#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a static course website (juejin-course style) from markdown chapters."""
import os
import re
import html
import shutil
from markdown_it import MarkdownIt

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")
BOOK_TITLE = "从零打造一个 AI Agent CLI"
BOOK_SUBTITLE = "从 50 行代码到生产级 AI 编程助手"
REPO = "https://github.com/woai3c/hello-agent"


def read_chapters():
    chapters = []
    for name in os.listdir(SRC):
        if not name.endswith(".md"):
            continue
        m = re.match(r"^(\d+)-(.+)\.md$", name)
        if not m:
            continue
        num = int(m.group(1))
        slug = m.group(2)
        with open(os.path.join(SRC, name), encoding="utf-8") as f:
            text = f.read()
        chapters.append({"num": num, "slug": slug, "text": text, "file": name})
    chapters.sort(key=lambda c: c["num"])
    return chapters


def chapter_title(text, num):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            t = line[2:].strip()
            # strip leading "第 N 章 "
            t = re.sub(r"^第\s*\d+\s*章\s*", "", t).strip()
            return t or f"第 {num} 章"
    return f"第 {num} 章"


def reading_time(text):
    # approximate: count CJK chars + latin words
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    minutes = cjk / 400.0 + words / 200.0
    total = max(20, int(round(minutes * 60)))
    mm, ss = divmod(total, 60)
    if mm == 0:
        return f"{ss}秒"
    return f"{mm}分{ss}秒"


def build_renderer():
    md = MarkdownIt("commonmark", {"html": True, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    return md


def render_chapter(md, text):
    """Render markdown to html and collect h2/h3 toc with anchors."""
    tokens = md.parse(text)
    toc = []
    used = {}
    # assign ids to headings
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.tag in ("h2", "h3"):
            inline = tokens[i + 1]
            title = inline.content
            base = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title).strip("-").lower() or "sec"
            idx = used.get(base, 0)
            used[base] = idx + 1
            hid = base if idx == 0 else f"{base}-{idx}"
            tok.attrSet("id", hid)
            toc.append({"id": hid, "text": title, "level": int(tok.tag[1])})
    body = md.renderer.render(tokens, md.options, {})
    return body, toc


PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{page_title} - {book_title}</title>
<link rel="stylesheet" href="assets/style.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css">
</head>
<body>
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="index.html">
      <svg class="brand-logo" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><path fill="#1e80ff" d="M12 2.2 22 10l-2.9 2.3L12 6.6 4.9 12.3 2 10z"/><path fill="#1e80ff" d="M7.1 12.9 12 16.9l4.9-4 2.9 2.3L12 21.8 2.2 15.2z"/></svg>
      <span class="brand-name">稀土掘金</span>
      <span class="brand-badge">课程</span>
    </a>
    <div class="topbar-title">{book_title}</div>
    <div class="topbar-right">
      <a class="login-btn" href="{repo}" target="_blank" rel="noopener">项目源码</a>
    </div>
  </div>
</header>

<div class="layout">
  <aside class="sidebar">
    <div class="buy-card">
      <div class="buy-tag">限时特惠</div>
      <div class="buy-price"><span class="cur">¥</span>34.95 <button class="buy-btn">立即学习</button></div>
      <div class="buy-origin">原价 ¥69.9</div>
    </div>
    <nav class="chapter-list">
      <ol>
        {chapter_items}
      </ol>
    </nav>
  </aside>

  <main class="content">
    <article class="markdown-body">
      {body}
    </article>
    <div class="pager">
      {prev_link}
      {next_link}
    </div>
  </main>

  <aside class="toc">
    <div class="toc-head"><span>目录</span><span class="toc-toggle">≫</span></div>
    <ul>{toc_items}</ul>
  </aside>
</div>

<script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/common.min.js"></script>
<script>hljs.highlightAll();</script>
<script src="assets/app.js"></script>
</body>
</html>
"""


def make_chapter_items(chapters, active_id):
    items = []
    for c in chapters:
        cls = " class=\"active\"" if c["id"] == active_id else ""
        items.append(
            f'<li{cls}><a href="{c["id"]}.html">'
            f'<span class="num">{c["num"]}</span>'
            f'<span class="info"><span class="t">{html.escape(c["title"])}</span>'
            f'<span class="d">学习时长: {c["time"]}</span></span>'
            f'</a></li>'
        )
    return "\n        ".join(items)


def main():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(os.path.join(OUT, "assets"), exist_ok=True)
    web = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
    for asset in ("style.css", "app.js"):
        shutil.copy(os.path.join(web, asset), os.path.join(OUT, "assets", asset))
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    md = build_renderer()
    chapters = read_chapters()
    for c in chapters:
        c["id"] = f"ch-{c['num']}"
        c["title"] = chapter_title(c["text"], c["num"])
        c["time"] = reading_time(c["text"])
        c["html"], c["toc"] = render_chapter(md, c["text"])

    for i, c in enumerate(chapters):
        toc_items = "\n      ".join(
            f'<li class="lv{t["level"]}"><a href="#{t["id"]}">{html.escape(t["text"])}</a></li>'
            for t in c["toc"]
        ) or '<li class="muted">—</li>'
        prev_link = next_link = ""
        if i > 0:
            p = chapters[i - 1]
            prev_link = f'<a class="prev" href="{p["id"]}.html">← {html.escape(p["title"])}</a>'
        if i < len(chapters) - 1:
            n = chapters[i + 1]
            next_link = f'<a class="next" href="{n["id"]}.html">{html.escape(n["title"])} →</a>'
        page = PAGE.format(
            page_title=html.escape(c["title"]),
            book_title=html.escape(BOOK_TITLE),
            repo=REPO,
            chapter_items=make_chapter_items(chapters, c["id"]),
            body=c["html"],
            toc_items=toc_items,
            prev_link=prev_link,
            next_link=next_link,
        )
        with open(os.path.join(OUT, f'{c["id"]}.html'), "w", encoding="utf-8") as f:
            f.write(page)

    # index redirects to first chapter but is also a landing overview
    first = chapters[0]
    chapter_items = make_chapter_items(chapters, None)
    cards = "\n      ".join(
        f'<a class="card" href="{c["id"]}.html"><span class="card-num">{c["num"]:02d}</span>'
        f'<span class="card-t">{html.escape(c["title"])}</span>'
        f'<span class="card-d">{c["time"]}</span></a>'
        for c in chapters
    )
    index = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(BOOK_TITLE)}</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body class="landing-body">
<header class="topbar">
  <div class="topbar-inner">
    <a class="brand" href="index.html">
      <svg class="brand-logo" viewBox="0 0 24 24" width="24" height="24" aria-hidden="true"><path fill="#1e80ff" d="M12 2.2 22 10l-2.9 2.3L12 6.6 4.9 12.3 2 10z"/><path fill="#1e80ff" d="M7.1 12.9 12 16.9l4.9-4 2.9 2.3L12 21.8 2.2 15.2z"/></svg>
      <span class="brand-name">稀土掘金</span>
      <span class="brand-badge">课程</span>
    </a>
    <div class="topbar-title">{html.escape(BOOK_TITLE)}</div>
    <div class="topbar-right">
      <a class="login-btn" href="{REPO}" target="_blank" rel="noopener">项目源码</a>
    </div>
  </div>
</header>
<div class="landing">
  <h1>{html.escape(BOOK_TITLE)}</h1>
  <p class="sub">{html.escape(BOOK_SUBTITLE)}</p>
  <p class="desc">共 {len(chapters)} 章 · 从最小可运行的 Agent 出发，逐层拆解一个生产级 AI 编程助手的完整实现。</p>
  <a class="start-btn" href="{first['id']}.html">开始学习</a>
  <div class="cards">
      {cards}
  </div>
</div>
</body>
</html>
"""
    with open(os.path.join(OUT, "index.html"), "w", encoding="utf-8") as f:
        f.write(index)

    print(f"Generated {len(chapters)} chapters into {OUT}")


if __name__ == "__main__":
    main()
