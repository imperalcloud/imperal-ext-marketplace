"""Marketplace · Pydantic param + return models for @chat.function tools.

Federal V17 (typed params), V23 (read tools data_model), V24 (write/destructive
tools data_model) — every @chat.function declares both a typed `params:` and
a `data_model=` so the platform can validate $REF paths and prevent naming
drift across the read/write boundary.
"""
from __future__ import annotations

from datetime import datetime  # noqa: F401 — resolves SDL facet forward-refs
from typing import Literal, Optional  # noqa: F401 — Literal resolves facet refs

from pydantic import BaseModel, ConfigDict, Field, model_validator

from imperal_sdk import sdl
# Ref is referenced (as a string forward-ref under `from __future__ import
# annotations`) by inherited SDL facet fields (e.g. Categorized.categories:
# list[Ref]). Importing it + datetime + Literal into this module's namespace
# lets Pydantic auto-resolve those forward-refs when it builds the subclass
# schema at load time — no explicit model_rebuild() needed.
from imperal_sdk.sdl import Ref  # noqa: F401


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

class AppProjection(sdl.Entity, sdl.Categorized, sdl.Rated, sdl.Versioned):
    """Slim app snapshot returned in list/search responses.

    SDL-additive (non-breaking): subclasses sdl.Entity + facets while keeping
    every existing field verbatim. Canonical id/title are derived from
    app_id/display_name via a mode="before" validator so existing dict
    construction (``AppProjection(**projected_dict)``) keeps working unchanged.
    """
    # --- existing fields kept verbatim (search cards / sidebar rely on them) ---
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

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault(
                "title", data.get("display_name") or data.get("app_id") or ""
            )
        return data


class SearchAppsResult(sdl.EntityList[AppProjection]):
    """search_marketplace return shape — a REAL sdl.EntityList[AppProjection]
    (items=[...], total=..., x-sdl='entity-list'). The echoed query/category
    scalars are kept as extra typed fields (EntityList is a pydantic BaseModel,
    so additive fields are allowed). NO legacy {apps:[dict],total} wrapper."""
    query: str = ""
    category: str = ""


class AppDetailsResult(sdl.Entity, sdl.Categorized, sdl.Rated, sdl.Versioned):
    """Federal V23 — return shape for get_app_details. Loose dict since
    upstream detail payload carries variable metadata fields.

    SDL-additive (non-breaking): subclasses sdl.Entity + facets, keeps every
    existing field plus ``extra="allow"`` so the variable upstream detail
    payload (price tiers, etc.) still passes through unchanged. The x-sdl
    marker is preserved in model_config so the platform still detects this as
    an SDL entity from its return_schema."""
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
    # Allow extra keys upstream detail may carry (price tiers, etc.) while
    # keeping the SDL entity marker the platform reads from return_schema.
    model_config = ConfigDict(extra="allow", json_schema_extra={"x-sdl": "entity"})

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault(
                "title", data.get("display_name") or data.get("app_id") or ""
            )
        return data


class InstalledAppEntry(sdl.Entity, sdl.Categorized, sdl.Versioned):
    """One installed-app row. SDL-additive: sdl.Entity + facets, existing
    fields kept verbatim; id/title derived from app_id/display_name."""
    app_id: Optional[str] = None
    display_name: Optional[str] = None
    category: Optional[str] = None
    version: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault(
                "title", data.get("display_name") or data.get("app_id") or ""
            )
        return data


class InstalledAppsResult(sdl.EntityList[InstalledAppEntry]):
    """list_my_installed return shape — a REAL sdl.EntityList[InstalledAppEntry]
    (items=[...], total=..., x-sdl='entity-list'). NO legacy {apps:[dict],total}
    wrapper."""
    pass


class RecommendPick(sdl.Entity):
    """One ranked recommendation pick. SDL-additive: sdl.Entity, existing
    fields kept verbatim; id/title derived from app_id (no human-name field
    on this projection, so title falls back to app_id)."""
    app_id: str = ""
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault("title", data.get("app_id") or "")
        return data


class RecommendResult(sdl.EntityList[RecommendPick]):
    """recommend_for_intent return shape — a REAL sdl.EntityList[RecommendPick]
    (items=[...], x-sdl='entity-list'). The echoed user_need and the
    considered_count scalar are kept as extra typed fields (additive on the
    pydantic EntityList). NO legacy {picks:[dict],user_need,considered_count}
    wrapper."""
    user_need: str = ""
    considered_count: int = 0


class InstallResult(sdl.Entity):
    """Federal V24 — return shape for install_app (write).

    Loose because auth-gw /v1/marketplace/{app_id}/install response carries
    server-side install metadata that may evolve. SDL-additive: subclasses
    sdl.Entity (id/title from app_id), keeps every existing field plus
    ``extra="allow"`` and the x-sdl marker."""
    app_id: Optional[str] = None
    install_count: Optional[int] = None
    installed_at: Optional[str] = None
    success: Optional[bool] = None
    model_config = ConfigDict(extra="allow", json_schema_extra={"x-sdl": "entity"})

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault("title", data.get("app_id") or "")
        return data


class UninstallResult(sdl.Entity):
    """Federal V24 — return shape for uninstall_app (destructive).

    SDL-additive: subclasses sdl.Entity (id/title from app_id), keeps every
    existing field plus ``extra="allow"`` and the x-sdl marker."""
    app_id: Optional[str] = None
    install_count: Optional[int] = None
    uninstalled_at: Optional[str] = None
    success: Optional[bool] = None
    model_config = ConfigDict(extra="allow", json_schema_extra={"x-sdl": "entity"})

    @model_validator(mode="before")
    @classmethod
    def _sdl_canon(cls, data):
        if isinstance(data, dict):
            data.setdefault("id", data.get("app_id") or "")
            data.setdefault("title", data.get("app_id") or "")
        return data
