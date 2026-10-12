"""A tiny, dependency-free markdown -> HTML for coach reports.

Handles the subset coach reports use: ATX headers, unordered (-, *) and
ordered (1.) lists, **bold**, `code` and paragraphs. Every text run is escaped
BEFORE any tag is inserted, so a player name or a stray '<' in a report cannot
break out into the page. Links are not markdown links at all unless the
caller whitelists one exact shape (`link`): anything else stays literal text,
so no href a model writes can reach the page.
"""

import re

from .html import esc


def _inline(text, link):
    out = esc(text)  # escape FIRST -> injection-safe
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    if link is not None:
        pattern, template = link
        out = pattern.sub(template, out)
    return out


def markdown_to_html(md, link=None):
    """`link` is an optional (compiled regex over the ESCAPED text, replacement
    template) pair for the one link shape a page allows."""
    parts, para, cur = [], [], [None]

    def flush_para():
        if para:
            parts.append("<p>" + _inline(" ".join(para), link) + "</p>")
            para.clear()

    def close_list():
        if cur[0]:
            parts.append(f"</{cur[0]}>")
            cur[0] = None

    def open_list(kind):
        if cur[0] != kind:
            close_list()
            parts.append(f"<{kind}>")
            cur[0] = kind

    for raw in (md or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush_para()
            close_list()
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            flush_para()
            close_list()
            level = len(h.group(1))
            tag = "h3" if level <= 1 else "h4" if level == 2 else "h5"
            parts.append(f"<{tag}>{_inline(h.group(2), link)}</{tag}>")
            continue
        ul = re.match(r"^[-*]\s+(.*)$", line)
        if ul:
            flush_para()
            open_list("ul")
            parts.append(f"<li>{_inline(ul.group(1), link)}</li>")
            continue
        ol = re.match(r"^\d+\.\s+(.*)$", line)
        if ol:
            flush_para()
            open_list("ol")
            parts.append(f"<li>{_inline(ol.group(1), link)}</li>")
            continue
        close_list()
        para.append(line)
    flush_para()
    close_list()
    return "".join(parts)
