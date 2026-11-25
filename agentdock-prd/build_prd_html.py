#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 PRD-AgentDock.md 生成为 agentdock-prd.html。

设计意图：**MD 是唯一事实源**。HTML 不再手工维护，避免"文档不同步"这类缺陷再次出现。
用法：  python build_prd_html.py
"""

import io
import os
import re
import html

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MD = os.path.join(ROOT, "PRD-AgentDock.md")
HEAD = os.path.join(HERE, "_tpl", "head.html")
OUT = os.path.join(HERE, "agentdock-prd.html")

VOID_OK = True


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(s: str) -> str:
    """行内元素：先保护行内代码，再处理粗体/斜体/链接。"""
    codes = []

    def keep(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    s = re.sub(r"`([^`]+)`", keep, s)
    s = esc(s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)

    def back(m):
        return "<code>%s</code>" % esc(codes[int(m.group(1))])

    return re.sub(r"\x00(\d+)\x00", back, s)


def cell(txt: str) -> str:
    t = txt.strip()
    if t in ("P0", "P1", "P2"):
        return '<span class="pill p%s">%s</span>' % (t[1], t)
    return inline(t)


def split_row(line: str):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c for c in line.split("|")]


def render_table(rows):
    head = rows[0]
    sep = "".join(rows[1]) if len(rows) > 1 else ""
    body = rows[2:] if len(rows) > 1 and re.match(r"^[\s:|-]+$", sep or "-") else rows[1:]
    out = ['<div class="table-wrap">', "<table>", "<thead><tr>"]
    for c in head:
        out.append("<th>%s</th>" % cell(c))
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>")
        for c in r:
            out.append("<td>%s</td>" % cell(c))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_list(items, ordered):
    tag = "ol" if ordered else "ul"
    out = ["<%s>" % tag]
    for it in items:
        out.append("<li>%s</li>" % inline(it))
    out.append("</%s>" % tag)
    return "".join(out)


def slug(num: str) -> str:
    return "s" + num.lower().replace(".", "-")


def main():
    md = io.open(MD, encoding="utf-8").read()
    lines = md.split("\n")

    # ---------- 标题与元信息 ----------
    title = ""
    meta_rows = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("# "):
            title = lines[i][2:].strip()
            i += 1
            break
        i += 1

    # 第一张表 = 封面元信息
    while i < len(lines) and not lines[i].strip().startswith("|"):
        i += 1
    if i < len(lines):
        rows = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            rows.append(split_row(lines[i]))
            i += 1
        meta_rows = rows[2:]

    # ---------- 正文解析 ----------
    body = []
    toc = []
    sec_no = 0
    in_section = False
    cur_id = ""
    buf = []           # 段落缓冲
    listbuf = []       # 列表缓冲
    list_ordered = False
    quote = []         # 引用缓冲
    table = []         # 表格缓冲
    code = []          # 代码缓冲
    code_lang = ""
    in_code = False
    skip_toc = False

    def flush_para():
        nonlocal buf
        if buf:
            txt = " ".join(x.strip() for x in buf)
            body.append("<p>%s</p>" % inline(txt))
            buf = []

    def flush_list():
        nonlocal listbuf
        if listbuf:
            body.append(render_list(listbuf, list_ordered))
            listbuf = []

    def flush_quote():
        nonlocal quote
        if quote:
            joined = " ".join(quote)
            cls = "callout"
            if "注意" in joined[:30] or "禁止" in joined[:30]:
                cls = "callout warn"
            inner = []
            ol_buf = []

            def flush_ol():
                if ol_buf:
                    inner.append(
                        "<ol>" + "".join("<li>%s</li>" % inline(x) for x in ol_buf) + "</ol>"
                    )
                    ol_buf.clear()

            for q in quote:
                m = re.match(r"^\d+\.\s+(.*)$", q)
                if m:
                    ol_buf.append(m.group(1))
                else:
                    flush_ol()
                    inner.append("<p>%s</p>" % inline(q))
            flush_ol()
            body.append('<div class="%s">%s</div>' % (cls, "".join(inner)))
            quote = []

    def flush_table():
        nonlocal table
        if table:
            body.append(render_table(table))
            table = []

    def flush_all():
        flush_para()
        flush_list()
        flush_quote()
        flush_table()

    while i < len(lines):
        ln = lines[i]
        s = ln.rstrip()

        # 代码块
        if s.strip().startswith("```"):
            if not in_code:
                flush_all()
                in_code = True
                code_lang = s.strip()[3:].strip()
                code = []
            else:
                in_code = False
                raw = "\n".join(code)
                if code_lang == "mermaid":
                    body.append(
                        '<figure class="diagram"><pre class="mermaid">%s</pre></figure>'
                        % esc(raw)
                    )
                else:
                    body.append('<pre class="codeblk">%s</pre>' % esc(raw))
            i += 1
            continue
        if in_code:
            code.append(ln)
            i += 1
            continue

        # 分隔线
        if s.strip() in ("---", "***"):
            flush_all()
            i += 1
            continue

        # 空行
        if not s.strip():
            flush_para()
            flush_list()
            flush_quote()
            flush_table()
            i += 1
            continue

        # 表格
        if s.strip().startswith("|"):
            flush_para()
            flush_list()
            flush_quote()
            table.append(split_row(s))
            i += 1
            continue
        else:
            flush_table()

        # 引用
        if s.startswith(">"):
            flush_para()
            flush_list()
            quote.append(s.lstrip("> ").strip())
            i += 1
            continue
        else:
            flush_quote()

        # 标题
        m = re.match(r"^(#{2,4})\s+(.*)$", s)
        if m:
            flush_all()
            level = len(m.group(1))
            text = m.group(2).strip()

            if level == 2:
                if text.startswith("目录"):
                    skip_toc = True
                    i += 1
                    continue
                skip_toc = False
                if in_section:
                    body.append("</section>")
                sec_no += 1
                m2 = re.match(r"^(\d+)\.\s*(.+)$", text)
                if m2:
                    label = "%02d" % int(m2.group(1))
                    name = m2.group(2)
                else:
                    m3 = re.match(r"^附录\s*([A-Z])[：:]\s*(.+)$", text)
                    if m3:
                        label = m3.group(1)
                        name = "附录 " + m3.group(1) + " · " + m3.group(2)
                    else:
                        label = "%02d" % sec_no
                        name = text
                cur_id = slug(label if not label.isalpha() else "app" + label)
                toc.append((cur_id, label, name))
                body.append('<section id="%s">' % cur_id)
                body.append('<h2><span class="num">%s</span>%s</h2>' % (label, esc(name)))
                in_section = True
                i += 1
                continue
            if skip_toc:
                i += 1
                continue
            if level == 3:
                body.append("<h3>%s</h3>" % inline(text))
                i += 1
                continue
            if level == 4:
                body.append("<h4>%s</h4>" % inline(text))
                i += 1
                continue

        if skip_toc:
            i += 1
            continue

        # 列表
        m = re.match(r"^\s*[-*]\s+(.*)$", s)
        if m:
            flush_para()
            if listbuf and list_ordered:
                flush_list()
            list_ordered = False
            listbuf.append(m.group(1))
            i += 1
            continue
        m = re.match(r"^\s*\d+\.\s+(.*)$", s)
        if m:
            flush_para()
            if listbuf and not list_ordered:
                flush_list()
            list_ordered = True
            listbuf.append(m.group(1))
            i += 1
            continue

        # 普通段落
        flush_list()
        buf.append(s)
        i += 1

    flush_all()
    if in_section:
        body.append("</section>")

    # ---------- 封面 ----------
    parts = title.split(" · ")
    h1 = parts[1] if len(parts) > 1 else title

    # 版本号从元信息表取，避免封面与正文漂移
    doc_ver = ""
    for r in meta_rows:
        if len(r) >= 2 and r[0].strip() == "文档版本":
            doc_ver = r[1].strip()
            break
    ver_tag = " · " + doc_ver if doc_ver else ""

    cover = ['<header class="cover">']
    cover.append('<div class="eyebrow">AgentDock · 产品需求文档%s</div>' % esc(ver_tag))
    cover.append("<h1>%s</h1>" % esc(h1))
    cover.append(
        '<p class="subtitle">一个对话入口 · 四个业务域 · 六个 Agent（18 个 skill）· 每个事实只有一个权威源。'
        "用户只看到一个窗口，系统按域委托、按权威裁决、按依赖顺序作答。</p>"
    )
    cover.append('<div class="meta-grid">')
    for r in meta_rows:
        if len(r) < 2:
            continue
        cover.append(
            '<div class="meta-item"><div class="label">%s</div><div class="value">%s</div></div>'
            % (inline(r[0].strip()), inline(r[1].strip()))
        )
    cover.append("</div></header>")

    # ---------- 目录 ----------
    nav = ['<nav class="toc"><h2>目录</h2><ol>']
    for cid, label, name in toc:
        if label.isalpha():
            nav.append('<li class="app"><a href="#%s" data-n="%s">%s</a></li>' % (cid, label, esc(name)))
        else:
            nav.append('<li><a href="#%s">%s</a></li>' % (cid, esc(name)))
    nav.append("</ol></nav>")

    extra_css = (
        "<style>\n"
        "      nav.toc li.app a::before { content: attr(data-n); }\n"
        "      main section > h2 .num { flex: 0 0 auto; }\n"
        "    </style>"
    )
    fallback = (
        "<script>\n"
        "  window.addEventListener('load', function () {\n"
        "    if (!window.mermaid || !mermaid.run) return;\n"
        "    try { mermaid.run({ querySelector: 'pre.mermaid' }); } catch (e) {}\n"
        "  });\n"
        "</script>"
    )

    head = io.open(HEAD, encoding="utf-8").read()
    doc = "\n".join(
        [
            head,
            extra_css,
            "</head>",
            "<body>",
            '  <article class="page">',
            "    " + "\n    ".join(cover),
            "    " + "\n    ".join(nav),
            "    <main>",
            "\n".join(body),
            "    </main>",
            '    <footer><div class="foot-note">AgentDock · 企业级 A2A 商家经营智能体中台 · PRD %s</div>'
            '<div class="foot-note">本文件由 build_prd_html.py 从 PRD-AgentDock.md 自动生成，请勿手工编辑。</div></footer>'
            % (doc_ver or "v2.1"),
            "  </article>",
            '  <script src="./_shared/js/echarts.min.js"></script>',
            '  <script src="./_shared/js/mermaid.min.js"></script>',
            '  <script src="./assets/charts.js"></script>',
            fallback,
            "</body>",
            "</html>",
        ]
    )

    io.open(OUT, "w", encoding="utf-8").write(doc)
    print("written:", OUT)
    print("sections:", len(toc), " lines:", doc.count("\n") + 1)


if __name__ == "__main__":
    main()
