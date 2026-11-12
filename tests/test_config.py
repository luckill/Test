import pytest

from taskbot.config import Column, ConfigError, load_config
from taskbot.task import TaskType

ENV = {"JIRA_EMAIL": "bot@example.com", "JIRA_API_TOKEN": "token"}
MINIMAL = '[jira]\nbase_url = "https://example.atlassian.net/"\n[jira.projects]\nPAY = "acme/payments"\n'


def write(tmp_path, content):
    path = tmp_path / "config.toml"
    path.write_text(content)
    return path


def test_minimal_config_gets_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL), ENV)
    assert config.jira_base_url == "https://example.atlassian.net"
    assert config.projects == {"PAY": "acme/payments"}
    assert config.poll_interval_seconds == 180
    assert config.columns[Column.READY_FOR_AGENT] == "Ready for agent"
    assert config.task_type("Bug") is TaskType.BUG
    assert config.task_type("Spike") is None
    assert config.database_path == tmp_path / "tasks.db"
    assert config.webhook is None
    assert config.model == "claude-opus-5-5"


def test_overrides_and_webhook(tmp_path):
    content = MINIMAL + (
        '[jira.issue_types]\nSpike = "chore"\n[jira.columns]\nready_for_agent = "Agent queue"\n'
        '[database]\npath = "/var/lib/taskbot/tasks.db"\n[webhook]\nport = 9000\n'
        '[agent]\nmodel = "claude-haiku-5-5"\n'
    )
    config = load_config(write(tmp_path, content), {**ENV, "JIRA_WEBHOOK_SECRET": "s3cret"})
    assert config.task_type("spike") is TaskType.CHORE
    assert config.model == "claude-haiku-5-5"
    assert config.columns[Column.READY_FOR_AGENT] == "Agent queue"
    assert str(config.database_path) == "/var/lib/taskbot/tasks.db"
    assert (config.webhook.host, config.webhook.port, config.webhook.secret) == ("0.0.0.0", 9000, "s3cret")


@pytest.mark.parametrize("content, env, message", [
    ('[jira]\nbase_url = "http://x"\n[jira.projects]\nPAY = "a/b"\n', ENV, "https://"),
    ('[jira]\nbase_url = "https://x"\n', ENV, "jira.projects is empty"),
    ('[jira]\nbase_url = "https://x"\n[jira.projects]\nPAY = "payments"\n', ENV, "owner/name"),
    ('[jira]\nbase_url = "https://x"\n[jira.projects]\npay = "a/b"\n', ENV, "not a Jira project key"),
    (MINIMAL + '[jira.issue_types]\nSpike = "research"\n', ENV, "bug, chore or feature"),
    (MINIMAL + '[jira.columns]\nreview = "Review"\n', ENV, "unknown column"),
    (MINIMAL, {"JIRA_EMAIL": "bot@example.com"}, "JIRA_API_TOKEN"),
    ("not toml [", ENV, "config.toml"),
])
def test_bad_config_is_rejected_with_a_reason(tmp_path, content, env, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, content), env)


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml", ENV)


def test_example_config_loads():
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.toml"
    assert load_config(example, ENV).projects == {"PAY": "your-org/payments"}
