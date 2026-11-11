"""Load and validate the config file. Secrets come from the environment."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from .task import TaskType


class Column(StrEnum):
    """The board columns. A ticket's column is its Jira status."""

    BACKLOG = "backlog"
    READY_FOR_AGENT = "ready_for_agent"
    NEEDS_INFO = "needs_info"
    PLAN_REVIEW = "plan_review"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


DEFAULT_COLUMN_NAMES = {
    Column.BACKLOG: "Backlog",
    Column.READY_FOR_AGENT: "Ready for agent",
    Column.NEEDS_INFO: "Needs info",
    Column.PLAN_REVIEW: "Plan review",
    Column.IN_PROGRESS: "In progress",
    Column.IN_REVIEW: "In review",
    Column.DONE: "Done",
}

# Jira work types, lower case. Override or extend under [jira.issue_types].
DEFAULT_ISSUE_TYPES = {
    "bug": TaskType.BUG,
    "story": TaskType.FEATURE,
    "feature": TaskType.FEATURE,
    "new feature": TaskType.FEATURE,
    "improvement": TaskType.FEATURE,
    "epic": TaskType.FEATURE,
    "task": TaskType.CHORE,
    "chore": TaskType.CHORE,
    "sub-task": TaskType.CHORE,
    "subtask": TaskType.CHORE,
}

_PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]+")
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class WebhookConfig:
    host: str
    port: int
    secret: str | None


@dataclass(frozen=True)
class Config:
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    poll_interval_seconds: int
    columns: Mapping[Column, str]
    projects: Mapping[str, str]  # Jira project key -> GitHub "owner/name"
    issue_types: Mapping[str, TaskType]  # lower-case Jira work type -> task type
    database_path: Path
    webhook: WebhookConfig | None

    def task_type(self, issue_type: str) -> TaskType | None:
        return self.issue_types.get(issue_type.casefold())


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found. Copy config.example.toml to start.") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from None

    jira = _table(data, "jira")
    base_url = str(jira.get("base_url", "")).rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigError("jira.base_url must be an https:// URL, e.g. https://your-site.atlassian.net")

    projects = {str(key): str(repo) for key, repo in _table(jira, "projects").items()}
    if not projects:
        raise ConfigError("jira.projects is empty: map at least one Jira project key to a GitHub repo")
    for key, repo in projects.items():
        if not _PROJECT_KEY.fullmatch(key):
            raise ConfigError(f"jira.projects: {key!r} is not a Jira project key")
        if not _REPO.fullmatch(repo):
            raise ConfigError(f"jira.projects.{key}: {repo!r} is not a repo in owner/name form")

    columns = dict(DEFAULT_COLUMN_NAMES)
    for name, status in _table(jira, "columns").items():
        try:
            columns[Column(name)] = str(status)
        except ValueError:
            known = ", ".join(Column)
            raise ConfigError(f"jira.columns: unknown column {name!r}; expected one of {known}") from None

    issue_types = dict(DEFAULT_ISSUE_TYPES)
    for name, kind in _table(jira, "issue_types").items():
        try:
            issue_types[str(name).casefold()] = TaskType(kind)
        except ValueError:
            raise ConfigError(f"jira.issue_types.{name}: {kind!r} is not bug, chore or feature") from None

    interval = jira.get("poll_interval_seconds", 180)
    if not isinstance(interval, int) or interval < 10:
        raise ConfigError("jira.poll_interval_seconds must be a whole number of at least 10")

    webhook = None
    if "webhook" in data:
        section = _table(data, "webhook")
        webhook = WebhookConfig(
            host=str(section.get("host", "0.0.0.0")),
            port=int(section.get("port", 8080)),
            secret=env.get("JIRA_WEBHOOK_SECRET") or None,
        )

    missing = [name for name in ("JIRA_EMAIL", "JIRA_API_TOKEN") if not env.get(name)]
    if missing:
        raise ConfigError(f"set the environment variable(s) {', '.join(missing)}")

    database = Path(str(_table(data, "database").get("path", "tasks.db")))
    return Config(
        jira_base_url=base_url,
        jira_email=env["JIRA_EMAIL"],
        jira_api_token=env["JIRA_API_TOKEN"],
        poll_interval_seconds=interval,
        columns=columns,
        projects=projects,
        issue_types=issue_types,
        database_path=database if database.is_absolute() else path.parent / database,
        webhook=webhook,
    )


def _table(data: Mapping, name: str) -> Mapping:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a table")
    return value
