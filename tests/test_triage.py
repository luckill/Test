"""Triage end to end: a fake Jira site, the real SDK with a local Claude, a real store."""

from functools import partial

import pytest

from helpers import FakeClaude, FakeJira, doc, field, heading, make_config, para, verdict
from taskbot.intake import Outcome, poll
from taskbot.readiness import CriterionCheck, FieldCheck, FieldStatus, Readiness
from taskbot.store import Store
from taskbot.task import TaskStatus
from taskbot.triage import Classification, classify, questions_comment, triage

PRESENT, MISSING, UNCLEAR = FieldStatus.PRESENT, FieldStatus.MISSING, FieldStatus.UNCLEAR
PLAN = "**Approach**\n\nUse the payment's currency when issuing the refund.\n\n**Steps**\n\n1. Read the currency from the payment."
ERRORS_QUESTION = "Which errors should be handled, and what should happen for each?"


class World:
    def __init__(self, tmp_path):
        self.config = make_config(tmp_path)
        self.site = FakeJira()
        self.site.add_issue("PAY-1")  # a story with two acceptance criteria
        self.jira = self.site.client(self.config)
        self.store = Store(self.config.database_path)
        self.claude = FakeClaude()
        self.started = []
        self.assess = partial(
            triage, jira=self.jira, store=self.store, claude=self.claude.client,
            start=lambda stored: self.started.append(stored.task.ticket_key),
        )

    def poll(self):
        return poll(self.jira, self.store, self.config, self.assess)

    def hand_over(self, key="PAY-1"):
        self.site.move(key, "Ready for agent")
        return self.poll()

    def status(self, key="PAY-1"):
        return self.site.issues[key]["status"]

    def task(self, key="PAY-1"):
        return self.store.get_task("jira", key)

    def fail_jira_once(self, method, path_end, skip=0):
        """Make one matching Jira request fail, after letting `skip` of them through."""
        real = self.jira._transport
        state = {"seen": 0}

        def transport(m, url, headers, body):
            if m == method and url.split("?")[0].endswith(path_end):
                state["seen"] += 1
                if state["seen"] == skip + 1:
                    return 500, {}, b"simulated failure"
            return real(m, url, headers, body)

        self.jira._transport = transport


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.store.close()


# -- classification ---------------------------------------------------------

def readiness(story=PRESENT, criteria=PRESENT, out_of_scope=MISSING, pointers=MISSING, checks=()):
    return Readiness(
        story=FieldCheck(story, "r", "What should change?"),
        acceptance_criteria=FieldCheck(criteria, "r", "What must be true for this to be done?"),
        out_of_scope=FieldCheck(out_of_scope, "r", "What does 'other stuff' cover?"),
        pointers=FieldCheck(pointers, "r", "Which file is 'the usual one'?"),
        criteria=tuple(checks),
    )


@pytest.mark.parametrize("result, expected", [
    (readiness(), Classification.COMPLETE),
    (readiness(out_of_scope=PRESENT, pointers=PRESENT), Classification.COMPLETE),
    (readiness(criteria=UNCLEAR), Classification.PARTIAL),
    (readiness(criteria=MISSING), Classification.PARTIAL),
    (readiness(story=UNCLEAR), Classification.PARTIAL),
    (readiness(story=MISSING), Classification.PARTIAL),  # criteria alone are something to work from
    (readiness(pointers=UNCLEAR), Classification.PARTIAL),
    (readiness(story=MISSING, criteria=MISSING), Classification.NONE),
    (readiness(story=MISSING, criteria=MISSING, pointers=PRESENT), Classification.NONE),
])
def test_classify(result, expected):
    assert classify(result) is expected


