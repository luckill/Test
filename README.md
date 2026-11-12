# taskbot

Turns Jira tickets into task records for a coding agent, and decides whether
each one is ready to start.

When a ticket is dragged to **Ready for agent**, the service stores one task
record for it and comments "Task received". It then reads the story: a complete
story gets a technical plan and moves to "In progress", and a story with gaps
gets questions and moves to "Needs info".

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
6. **Triage.** The story is classified as complete, partial or none, and the
   ticket is answered accordingly. See [Triage](#triage).

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
export ANTHROPIC_API_KEY=...
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

## Triage

After a ticket is recorded, `taskbot/triage.py` decides what happens to it.
It needs a Claude API key in `ANTHROPIC_API_KEY`.

1. **Readiness check** (`taskbot/readiness.py`). One model call marks the
   story, the out-of-scope list and the pointers as `present`, `missing` or
   `unclear`, and says for each acceptance criterion whether a test could be
   written from it. "Returns 404 for unknown IDs" passes; "Handles errors
   well" fails. Every gap comes with a question for the ticket's author.
2. **Classification.** The story is `complete` when the story and the
   acceptance criteria are present, every criterion is testable and neither
   optional field is unclear. It is `none` when there is no story and there are
   no acceptance criteria. Anything in between is `partial`.
3. **Path.**

| Story | What the service does |
| --- | --- |
| Complete | Writes a technical plan (`taskbot/plan.py`), posts it as a comment, moves the ticket to "In progress" and starts work. |
| Partial | Posts one question per gap and nothing else, and moves the ticket to "Needs info". |
| None | Records the classification and leaves the ticket where it is. This path is not built yet. |

Moving a ticket back to "Ready for agent" is a new hand-off, so the check
runs again. Replies written in the ticket's comments after the agent's
questions are added to the story, so answering in a comment works as well as
editing the description.

If the model gives no usable answer, the service says so on the ticket, moves
it to "Needs info" and does not retry on its own. A failed call to Jira or to
the Claude API is retried on the next poll, without asking the model again or
posting a comment twice.

"Starts work" currently means the task is marked `in_progress` and
`start_work` in `taskbot/triage.py` is called. That function is the place for
the implementation step, which does not exist yet.

The model is `claude-opus-5-5` unless `config.toml` says otherwise:

```toml
[agent]
model = "claude-haiku-5-5"
```

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
`duplicate` or `skipped`.

## Tests

```sh
.venv/bin/python -m pytest
```

The tests run against an in-memory Jira (`tests/helpers.py`); none of them
needs a Jira site.
