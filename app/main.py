"""HTTP API for the coding agent.

Run it locally:
    uvicorn app.main:app --reload --env-file .env
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query

from app.jira_client import JiraClient, JiraConfigError, JiraError, TicketRef

app = FastAPI(
    title="Coding Agent",
    version="0.1.0",
    description="Turns Jira tickets into pull requests.",
)


def get_jira_client() -> Iterator[JiraClient]:
    """Build a Jira client from the environment for one request."""
    try:
        client = JiraClient.from_env()
    except JiraConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        yield client
    finally:
        client.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/jira/ready-tickets", response_model=list[TicketRef])
def ready_tickets(
    project: Annotated[
        str,
        Query(min_length=1, max_length=50, description="Jira project key, such as PROJ"),
    ],
    jira: Annotated[JiraClient, Depends(get_jira_client)],
) -> list[TicketRef]:
    """List the tickets waiting in the "Ready for agent" column."""
    try:
        return jira.find_ready_tickets(project)
    except JiraError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
