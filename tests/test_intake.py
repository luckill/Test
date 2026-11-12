import json
from pathlib import Path

import jsonschema
import pytest

from helpers import FakeJira, doc, make_config, para
from taskbot.intake import Outcome, handle_ticket, poll
from taskbot.store import Store
from taskbot.task import TaskStatus, TaskType

ROOT = Path(__file__).parent.parent
SCHEMA = jsonschema.Draft202012Validator(json.loads((ROOT / "schemas" / "task.schema.json").read_text()))
RECEIVED = doc(para("Task received"))


@pytest.fixture
def world(tmp_path):
    config = make_config(tmp_path)
    site = FakeJira()
    site.add_issue("PAY-1")
    store = Store(config.database_path)
    yield site, site.client(config), store, config
    store.close()


def test_ready_ticket_becomes_one_task_and_gets_one_comment(world):
    site, jira, store, config = world
    site.move("PAY-1", "Ready for agent")
    assert poll(jira, store, config) == [("PAY-1", Outcome.CREATED)]

    [stored] = store.list_tasks()
    assert stored.status is TaskStatus.RECEIVED
    assert stored.task.to_dict() == {
        "source": "jira",
        "ticket_key": "PAY-1",
        "type": "bug",
        "story": "Wrong refund currency\n\nRefunds use the wrong currency.",
        "acceptance_criteria": ["Refund uses the payment currency.", "A test covers it."],
        "out_of_scope": ["Partial refunds."],
        "pointers": ["src/payments/refund_service.py", "RefundService.issue"],
        "repo": "acme/payments",
    }
    SCHEMA.validate(stored.task.to_dict())
    assert site.comments("PAY-1") == [RECEIVED]


def test_repeat_triggers_for_the_same_move_are_ignored(world):
    site, jira, store, config = world
    site.move("PAY-1", "Ready for agent")
    poll(jira, store, config)
    # The bot's own comment and later edits change the ticket but not its column.
    site.edit("PAY-1", summary="Edited after hand-off")
    assert poll(jira, store, config) == [("PAY-1", Outcome.DUPLICATE)]
    assert handle_ticket("PAY-1", jira, store, config) is Outcome.DUPLICATE
    assert len(store.list_tasks()) == 1
    assert store.get_task("jira", "PAY-1").task.story.startswith("Wrong refund currency")
    assert site.comments("PAY-1") == [RECEIVED]


def test_ticket_that_comes_back_refreshes_its_task_without_a_second_record(world):
    site, jira, store, config = world
    site.move("PAY-1", "Ready for agent")
    poll(jira, store, config)
    site.move("PAY-1", "Needs info")
    site.edit("PAY-1", summary="Refunds ignore the payment currency")
    site.move("PAY-1", "Ready for agent")
    assert poll(jira, store, config) == [("PAY-1", Outcome.REENTERED)]
    [stored] = store.list_tasks()
    assert stored.task.story.startswith("Refunds ignore the payment currency")
    assert site.comments("PAY-1") == [RECEIVED]


def test_tickets_elsewhere_are_skipped(world):
    site, jira, store, config = world
    assert handle_ticket("PAY-1", jira, store, config) is Outcome.SKIPPED
    site.add_issue("OPS-1")
    site.move("OPS-1", "Ready for agent")  # a project with no repo in the config
    assert handle_ticket("OPS-1", jira, store, config) is Outcome.SKIPPED
    assert store.list_tasks() == []
    assert site.comments("PAY-1") == site.comments("OPS-1") == []


def test_failed_comment_is_posted_on_the_next_trigger_without_a_second_task(world):
    site, jira, store, config = world
    site.move("PAY-1", "Ready for agent")
    real_transport = site.transport

    def failing(method, url, headers, body):
        if url.endswith("/comment"):
            return 500, {}, b"boom"
        return real_transport(method, url, headers, body)

    jira._transport = failing
    assert poll(jira, store, config) == []  # the failure is logged, not raised
    assert len(store.list_tasks()) == 1 and site.comments("PAY-1") == []

    jira._transport = real_transport
    assert poll(jira, store, config) == [("PAY-1", Outcome.DUPLICATE)]
    assert len(store.list_tasks()) == 1 and site.comments("PAY-1") == [RECEIVED]
    poll(jira, store, config)
    assert site.comments("PAY-1") == [RECEIVED]


def test_one_bad_ticket_does_not_block_the_others(world):
    site, jira, store, config = world
    site.add_issue("PAY-2")
    for key in ("PAY-1", "PAY-2"):
        site.move(key, "Ready for agent")
    site.issues["PAY-1"]["type"] = None  # makes the ticket unreadable
    assert poll(jira, store, config) == [("PAY-2", Outcome.CREATED)]


def test_work_types_map_to_task_types(world):
    site, jira, store, config = world
    for number, (issue_type, expected) in enumerate(
        [("Story", TaskType.FEATURE), ("Task", TaskType.CHORE), ("Spike", TaskType.CHORE)], start=2
    ):
        key = f"PAY-{number}"
        site.add_issue(key, issue_type=issue_type)
        site.move(key, "Ready for agent")
        handle_ticket(key, jira, store, config)
        assert store.get_task("jira", key).task.type is expected


def test_thin_ticket_is_still_recorded(world):
    site, jira, store, config = world
    site.add_issue("PAY-2", description=None, summary="Do the thing")
    site.move("PAY-2", "Ready for agent")
    assert handle_ticket("PAY-2", jira, store, config) is Outcome.CREATED
    task = store.get_task("jira", "PAY-2").task
    assert (task.story, task.acceptance_criteria) == ("Do the thing", [])
    SCHEMA.validate(task.to_dict())


def test_schema_example_is_valid():
    SCHEMA.validate(json.loads((ROOT / "schemas" / "task.example.json").read_text()))


def test_clarifications_are_the_questions_and_the_replies_that_follow():
    from taskbot.intake import clarifications
    from taskbot.jira import Comment

    thread = [
        Comment("1", "Dana", "A note from before the hand-off."),
        Comment("2", "Task Bot", "Task received"),
        Comment("3", "Task Bot", "Which errors?"),
        Comment("4", "Dana", "404 and 422."),
        Comment("5", "Task Bot", "**Technical plan**"),
        Comment("6", "Sam", "  "),
    ]
    mine = {"2": "received", "3": "questions", "5": "plan"}
    assert clarifications(thread, mine) == (
        "## Clarifications from the ticket's comments\n\n"
        "**The agent asked:**\n\nWhich errors?\n\n"
        "**Dana replied:**\n\n404 and 422."
    )
    assert clarifications(thread[:3], mine) == ""  # asked, but nobody has replied
    assert clarifications(thread, {"2": "received"}) == ""  # the agent never asked
