"""Convert between Atlassian Document Format (ADF) and Markdown.

ADF is the JSON tree Jira's REST API v3 uses for descriptions and comments.
Markdown is what the rest of the service reads and writes.

The conversion is lossy where Markdown has no equivalent: panels become
block quotes, underline and colour are dropped, attachments become a
placeholder, and a task list comes back from Markdown as a bullet list.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

Node = dict[str, Any]

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


# --------------------------------------------------------------------------
# ADF -> Markdown
# --------------------------------------------------------------------------

def adf_to_markdown(doc: Node | None) -> str:
    """Render an ADF document (or any ADF block node) as Markdown."""
    if not doc:
        return ""
    return _blocks((doc.get("content") or []) if doc.get("type") == "doc" else [doc])


def _blocks(nodes: list[Node]) -> str:
    return "\n\n".join(filter(None, (_block(n) for n in nodes)))


def _block(node: Node) -> str:
    kind = node.get("type")
    content = node.get("content") or []
    attrs = node.get("attrs") or {}

    if kind == "paragraph":
        return _inline(content)
    if kind == "heading":
        level = min(max(int(attrs.get("level", 1)), 1), 6)
        return f"{'#' * level} {_inline(content, breaks=' ')}"
    if kind == "bulletList":
        return "\n".join(_list_item("- ", item) for item in content)
    if kind == "orderedList":
        start = int(attrs.get("order", 1))
        return "\n".join(_list_item(f"{start + i}. ", item) for i, item in enumerate(content))
    if kind in ("taskList", "decisionList"):
        return _task_list(content)
    if kind == "codeBlock":
        text = "".join(n.get("text", "") for n in content)
        fence = "`" * max(3, _longest_run(text, "`") + 1)
        return f"{fence}{attrs.get('language') or ''}\n{text}\n{fence}"
    if kind in ("blockquote", "panel"):
        return _indent(_blocks(content), "> ", "> ")
    if kind == "rule":
        return "---"
    if kind == "table":
        return _table(content)
    if kind in ("expand", "nestedExpand"):
        title = _escape(attrs.get("title") or "", line_start=False)
        return "\n\n".join(filter(None, [f"**{title}**" if title else "", _blocks(content)]))
    if kind in ("mediaSingle", "mediaGroup"):
        return "\n".join(filter(None, (_block(n) for n in content)))
    if kind == "media":
        alt = _escape(attrs.get("alt") or "", line_start=False)
        if attrs.get("type") == "external" and attrs.get("url"):
            return f"![{alt}]({_href(attrs['url'])})"
        return f"\\[attachment: {alt}\\]" if alt else "\\[attachment\\]"
    if kind in ("blockCard", "embedCard"):
        return f"<{attrs['url']}>" if attrs.get("url") else ""
    if content:
        # Unknown container: keep whatever it holds.
        if all(_is_inline(n) for n in content):
            return _inline(content)
        return _blocks(content)
    if _is_inline(node):
        return _inline([node])
    return ""


def _list_item(marker: str, item: Node) -> str:
    parts: list[str] = []
    for child in item.get("content") or []:
        text = _block(child)
        if not text:
            continue
        # A bullet list may follow its paragraph directly; anything else
        # needs a blank line to stay a separate block when parsed back.
        tight = parts and child.get("type") in ("bulletList", "taskList")
        parts.append(("\n" if tight else "\n\n" if parts else "") + text)
    return _indent("".join(parts), marker, " " * len(marker))


def _task_list(items: list[Node]) -> str:
    lines: list[str] = []
    for item in items:
        if item.get("type") in ("taskList", "decisionList"):
            lines.append(_indent(_task_list(item.get("content") or []), "  ", "  "))
            continue
        attrs = item.get("attrs") or {}
        content = item.get("content") or []
        if item.get("type") == "decisionItem":
            marker = "- "
        else:
            marker = "- [x] " if attrs.get("state") == "DONE" else "- [ ] "
        body = _inline(content) if all(_is_inline(n) for n in content) else _blocks(content)
        lines.append(_indent(body, marker, "  "))
    return "\n".join(lines)


def _table(rows: list[Node]) -> str:
    grid = [
        [_cell(cell) for cell in row.get("content") or []]
        for row in rows
        if row.get("type") == "tableRow"
    ]
    grid = [row for row in grid if row]
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]
    lines = ["| " + " | ".join(row) + " |" for row in grid]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _cell(cell: Node) -> str:
    parts = []
    for child in cell.get("content") or []:
        if child.get("type") in ("paragraph", "heading"):
            parts.append(_inline(child.get("content") or [], breaks="<br>"))
        else:
            parts.append(_block(child).replace("\n", "<br>"))
    return "<br>".join(filter(None, parts)).replace("|", "\\|")


def _indent(text: str, first: str, rest: str) -> str:
    """Prefix the first line with `first` and every later line with `rest`."""
    lines = text.split("\n")
    return "\n".join(
        ((rest if i else first) + line).rstrip() for i, line in enumerate(lines)
    )


_INLINE_TYPES = {
    "text", "hardBreak", "mention", "emoji", "inlineCard", "date", "status",
    "mediaInline", "placeholder", "inlineExtension",
}
_MARK_ORDER = ("link", "strong", "em", "strike")
_OPEN = {"strong": "**", "em": "*", "strike": "~~"}


def _is_inline(node: Node) -> bool:
    return node.get("type") in _INLINE_TYPES


def _inline(nodes: list[Node], breaks: str = "\\\n") -> str:
    """Render inline nodes. `breaks` is what a hard break becomes."""
    out: list[str] = []
    stack: list[tuple[str, str]] = []  # open marks as (type, href)
    pending_space = ""
    line_start = True

    def close_to(depth: int) -> None:
        while len(stack) > depth:
            kind, href = stack.pop()
            out.append(f"]({_href(href)})" if kind == "link" else _OPEN[kind])

    def emit(text: str, marks: list[tuple[str, str]]) -> None:
        """Write non-blank `text`, switching the open marks to `marks`."""
        nonlocal pending_space, line_start
        keep = 0
        while keep < len(stack) and keep < len(marks) and stack[keep] == marks[keep]:
            keep += 1
        close_to(keep)
        if not line_start:
            out.append(pending_space)
        pending_space = ""
        for mark in marks[keep:]:
            stack.append(mark)
            out.append("[" if mark[0] == "link" else _OPEN[mark[0]])
        out.append(text)
        line_start = False

    def hard_break() -> None:
        nonlocal pending_space, line_start
        pending_space = ""
        if out:  # a break before any text means nothing
            emit(breaks, [])
            line_start = breaks.endswith("\n")

    for node in nodes:
        kind = node.get("type")
        attrs = node.get("attrs") or {}
        if kind == "text":
            raw = node.get("text", "")
            marks = {m.get("type"): (m.get("attrs") or {}) for m in node.get("marks") or []}
            wanted = [
                (name, marks[name].get("href", "") if name == "link" else "")
                for name in _MARK_ORDER
                if name in marks
            ]
            for i, piece in enumerate(raw.split("\n")):
                if i:
                    hard_break()
                core = piece.strip()
                if not core:
                    pending_space += piece
                    continue
                lead = piece[: len(piece) - len(piece.lstrip())]
                trail = piece[len(piece.rstrip()):]
                pending_space += lead
                href = marks.get("link", {}).get("href")
                if "code" in marks:
                    emit(_code_span(core), [m for m in wanted if m[0] == "link"])
                elif href and href == core and len(wanted) == 1 and _AUTOLINK.fullmatch(core):
                    emit(f"<{core}>", [])
                else:
                    emit(_escape(core, line_start), wanted)
                pending_space = trail
        elif kind == "hardBreak":
            hard_break()
        elif kind == "mention":
            name = attrs.get("text") or f"@{attrs.get('id', 'unknown')}"
            emit(_escape(name, line_start), [])
        elif kind == "emoji":
            symbol = attrs.get("text") or attrs.get("shortName")
            if symbol:
                emit(_escape(symbol, line_start), [])
        elif kind == "inlineCard":
            if attrs.get("url"):
                emit(f"<{attrs['url']}>", [])
        elif kind == "date":
            try:
                stamp = datetime.fromtimestamp(int(attrs["timestamp"]) / 1000, timezone.utc)
                emit(stamp.strftime("%Y-%m-%d"), [])
            except (KeyError, ValueError, OverflowError, OSError):
                pass
        elif kind == "status":
            if attrs.get("text"):
                emit(_escape(f"[{attrs['text']}]", line_start), [])
        elif kind == "mediaInline":
            emit("\\[attachment\\]", [])
        elif attrs.get("text"):
            emit(_escape(str(attrs["text"]), line_start), [])

    close_to(0)
    while out and out[-1] == breaks:  # a break at the end of a block means nothing
        out.pop()
    return "".join(out).strip()


_AUTOLINK = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*")
_ALWAYS = re.compile(r"[\\`*\[\]]")
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")
_HTML_OPEN = re.compile(r"<(?=[A-Za-z/!?])")
_ENTITY = re.compile(r"&(?=#?[A-Za-z0-9]+;)")
_TILDES = re.compile(r"~(?=~)|(?<=~)~")
_LINE_START = re.compile(
    r"(?P<mark>#{1,6}(?=\s|$)|>|[-+](?=\s|$)|[-=]+\s*$)"
    r"|(?P<num>\d{1,9})(?P<dot>[.)])(?=\s|$)"
)


def _escape(text: str, line_start: bool) -> str:
    """Backslash-escape `text` so Markdown reads it as plain text."""
    text = _ALWAYS.sub(lambda m: "\\" + m.group(), text)
    text = _UNDERSCORE.sub(r"\\_", text)
    text = _HTML_OPEN.sub(r"\\<", text)
    text = _ENTITY.sub(r"\\&", text)
    text = _TILDES.sub(r"\\~", text)
    if line_start:
        match = _LINE_START.match(text)
        if match and match.group("mark"):
            text = "\\" + text
        elif match:
            text = f"{match.group('num')}\\{text[match.end('num'):]}"
    return text


def _code_span(text: str) -> str:
    fence = "`" * (_longest_run(text, "`") + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _longest_run(text: str, char: str) -> int:
    return max((len(m.group()) for m in re.finditer(re.escape(char) + "+", text)), default=0)


def _href(url: str) -> str:
    url = url.replace(" ", "%20")
    return f"<{url}>" if "(" in url or ")" in url else url


def adf_to_text(node: Node | None) -> str:
    """Plain text of an ADF node, with no Markdown syntax.

    A link whose text differs from its target is written as "text (target)".
    """
    if not node:
        return ""
    kind = node.get("type")
    attrs = node.get("attrs") or {}
    if kind == "text":
        text = node.get("text", "")
        for mark in node.get("marks") or []:
            href = (mark.get("attrs") or {}).get("href")
            if mark.get("type") == "link" and href and href != text:
                return f"{text} ({href})"
        return text
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji", "status"):
        return attrs.get("text") or attrs.get("shortName") or ""
    if kind in ("inlineCard", "blockCard", "embedCard"):
        return attrs.get("url") or ""
    children = node.get("content") or []
    separator = "" if all(_is_inline(c) for c in children) else "\n"
    return separator.join(filter(None, (adf_to_text(c) for c in children)))


# --------------------------------------------------------------------------
# Markdown -> ADF
# --------------------------------------------------------------------------

def markdown_to_adf(markdown: str) -> Node:
    """Parse Markdown into an ADF document."""
    tree = SyntaxTreeNode(_MD.parse(markdown or ""))
    return {"version": 1, "type": "doc", "content": _to_blocks(tree.children, top=True)}


def markdown_to_text(markdown: str) -> str:
    """Strip Markdown syntax, leaving plain text."""
    return adf_to_text(markdown_to_adf(markdown)).strip()


def _to_blocks(nodes: list[SyntaxTreeNode], top: bool) -> list[Node]:
    """Convert block nodes.

    ADF only allows headings, quotes, rules and tables at the top level, so
    inside a list item or a quote (`top` false) they are flattened.
    """
    out: list[Node] = []
    for node in nodes:
        kind = node.type
        if kind == "paragraph":
            content = _to_inline(node.children[0].children if node.children else [])
            if content:
                out.append({"type": "paragraph", "content": content})
        elif kind == "heading":
            inline = node.children[0].children if node.children else []
            if top:
                out.append({
                    "type": "heading",
                    "attrs": {"level": int(node.tag[1])},
                    "content": _to_inline(inline),
                })
            elif inline:
                out.append({"type": "paragraph", "content": _to_inline(inline, ("strong",))})
        elif kind in ("bullet_list", "ordered_list"):
            adf: Node = {"type": "bulletList" if kind == "bullet_list" else "orderedList"}
            if kind == "ordered_list" and node.attrs.get("start") is not None:
                adf["attrs"] = {"order": int(node.attrs["start"])}
            adf["content"] = [_to_list_item(item) for item in node.children]
            out.append(adf)
        elif kind in ("fence", "code_block"):
            block: Node = {"type": "codeBlock"}
            language = node.info.strip().split()[0] if node.info.strip() else ""
            if language:
                block["attrs"] = {"language": language}
            text = node.content[:-1] if node.content.endswith("\n") else node.content
            if text:
                block["content"] = [{"type": "text", "text": text}]
            out.append(block)
        elif kind == "blockquote":
            inner = _to_blocks(node.children, top=False)
            if top and inner:
                out.append({"type": "blockquote", "content": inner})
            else:
                out.extend(inner)
        elif kind == "hr":
            if top:
                out.append({"type": "rule"})
        elif kind == "table":
            rows = [row for section in node.children for row in section.children]
            if top:
                out.append({"type": "table", "content": [_to_row(row) for row in rows]})
            else:
                for row in rows:
                    cells = [_to_inline(c.children[0].children if c.children else []) for c in row.children]
                    line: list[Node] = []
                    for cell in filter(None, cells):
                        line += ([{"type": "text", "text": " | "}] if line else []) + cell
                    if line:
                        out.append({"type": "paragraph", "content": _merge(line)})
        elif kind == "html_block":
            text = node.content.strip()
            if text:
                out.append({"type": "paragraph", "content": [{"type": "text", "text": text}]})
    return out


def _to_list_item(item: SyntaxTreeNode) -> Node:
    content = _to_blocks(item.children, top=False)
    # ADF requires a list item to open with a paragraph or a code block.
    if not content or content[0]["type"] not in ("paragraph", "codeBlock"):
        content.insert(0, {"type": "paragraph", "content": []})
    return {"type": "listItem", "content": content}


def _to_row(row: SyntaxTreeNode) -> Node:
    cells = []
    for cell in row.children:
        inline = _to_inline(cell.children[0].children if cell.children else [])
        cells.append({
            "type": "tableHeader" if cell.type == "th" else "tableCell",
            "content": [{"type": "paragraph", "content": inline}],
        })
    return {"type": "tableRow", "content": cells}


_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BARE_URL = re.compile(r"https?://[^\s<>]+")


def _to_inline(nodes: list[SyntaxTreeNode], marks: tuple[Any, ...] = ()) -> list[Node]:
    return _merge(_inline_nodes(nodes, marks))


def _inline_nodes(nodes: list[SyntaxTreeNode], marks: tuple[Any, ...]) -> list[Node]:
    out: list[Node] = []
    for node in nodes:
        kind = node.type
        if kind == "text":
            out += _linkified(node.content, marks)
        elif kind == "code_inline":
            # ADF lets `code` combine with a link and nothing else.
            links = tuple(m for m in marks if isinstance(m, tuple))
            out.append(_text(node.content, links + ("code",)))
        elif kind == "softbreak":
            out.append(_text(" ", marks))
        elif kind == "hardbreak":
            out.append({"type": "hardBreak"})
        elif kind in ("strong", "em"):
            out += _inline_nodes(node.children, _with(marks, kind))
        elif kind == "s":
            out += _inline_nodes(node.children, _with(marks, "strike"))
        elif kind == "link":
            href = node.attrs.get("href", "")
            has_link = any(isinstance(m, tuple) for m in marks)
            out += _inline_nodes(node.children, marks if has_link or not href else marks + (("link", href),))
        elif kind == "image":
            src = node.attrs.get("src", "")
            label = node.content or src
            if label:
                out.append(_text(label, _with(marks, ("link", src)) if src else marks))
        elif kind == "html_inline":
            out.append({"type": "hardBreak"} if _BR.fullmatch(node.content) else _text(node.content, marks))
        elif node.content:
            out.append(_text(node.content, marks))
    return [n for n in out if n.get("type") != "text" or n["text"]]


def _with(marks: tuple[Any, ...], mark: Any) -> tuple[Any, ...]:
    return marks if mark in marks else marks + (mark,)


def _text(text: str, marks: tuple[Any, ...]) -> Node:
    node: Node = {"type": "text", "text": text}
    if marks:
        node["marks"] = [
            {"type": "link", "attrs": {"href": m[1]}} if isinstance(m, tuple) else {"type": m}
            for m in marks
        ]
    return node


def _linkified(text: str, marks: tuple[Any, ...]) -> list[Node]:
    """Split `text` so bare URLs carry a link mark, as Jira does not auto-link."""
    if any(isinstance(m, tuple) for m in marks):
        return [_text(text, marks)]
    out: list[Node] = []
    position = 0
    for match in _BARE_URL.finditer(text):
        url = match.group().rstrip(".,;:!?\"'")
        if url.endswith(")") and "(" not in url:
            url = url.rstrip(")")
        end = match.start() + len(url)
        out.append(_text(text[position:match.start()], marks))
        out.append(_text(url, marks + (("link", url),)))
        position = end
    out.append(_text(text[position:], marks))
    return out


def _merge(nodes: list[Node]) -> list[Node]:
    """Join neighbouring text nodes that carry the same marks."""
    out: list[Node] = []
    for node in nodes:
        previous = out[-1] if out else None
        if (
            previous
            and node.get("type") == "text"
            and previous.get("type") == "text"
            and previous.get("marks") == node.get("marks")
        ):
            previous["text"] += node["text"]
        else:
            out.append(dict(node))
    return out
