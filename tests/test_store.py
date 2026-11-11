import pytest

from taskbot.store import Handoff, Store
from taskbot.task import Task, TaskStatus, TaskType


def make_task(**changes):
    values = dict(
        source="jira", ticket_key="PAY-1", type=TaskType.BUG, story="Fix it.", repo="acme/payments",
        acceptance_criteria=["Works"], out_of_scope=["Rewrite"], pointers=["a.py"],
    )
    values.update(changes)
    return Task(**values)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "tasks.db")
    yield store
    store.close()


def test_first_handoff_creates_a_received_task(store):
    assert store.record_handoff(make_task(), "101") is Handoff.CREATED
    stored = store.get_task("jira", "PAY-1")
    assert stored.task == make_task()
    assert stored.status is TaskStatus.RECEIVED
    assert not store.is_acknowledged("jira", "PAY-1", "101")


def test_same_status_change_is_a_duplicate_and_changes_nothing(store):
    store.record_handoff(make_task(), "101")
    assert store.record_handoff(make_task(story="Edited."), "101") is Handoff.DUPLICATE
    assert store.get_task("jira", "PAY-1").task.story == "Fix it."
    assert len(store.list_tasks()) == 1


def test_new_status_change_refreshes_the_same_task_and_keeps_its_status(store):
    store.record_handoff(make_task(), "101")
    store.set_status("jira", "PAY-1", TaskStatus.NEEDS_INFO)
    assert store.record_handoff(make_task(story="Clearer."), "102") is Handoff.REENTERED
    stored = store.get_task("jira", "PAY-1")
    assert (stored.task.story, stored.status) == ("Clearer.", TaskStatus.NEEDS_INFO)
    assert len(store.list_tasks()) == 1
    assert store.is_acknowledged("jira", "PAY-1", "102")


def test_acknowledgement(store):
    store.record_handoff(make_task(), "101")
    store.mark_acknowledged("jira", "PAY-1", "101")
    assert store.is_acknowledged("jira", "PAY-1", "101")
    assert not store.is_acknowledged("jira", "PAY-1", "999")


def test_tasks_are_kept_apart_by_ticket_and_survive_reopening(tmp_path):
    store = Store(tmp_path / "tasks.db")
    store.record_handoff(make_task(), "101")
    store.record_handoff(make_task(ticket_key="PAY-2"), "101")
    store.close()
    reopened = Store(tmp_path / "tasks.db")
    assert [stored.task.ticket_key for stored in reopened.list_tasks()] == ["PAY-1", "PAY-2"]
    assert reopened.record_handoff(make_task(), "101") is Handoff.DUPLICATE
    reopened.close()


def test_set_status_for_an_unknown_task_fails(store):
    with pytest.raises(KeyError):
        store.set_status("jira", "PAY-404", TaskStatus.DONE)
