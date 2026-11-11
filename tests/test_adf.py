import pytest

from helpers import bullets, doc, heading, para, text
from taskbot.adf import adf_to_markdown, markdown_to_adf, markdown_to_text


def md(*blocks):
    return adf_to_markdown(doc(*blocks))


def test_headings_paragraphs_and_marks():
    out = md(
        heading(2, "Story"),
        para("As a ", text("user ", "strong"), "I want ", text("refund_id", "code"), " and ", text("docs", "link:https://e.com/a")),
    )
    assert out == "## Story\n\nAs a **user** I want `refund_id` and [docs](https://e.com/a)"


def test_whitespace_moves_outside_marks_so_they_still_parse():
    out = md(para(text("bold ", "strong"), text("and ", "strong", "em"), text("both", "em")))
    assert out == "**bold *and*** *both*"
    assert markdown_to_adf(out)["content"][0]["content"][0] == text("bold ", "strong")


@pytest.mark.parametrize("plain", [
    "# not a heading", "1. not a list", "- not a bullet", "> not a quote", "---",
    "2 * 3 * 4", "a [link] here", "see <b>html</b>", "__init__.py", "&amp; stays", "~~kept~~",
    "back`tick", "under_score stays readable",
])
def test_plain_text_survives_a_round_trip(plain):
    out = md(para(plain))
    assert markdown_to_adf(out) == doc(para(plain))


def test_intraword_underscores_are_not_escaped():
    assert md(para("src/my_file.py")) == "src/my_file.py"


def test_lists_nest_and_hold_code_blocks():
    nested = {
        "type": "bulletList",
        "content": [
            {"type": "listItem", "content": [para("one"), bullets("inner")]},
            {"type": "listItem", "content": [
                para("two"),
                {"type": "codeBlock", "attrs": {"language": "py"}, "content": [text("x = 1\n\ny = 2")]},
            ]},
        ],
    }
    out = md(nested)
    assert out == "- one\n  - inner\n- two\n\n  ```py\n  x = 1\n\n  y = 2\n  ```"
    assert markdown_to_adf(out) == doc(nested)


def test_ordered_list_keeps_its_start():
    numbered = {
        "type": "orderedList",
        "attrs": {"order": 3},
        "content": [{"type": "listItem", "content": [para("three")]}, {"type": "listItem", "content": [para("four")]}],
    }
    assert md(numbered) == "3. three\n4. four"
    assert markdown_to_adf("3. three\n4. four") == doc(numbered)


def test_task_list_becomes_checkboxes():
    tasks = {"type": "taskList", "attrs": {"localId": "a"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "b", "state": "TODO"}, "content": [text("open")]},
        {"type": "taskItem", "attrs": {"localId": "c", "state": "DONE"}, "content": [text("closed")]},
    ]}
    assert md(tasks) == "- [ ] open\n- [x] closed"


def test_hard_breaks_quotes_rules_and_panels():
    out = md(
        para("one", {"type": "hardBreak"}, "2. two", {"type": "hardBreak"}),
        {"type": "blockquote", "content": [para("quoted"), para("more")]},
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [para("note")]},
        {"type": "rule"},
    )
    assert out == "one\\\n2\\. two\n\n> quoted\n>\n> more\n\n> note\n\n---"


def test_table_round_trip():
    table = {"type": "table", "content": [
        {"type": "tableRow", "content": [
            {"type": "tableHeader", "content": [para("Name")]},
            {"type": "tableHeader", "content": [para("a|b")]},
        ]},
        {"type": "tableRow", "content": [
            {"type": "tableCell", "content": [para("x", {"type": "hardBreak"}, "y")]},
            {"type": "tableCell", "content": [para(text("code", "code"))]},
        ]},
    ]}
    out = md(table)
    assert out == "| Name | a\\|b |\n| --- | --- |\n| x<br>y | `code` |"
    assert markdown_to_adf(out) == doc(table)


def test_inline_nodes_without_a_markdown_form():
    out = md(para(
        {"type": "mention", "attrs": {"id": "1", "text": "@Ana"}}, " ",
        {"type": "inlineCard", "attrs": {"url": "https://e.com/card"}}, " ",
        {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}}, " ",
        {"type": "date", "attrs": {"timestamp": "1700000000000"}}, " ",
        {"type": "status", "attrs": {"text": "WIP"}},
    ))
    assert out == "@Ana <https://e.com/card> 🎉 2023-11-14 \\[WIP\\]"


def test_unknown_nodes_keep_their_content():
    assert md({"type": "futureBlock", "content": [para("kept")]}) == "kept"
    assert adf_to_markdown(None) == ""


def test_markdown_to_adf_marks_links_and_bare_urls():
    out = markdown_to_adf("See **bold**, *em*, ~~gone~~, `code`, [docs](https://e.com/d) and https://e.com/x.")
    assert out["content"][0]["content"] == [
        text("See "), text("bold", "strong"), text(", "), text("em", "em"), text(", "),
        text("gone", "strike"), text(", "), text("code", "code"), text(", "),
        text("docs", "link:https://e.com/d"), text(" and "),
        text("https://e.com/x", "link:https://e.com/x"), text("."),
    ]


def test_markdown_blocks_adf_cannot_nest_are_flattened():
    out = markdown_to_adf("- # Title\n  > quote\n\n> ## Inside\n> text")
    item = out["content"][0]["content"][0]
    assert item["content"] == [para(text("Title", "strong")), para("quote")]
    assert out["content"][1] == {"type": "blockquote", "content": [para(text("Inside", "strong")), para("text")]}


def test_list_item_always_opens_with_a_paragraph():
    item = markdown_to_adf("- - inner")["content"][0]["content"][0]
    assert item["content"][0] == {"type": "paragraph", "content": []}
    assert item["content"][1]["type"] == "bulletList"


def test_code_block_and_empty_document():
    assert markdown_to_adf("```sh\nls -la\n```") == doc(
        {"type": "codeBlock", "attrs": {"language": "sh"}, "content": [text("ls -la")]}
    )
    assert markdown_to_adf("") == doc()


def test_markdown_to_text_strips_syntax():
    assert markdown_to_text("`src/__init__.py` and [docs](https://e.com) \\*x\\*") == "src/__init__.py and docs (https://e.com) *x*"


def test_typical_agent_comment_is_stable_through_adf():
    comment = (
        "## Plan\n\n1. Fix `refund()` in **payments**\n2. Add a test\n\n"
        "- [see docs](https://e.com/d)\n- note\n\n```py\nprint('hi')\n```\n\n> quoted\n\n---\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |"
    )
    assert adf_to_markdown(markdown_to_adf(comment)) == comment
