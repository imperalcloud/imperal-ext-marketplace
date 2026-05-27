"""Marketplace · Pydantic param + return models for @chat.function tools.

Federal V17 (typed params), V23 (read tools data_model), V24 (write/destructive
tools data_model) — every @chat.function declares both a typed `params:` and
a `data_model=` so the platform can validate $REF paths and prevent naming
drift across the read/write boundary.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ─── Param models ─────────────────────────────────────────────────────── #

class EmptyParams(BaseModel):
    """Federal V17 — no-arg chat.function still needs a typed param model."""
    pass


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


# ─── Return models (data_model=) ──────────────────────────────────────── #

class AppProjection(BaseModel):
    """Slim app snapshot returned in list/search responses."""
    app_id: Optional[str] = None
    display_name: Optional[str] = None
    short_description: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None
    install_count: Optional[int] = None
    avg_rating: Optional[float] = None
    review_count: Optional[int] = None
    featured: Optional[bool] = None
    tags: Optional[list[str]] = None
    system: Optional[bool] = None
    is_installed: Optional[bool] = None


class SearchAppsResult(BaseModel):
    """Federal V23 — return shape for search_marketplace."""
    query: str
    category: str
    total: int
    apps: list[AppProjection]


class AppDetailsResult(BaseModel):
    """Federal V23 — return shape for get_app_details. Loose dict since
    upstream detail payload carries variable metadata fields."""
    app_id: Optional[str] = None
    display_name: Optional[str] = None
    short_description: Optional[str] = None
    long_description: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None
    install_count: Optional[int] = None
    avg_rating: Optional[float] = None
    review_count: Optional[int] = None
    featured: Optional[bool] = None
    tags: Optional[list[str]] = None
    system: Optional[bool] = None
    is_installed: Optional[bool] = None
    author: Optional[str] = None
    homepage: Optional[str] = None
    license: Optional[str] = None
    # Allow extra keys upstream detail may carry (price tiers, etc.)
    model_config = {"extra": "allow"}


class InstalledAppEntry(BaseModel):
    app_id: Optional[str] = None
    display_name: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None


class InstalledAppsResult(BaseModel):
    """Federal V23 — return shape for list_my_installed."""
    total: int
    apps: list[InstalledAppEntry]


class RecommendPick(BaseModel):
    app_id: str
    reason: str


class RecommendResult(BaseModel):
    """Federal V23 — return shape for recommend_for_intent."""
    user_need: str
    picks: list[RecommendPick]
    considered_count: int


class InstallResult(BaseModel):
    """Federal V24 — return shape for install_app (write).

    Loose because auth-gw /v1/marketplace/{app_id}/install response carries
    server-side install metadata that may evolve."""
    app_id: Optional[str] = None
    install_count: Optional[int] = None
    installed_at: Optional[str] = None
    success: Optional[bool] = None
    model_config = {"extra": "allow"}


class UninstallResult(BaseModel):
    """Federal V24 — return shape for uninstall_app (destructive)."""
    app_id: Optional[str] = None
    install_count: Optional[int] = None
    uninstalled_at: Optional[str] = None
    success: Optional[bool] = None
    model_config = {"extra": "allow"}
