# Coding Agent

A service that picks up Jira tickets and turns them into pull requests. This is the
starting point: a FastAPI app with one Jira call wired in.

## Setup

Requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env    # then fill in your Jira site, bot email and API token
```

## Run

```bash
uvicorn app.main:app --reload --env-file .env
```

The interactive API docs are at http://127.0.0.1:8000/docs.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Returns `{"status": "ok"}` |
| GET | `/jira/ready-tickets?project=PROJ` | Lists the tickets in the "Ready for agent" column of a Jira project |

`/jira/ready-tickets` answers 503 when the Jira settings are missing and 502 when
Jira rejects the request, with Jira's own message in `detail`.

The API has no login yet. Keep it bound to `127.0.0.1` until it does.

## Tests

```bash
pytest
```

The tests use a fake Jira, so they need no network and no credentials.

## Layout

```
app/
  main.py          FastAPI app and routes
  jira_client.py   Jira Cloud client
schemas/
  task.schema.json Internal task format
tests/
```
