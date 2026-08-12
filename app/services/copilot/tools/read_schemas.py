"""Typed input contracts for the read-only (search/list) domain tools
(architecture Section 5 / 26, read side). Kept in a separate module from
schemas.py since these back "kind='read'" ToolSpecs, not write tools."""

from typing import Literal

from app.services.copilot.tools.schemas import ToolInput


class SearchRocksInput(ToolInput):
    team_id: int | None = None


class SearchIssuesInput(ToolInput):
    open_only: bool = False


class SearchKpisInput(ToolInput):
    team_id: int | None = None


class SearchMeetingsInput(ToolInput):
    upcoming_only: bool = False


class SearchClientRequestsInput(ToolInput):
    pass


class SearchProjectsInput(ToolInput):
    pass


class SearchTeamsInput(ToolInput):
    pass


class GetMyScoreboardInput(ToolInput):
    period: Literal["this_week", "this_month", "this_quarter", "this_year"] = "this_month"


class GetTeamScoreboardInput(ToolInput):
    team_id: int
    period: Literal["this_week", "this_month", "this_quarter", "this_year"] = "this_month"


class GetOrgScoreboardInput(ToolInput):
    period: Literal["this_week", "this_month", "this_quarter", "this_year"] = "this_month"
    team_id: int | None = None


class SearchEverythingInput(ToolInput):
    query: str
    limit: int = 10


class SearchKnowledgeInput(ToolInput):
    """architecture item 1 — Knowledge/RAG retrieval. doc_type/tags are
    metadata filters applied before ranking (not a post-filter on already
    top-k'd results, so a narrow filter can't starve out relevant matches)."""
    query: str
    doc_type: str | None = None
    tags: list[str] | None = None
    limit: int = 5
