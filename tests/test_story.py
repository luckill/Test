from pathlib import Path

from taskbot.story import Story, parse_story

ROOT = Path(__file__).parent.parent


def test_four_headings_are_parsed():
    story = parse_story(
        "## Story\n\nRefunds use the wrong currency.\n\nReproduce by refunding a EUR order.\n\n"
        "## Acceptance criteria\n\n- Refund uses the payment currency.\n- A test covers it.\n\n"
        "## Out of scope\n\n- Partial refunds.\n\n"
        "## Pointers\n\n- `src/payments/__init__.py`\n- [runbook](https://e.com/r)\n- RefundService.issue"
    )
    assert story == Story(
        story="Refunds use the wrong currency.\n\nReproduce by refunding a EUR order.",
        acceptance_criteria=["Refund uses the payment currency.", "A test covers it."],
        out_of_scope=["Partial refunds."],
        pointers=["src/payments/__init__.py", "runbook (https://e.com/r)", "RefundService.issue"],
    )


def test_headings_match_at_any_level_in_any_case_and_in_bold():
    story = parse_story("# STORY:\n\nText.\n\n**Acceptance Criteria**\n\n- One\n\n#### out of scope\n\n- Two")
    assert (story.story, story.acceptance_criteria, story.out_of_scope) == ("Text.", ["One"], ["Two"])


def test_text_before_the_first_heading_and_other_headings_stay_in_the_story():
    story = parse_story("Intro.\n\n## Story\n\nBody.\n\n### Background\n\n- detail\n\n## Pointers\n\n- a.py")
    assert story.story == "Intro.\n\nBody.\n\n### Background\n\n- detail"
    assert story.pointers == ["a.py"]


def test_description_without_headings_is_all_story():
    assert parse_story("Just fix it.") == Story(story="Just fix it.")
    assert parse_story("") == Story()


def test_entries_from_checkboxes_numbers_paragraphs_and_nested_lists():
    story = parse_story(
        "## Acceptance criteria\n\n- [ ] Unchecked\n- [x] Checked\n1. Numbered\n\n"
        "Loose line\\\nSecond line\nwrapped\n\n- Parent\n  - child"
    )
    assert story.acceptance_criteria == [
        "Unchecked", "Checked", "Numbered", "Loose line", "Second line wrapped", "Parent\n- child",
    ]


def test_placeholders_and_duplicates_are_dropped():
    story = parse_story("## Out of scope\n\n- None\n\n## Pointers\n\nN/A\n\n## Acceptance criteria\n\n- Same\n- Same")
    assert (story.out_of_scope, story.pointers, story.acceptance_criteria) == ([], [], ["Same"])


def test_heading_text_inside_a_code_block_is_not_a_heading():
    story = parse_story("## Story\n\n```\n## Pointers\n```\n\nStill the story.")
    assert story.story == "```\n## Pointers\n```\n\nStill the story."
    assert story.pointers == []


def test_the_template_has_the_four_headings_and_nothing_else():
    template = (ROOT / "templates" / "story.md").read_text()
    assert [line for line in template.splitlines() if line] == [
        "## Story", "## Acceptance criteria", "## Out of scope", "## Pointers",
    ]
    assert parse_story(template) == Story()
