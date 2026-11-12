"""Command line: `taskbot run` and `taskbot check`."""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time
from functools import partial

import anthropic

from .config import Config, ConfigError, load_config
from .intake import Triage, handle_ticket, poll
from .jira import JiraClient, JiraError
from .store import Store
from .triage import triage
from .webhook import PATH, make_server

log = logging.getLogger("taskbot")


def run(
    config: Config, jira: JiraClient, store: Store, once: bool = False, triage: Triage | None = None
) -> None:
    """Poll on a timer and, if the webhook is configured, react to deliveries.

    All handling happens on this thread. The webhook server only queues keys.
    Without `triage`, tickets are recorded and acknowledged but not assessed.
    """
    signals: queue.Queue[str] = queue.Queue()
    if config.webhook and not once:
        server = make_server(config.webhook.host, config.webhook.port, signals.put, config.webhook.secret)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("webhook listening on %s:%s%s", config.webhook.host, server.server_port, PATH)
        if not config.webhook.secret:
            log.warning("JIRA_WEBHOOK_SECRET is not set: webhook deliveries are not verified")

    next_poll = time.monotonic()
    while True:
        try:
            key = signals.get(timeout=max(0.0, next_poll - time.monotonic()))
        except queue.Empty:
            key = None
        try:
            if key is None:
                next_poll = time.monotonic() + config.poll_interval_seconds
                poll(jira, store, config, triage)
                if once:
                    return
            else:
                handle_ticket(key, jira, store, config, triage)
        except Exception:
            log.exception("intake failed%s; will retry on the next poll", f" for {key}" if key else "")
            if once:
                raise


def check(config: Config, jira: JiraClient) -> bool:
    """Verify the Jira side of the setup. Prints a line per finding."""
    ok = True
    try:
        print(f"Signed in to {config.jira_base_url} as {jira.myself()}")
    except JiraError as error:
        print(f"Cannot sign in to {config.jira_base_url}: {error}")
        return False
    for project, repo in config.projects.items():
        try:
            statuses = {name.casefold() for name in jira.project_statuses(project)}
        except JiraError as error:
            print(f"{project}: cannot read the project: {error}")
            ok = False
            continue
        missing = [name for name in config.columns.values() if name.casefold() not in statuses]
        if missing:
            print(f"{project} -> {repo}: missing columns: {', '.join(missing)}")
            ok = False
        else:
            print(f"{project} -> {repo}: all {len(config.columns)} columns found")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taskbot")
    parser.add_argument("--config", default="config.toml", help="path to the config file")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="watch Jira and record tasks")
    run_parser.add_argument("--once", action="store_true", help="poll once and exit")
    commands.add_parser("check", help="verify the Jira credentials and board columns")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2
    jira = JiraClient(config.jira_base_url, config.jira_email, config.jira_api_token, config.columns)

    if args.command == "check":
        return 0 if check(config, jira) else 1
    store = Store(config.database_path)
    # The Claude client reads its credentials from the environment
    # (ANTHROPIC_API_KEY) when the first ticket is assessed.
    assess = partial(triage, jira=jira, store=store, claude=anthropic.Anthropic(), model=config.model)
    try:
        run(config, jira, store, once=args.once, triage=assess)
    except KeyboardInterrupt:
        pass
    except Exception:
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