def test_questions_cover_the_gaps_and_nothing_else():
    result = readiness(
        criteria=UNCLEAR, pointers=UNCLEAR, out_of_scope=PRESENT,
        checks=[
            CriterionCheck("Returns 404 for unknown IDs", True, "r"),
            CriterionCheck("Handles errors\nwell", False, "r", ERRORS_QUESTION),
        ],
    )
    assert questions_comment(result) == (
        "I can't start on this yet. Please answer the questions below, in a comment or by "
        "editing the description, then move the ticket back to **Ready for agent**.\n"
        "\n**Acceptance criteria**\n\n"
        f'- "Handles errors well": {ERRORS_QUESTION}\n'
        "\n**Pointers**\n\n"
        "- Which file is 'the usual one'?"
    )
    no_criteria = questions_comment(readiness(story=UNCLEAR, criteria=MISSING))
    assert "**Story**\n\n- What should change?" in no_criteria
    assert "**Acceptance criteria**\n\n- What must be true for this to be done?" in no_criteria
    assert "Out of scope" not in no_criteria and "Pointers" not in no_criteria


# -- the complete path ------------------------------------------------------

def test_complete_story_gets_a_plan_moves_to_in_progress_and_starts(world):
    world.claude.verdict = verdict([True, True])
    world.claude.plan = PLAN
    assert world.hand_over() == [("PAY-1", Outcome.CREATED)]

    assert world.site.comment_texts("PAY-1") == [
        "Task received",
        f"**Technical plan**\n\n{PLAN}\n\nStarting work now.",
    ]
    assert world.status() == "In progress"
    assert world.task().status is TaskStatus.IN_PROGRESS
    assert world.task().plan == PLAN
    assert world.started == ["PAY-1"]
    assert world.claude.kinds == ["readiness", "plan"]
    kind, sent = world.claude.requests[1]
    assert sent.startswith("Repository: acme/payments\nTask type: bug\n\n<story>\nWrong refund currency")

    assert world.poll() == []  # the ticket has left the column
    assert world.claude.kinds == ["readiness", "plan"]


# -- the partial path -------------------------------------------------------

def test_partial_story_gets_questions_and_moves_to_needs_info(world):
    world.claude.verdict = verdict([True, ERRORS_QUESTION])
    world.hand_over()

    received, questions = world.site.comment_texts("PAY-1")
    assert received == "Task received"
    assert questions == (
        "I can't start on this yet. Please answer the questions below, in a comment or by "
        "editing the description, then move the ticket back to **Ready for agent**.\n"
        "\n**Acceptance criteria**\n\n"
        f'- "A test covers it.": {ERRORS_QUESTION}'
    )
    assert world.status() == "Needs info"
    assert world.task().status is TaskStatus.NEEDS_INFO
    assert world.task().plan is None
    assert world.started == []
    assert world.claude.kinds == ["readiness"]  # no plan is written for a partial story


def test_moving_the_ticket_back_runs_the_check_again_with_the_replies(world):
    world.claude.verdict = verdict([True, ERRORS_QUESTION])
    world.hand_over()

    world.site.add_comment("PAY-1", "A refund in EUR must show `EUR` on the receipt.", author="Dana")
    world.claude.verdict = verdict([True, True])
    world.claude.plan = PLAN
    assert world.hand_over() == [("PAY-1", Outcome.REENTERED)]

    assert world.claude.kinds == ["readiness", "readiness", "plan"]
    _, second_check = world.claude.requests[1]
    assert "## Clarifications from the ticket's comments" in second_check
    assert f"**The agent asked:**\n\nI can't start on this yet." in second_check
    assert ERRORS_QUESTION in second_check
    assert "**Dana replied:**\n\nA refund in EUR must show `EUR` on the receipt." in second_check
    assert "Task received" not in second_check  # the agent's other comments are not replies

    assert world.status() == "In progress"
    assert world.task().status is TaskStatus.IN_PROGRESS
    assert "Dana replied" in world.task().task.story
    assert world.started == ["PAY-1"]
    texts = world.site.comment_texts("PAY-1")
    assert texts.count("Task received") == 1
    assert texts[-1].startswith("**Technical plan**")


