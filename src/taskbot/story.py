"""Split a ticket description, as Markdown, into the four story sections.

The description follows templates/story.md: the headings Story, Acceptance
criteria, Out of scope and Pointers, at any heading level. A line holding
only the heading in bold counts as a heading too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from markdown_it import MarkdownIt

from .adf import markdown_to_text

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])

_HEADINGS = {
    "story": "story",
    "acceptance criteria": "acceptance_criteria",
    "out of scope": "out_of_scope",
    "pointers": "pointers",
}
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])(?:\s+|$)")
_CHECKBOX = re.compile(r"^\\?\[[ xX]\\?\]\s+")
_EMPTY_ITEMS = {"", "-", "none", "none.", "n/a", "na"}


@dataclass
class Story:
    story: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    pointers: list[str] = field(default_factory=list)


def parse_story(markdown: str) -> Story:
    """Parse a description into its sections.

    Text before the first known heading belongs to the story. The story is
    kept as Markdown. Each list item, or each paragraph where a section has
    no list, becomes one entry: Markdown for acceptance criteria and out of
    scope, plain text for pointers so paths and URLs can be used as they are.
    """
    lines = markdown.splitlines()
    tokens = _MD.parse(markdown)
    blocks: dict[str, list[tuple[str, int, int]]] = {key: [] for key in _HEADINGS.values()}
    section = "story"

    for i, token in enumerate(tokens):
        if token.level != 0 or token.map is None or token.nesting == -1:
            continue
        start, end = token.map
        heading = _section_heading(tokens, i)
        if heading:
            section = heading
        elif section != "story" and token.type in ("bullet_list_open", "ordered_list_open"):
            blocks[section] += [
                ("item", *item.map)
                for item in tokens[i:]
                if item.type == "list_item_open" and item.level == 1 and item.map
                and start <= item.map[0] < end
            ]
        else:
            kind = "paragraph" if token.type == "paragraph_open" else "block"
            blocks[section].append((kind, start, end))

    result = Story()
    story_blocks = blocks["story"]
    if story_blocks:
        result.story = "\n\n".join(
            "\n".join(lines[start:end]).strip() for _, start, end in story_blocks
        ).strip()
    for key in ("acceptance_criteria", "out_of_scope", "pointers"):
        entries: list[str] = []
        for kind, start, end in blocks[key]:
            if kind == "item":
                texts = [_item_text(lines[start:end])]
            elif kind == "paragraph":
                texts = _paragraph_entries(lines[start:end])
            else:
                texts = ["\n".join(lines[start:end]).strip().lstrip("#").strip()]
            for text in texts:
                if key == "pointers":
                    text = markdown_to_text(text)
                if text.casefold() not in _EMPTY_ITEMS and text not in entries:
                    entries.append(text)
        setattr(result, key, entries)
    return result


def _section_heading(tokens: list, index: int) -> str | None:
    """The section a block token opens, if it is one of the four headings."""
    token = tokens[index]
    if token.type not in ("heading_open", "paragraph_open"):
        return None
    # markdown-it leaves empty text tokens around inline marks.
    children = [c for c in tokens[index + 1].children or [] if c.type != "text" or c.content]
    kinds = [child.type for child in children]
    if token.type == "paragraph_open" and (
        kinds[:1] != ["strong_open"] or kinds.count("strong_open") != 1
        or any(kind not in ("strong_open", "strong_close", "text") for kind in kinds)
    ):
        return None
    title = "".join(child.content for child in children if child.type in ("text", "code_inline"))
    return _HEADINGS.get(" ".join(title.casefold().strip().rstrip(":").split()))


def _item_text(lines: list[str]) -> str:
    """The Markdown of one list item, without its marker or indentation."""
    first = _LIST_MARKER.sub("", lines[0], count=1)
    indent = len(lines[0]) - len(first)
    rest = [line[indent:] if not line[:indent].strip() else line.lstrip() for line in lines[1:]]
    return _CHECKBOX.sub("", "\n".join([first, *rest]).strip())


def _paragraph_entries(lines: list[str]) -> list[str]:
    """One entry per line that ends in a hard break; wrapped lines are joined."""
    entries, current = [], []
    for line in lines:
        hard_break = line.endswith(("\\", "  "))
        current.append(line.strip().removesuffix("\\").strip())
        if hard_break:
            entries.append(" ".join(current))
            current = []
    if current:
        entries.append(" ".join(current))
    return [_CHECKBOX.sub("", entry) for entry in entries]
