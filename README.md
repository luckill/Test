# taskbot

Turns Jira tickets into task records for a coding agent.

This is phase 1, Jira intake: when a ticket is dragged to **Ready for agent**,
the service stores one task record for it and comments "Task received" on the
ticket.

## How it works

1. **Trigger.** Every few minutes the service asks Jira for the tickets in
   "Ready for agent" in the configured projects. A Jira webhook can deliver the
   same signal sooner.
2. **Fetch.** A trigger only names a ticket. The ticket is always fetched again
   through the REST API, so a stale, repeated or forged trigger does no harm.
3. **Convert.** The description arrives as Atlassian Document Format and is
   converted to Markdown (`taskbot/adf.py` converts both ways).
4. **Parse.** The four headings of the story template become the task's story,
   acceptance criteria, out of scope and pointers (`taskbot/story.py`). The
   ticket's summary is put at the top of the story. The work type gives the
   task type and the project gives the repo.
5. **Store.** The task is saved in SQLite with the status `received`
   (`taskbot/store.py`). Each move of a ticket into "Ready for agent" is
   recorded once, so repeat triggers for the same move are ignored. A ticket
   that leaves the column and comes back refreshes its existing task; it never
   gets a second record.

The task format is described in [`schemas/task.schema.json`](schemas/task.schema.json).

## Install

Needs Python 3.11 or newer.

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp config.example.toml config.toml
```

Edit `config.toml`: set your Jira site and map each Jira project key to a
GitHub repo. Then set the credentials in the environment:

```sh
export JIRA_EMAIL=bot@your-company.com
export JIRA_API_TOKEN=...
```

Comments are posted as this account, so give the bot an account of its own. It
needs permission to browse, comment on and transition tickets in the mapped
projects. Create the token at
<https://id.atlassian.com/manage-profile/security/api-tokens>.

## Set up Jira

These steps are done by hand in Jira.

### Board columns

Add these columns to each project's board. A column must have a status of the
same name. In a team-managed project, adding the column creates it. In a
company-managed project, add the statuses to the workflow, allow every status
to move to every other, and map each status to its column.

| Column | Who moves a ticket here | What it means |
| --- | --- | --- |
| Backlog | You | Not ready for the agent |
| Ready for agent | You | Hands the ticket to the agent, or approves a plan |
| Needs info | Agent | The story has gaps, and the questions are in the comments |
| Plan review | Agent | The agent drafted a story and plan and is waiting for approval |
| In progress | Agent | Work has started |
| In review | Agent | A pull request is open |
| Done | Agent, after you merge | The pull request is merged |

### Story template

Make [`templates/story.md`](templates/story.md) the default description, so new
tickets start with the four headings.

- **Team-managed project:** project settings, then Work types. For each work
  type, select the Description field and enter the four headings as its default
  text.
- **Company-managed project:** Jira has no default for the built-in
  Description field. Use an automation rule that fills the description when a
  ticket is created with an empty one.

### Check

```sh
.venv/bin/taskbot check
```

This signs in and reports any column that is missing from a mapped project.

## Run

```sh
.venv/bin/taskbot run          # keep watching
.venv/bin/taskbot run --once   # poll once and exit
```

### Switching to the webhook

Once the service has a public URL:

1. Uncomment the `[webhook]` section in `config.toml` and restart.
2. In Jira, go to Settings, System, WebHooks and create a webhook:
   - URL: `https://<public host>/webhooks/jira`
   - Event: work item updated (`jira:issue_updated`)
   - JQL filter: `status = "Ready for agent"`
3. If you set a secret on the webhook, put the same value in
   `JIRA_WEBHOOK_SECRET`. Deliveries without a valid signature are then
   rejected.

Polling keeps running as a safety net for missed deliveries. Raise
`poll_interval_seconds` if you want it to run less often.

## For later phases

```python
from taskbot.config import Column
from taskbot.task import TaskStatus

jira.post_comment("PAY-123", "Markdown **text**")
jira.move_ticket("PAY-123", Column.NEEDS_INFO)
store.set_status("jira", "PAY-123", TaskStatus.NEEDS_INFO)
```

`jira` is a `taskbot.jira.JiraClient` and `store` a `taskbot.store.Store`.
`handle_ticket` in `taskbot/intake.py` returns `created`, `reentered`,
`duplicate` or `skipped`, which is where a later phase picks up.

## Tests

```sh
.venv/bin/python -m pytest
```

The tests run against an in-memory Jira (`tests/helpers.py`); none of them
needs a Jira site.
