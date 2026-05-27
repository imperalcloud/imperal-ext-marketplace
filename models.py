"""Marketplace · Pydantic param models for @chat.function tools."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SearchAppsParams(BaseModel):
    """Search Marketplace by free-text keyword + optional category."""
    query: str = Field(
        ...,
        description=(
            "Free-text search query — matches app name and description "
            "substring. Pass the user's wording verbatim when possible."
        ),
    )
    category: Optional[str] = Field(
        default=None,
        description=(
            "Optional Marketplace category filter (productivity, "
            "communication, media, analytics, etc). Omit for all categories."
        ),
    )
    limit: int = Field(
        default=20,
        description="Max results (default 20, capped server-side at 50).",
        ge=1, le=50,
    )


class AppIdParams(BaseModel):
    """Target a specific Marketplace app by id."""
    app_id: str = Field(
        ...,
        description=(
            "Marketplace app id (e.g. 'spotify', 'mail', 'notes'). "
            "Pull from prior search_marketplace result or skeleton featured "
            "list. NEVER fabricate."
        ),
    )


class RecommendParams(BaseModel):
    """Recommend apps for a user need (LLM-reranked)."""
    user_need: str = Field(
        ...,
        description=(
            "Plain-language description of what the user wants to do "
            "(e.g. 'manage my email better', 'monitor my domains'). "
            "The tool fetches candidate apps from Marketplace and asks "
            "an LLM to pick the top-3 best fits with reasoning."
        ),
    )