def test_a_story_that_is_still_partial_is_asked_about_again(world):
    world.claude.verdict = verdict([True, ERRORS_QUESTION])
    world.hand_over()
    world.site.add_comment("PAY-1", "Not sure yet.")
    world.claude.verdict = verdict([True, "What exactly must the test check?"])
    world.hand_over()

    assert world.status() == "Needs info"
    asked = [text for text in world.site.comment_texts("PAY-1") if text.startswith("I can't start")]
    assert len(asked) == 2 and "What exactly must the test check?" in asked[1]


def test_an_edited_description_is_checked_without_reading_comments(world):
    world.claude.verdict = verdict([True, ERRORS_QUESTION])
    world.hand_over()
    world.site.edit("PAY-1", description=doc(heading(2, "Story"), para("Now with detail.")))
    world.claude.verdict = verdict([], story=field())
    world.hand_over()
    _, second_check = world.claude.requests[1]
    assert "Now with detail." in second_check
    assert "Clarifications" not in second_check  # nobody replied in the comments


# -- no story, and a model that gives no answer -----------------------------

def test_ticket_with_no_story_is_classified_and_left_alone(world):
    world.site.add_issue("PAY-2", description=None, summary="Do the thing")
    world.claude.verdict = verdict([], story=field("missing", "What should change?"))
    world.site.move("PAY-2", "Ready for agent")
    assert world.poll() == [("PAY-2", Outcome.CREATED)]

    assert world.site.comment_texts("PAY-2") == ["Task received"]
    assert world.status("PAY-2") == "Ready for agent"
    assert world.task("PAY-2").status is TaskStatus.RECEIVED
    assert world.store.get_handoff("jira", "PAY-2", world.jira.get_ticket("PAY-2").status_change_id).classification == "none"

    assert world.poll() == [("PAY-2", Outcome.DUPLICATE)]
    assert world.claude.kinds == ["readiness"]  # not asked again while it sits there


def test_a_refusal_is_reported_on_the_ticket_once_and_not_retried(world):
    world.claude.refuse = True
    world.hand_over()

    received, failure = world.site.comment_texts("PAY-1")
    assert failure.startswith("I could not assess this ticket automatically, so I have not started.")
    assert "the model declined the request (cyber)" in failure
    assert world.status() == "Needs info"
    assert world.task().status is TaskStatus.NEEDS_INFO
    assert world.started == []
    assert world.claude.kinds == ["readiness"]


# -- failures part-way ------------------------------------------------------

def test_an_api_error_leaves_the_ticket_for_the_next_poll(world):
    world.claude.verdict = verdict([True, True])
    world.claude.fail_next = [500]
    assert world.hand_over() == []  # logged, not raised
    assert world.status() == "Ready for agent"
    assert world.site.comment_texts("PAY-1") == ["Task received"]

    assert world.poll() == [("PAY-1", Outcome.DUPLICATE)]
    assert world.status() == "In progress"
    assert world.site.comment_texts("PAY-1")[0] == "Task received"
    assert len(world.site.comments("PAY-1")) == 2


@pytest.mark.parametrize("path_end, skip", [
    ("/comment", 1),      # "Task received" goes through; posting the plan fails
    ("/transitions", 0),  # the plan is posted; moving the ticket fails
])
def test_a_jira_failure_is_finished_later_without_asking_or_posting_twice(world, path_end, skip):
    world.claude.verdict = verdict([True, True])
    world.fail_jira_once("POST", path_end, skip=skip)
    assert world.hand_over() == []  # the failure is logged
    assert world.status() == "Ready for agent"
    assert world.claude.kinds == ["readiness", "plan"]

    assert world.poll() == [("PAY-1", Outcome.DUPLICATE)]
    assert world.claude.kinds == ["readiness", "plan"]  # the saved decision is reused
    assert world.status() == "In progress"
    assert world.task().status is TaskStatus.IN_PROGRESS
    texts = world.site.comment_texts("PAY-1")
    assert texts.count("Task received") == 1
    assert len([text for text in texts if text.startswith("**Technical plan**")]) == 1
    assert world.started == ["PAY-1"]
