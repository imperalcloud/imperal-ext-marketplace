"""Marketplace · @chat.function read handlers (search + details).

Split per federal handlers-<300 LOC rule (2026-05-27):
  * handlers.py            — search_marketplace, get_app_details
  * handlers_installed.py  — list_my_installed, recommend_for_intent
  * handlers_lifecycle.py  — install_app, uninstall_app

Permission model (federal): every user may search/inspect every app
without confirmation. Read handlers carry data_model= per V23.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat import ActionResult

from app import chat
from api import get_marketplace_app_details, search_marketplace_apps
from models import (
    AppDetailsResult,
    AppIdParams,
    SearchAppsParams,
    SearchAppsResult,
)

log = logging.getLogger("marketplace.handlers")


_APP_PROJECTION_KEYS = (
    "app_id", "display_name", "short_description", "category",
    "version", "install_count", "avg_rating", "review_count",
    "featured", "tags", "system", "is_installed",
)


def _project_app(app: dict) -> dict:
    """Project a full app payload down to sidebar/search-card-relevant keys."""
    return {k: app.get(k) for k in _APP_PROJECTION_KEYS if k in app}


# ─── Read ─────────────────────────────────────────────────────────────── #

@chat.function(
    "search_marketplace",
    action_type="read",
    data_model=SearchAppsResult,
    description=(
        "Search apps in the Imperal Marketplace by free-text query. "
        "Use when user asks 'do you have X', 'is there an app for Y', "
        "'find me an extension for Z'."
    ),
)
async def fn_search_marketplace(ctx, params: SearchAppsParams) -> ActionResult:
    """Search Marketplace and return a slim list of matching app cards."""
    try:
        apps = await search_marketplace_apps(
            ctx,
            query=params.query,
            category=params.category or "",
            limit=int(params.limit or 20),
        )
    except Exception as exc:
        log.warning("search_marketplace: %s", exc, exc_info=True)
        return ActionResult.error("Failed to search the Marketplace.")

    projected = [_project_app(a) for a in apps]

    # No keyword match — common for non-English queries against the (often
    # sparse, English) catalog metadata, e.g. "телеграм бот". Return the FULL
    # catalog instead of an empty result so the user always sees options and
    # Webbee's narrator can point to the right app semantically. Cheap (one
    # extra HTTP fetch, no LLM). Full multilingual semantic search is a
    # separate, deferred design (specs/2026-05-29-marketplace-multilingual-search).
    if not projected and (params.query or params.category):
        try:
            all_apps = await search_marketplace_apps(ctx, query="", limit=50)
        except Exception as exc:
            log.warning("search_marketplace catalog fallback: %s", exc, exc_info=True)
            all_apps = []
        all_projected = [_project_app(a) for a in all_apps]
        if all_projected:
            return ActionResult.success(
                data={
                    "query": params.query,
                    "category": params.category or "",
                    "total": len(all_projected),
                    "apps": all_projected,
                },
                summary=(
                    f"No exact match for '{params.query}' — showing all "
                    f"{len(all_projected)} available apps so you can choose."
                ),
            )

    summary = (
        f"Found {len(projected)} app(s) matching '{params.query}'."
        if projected
        else f"No apps found matching '{params.query}'."
    )
    return ActionResult.success(
        data={
            "query": params.query,
            "category": params.category or "",
            "total": len(projected),
            "apps": projected,
        },
        summary=summary,
    )


@chat.function(
    "get_app_details",
    action_type="read",
    data_model=AppDetailsResult,
    description=(
        "Get full details for a specific Marketplace app — description, "
        "version, install count, average rating, install status for "
        "current user, tags. Use after search_marketplace."
    ),
)
async def fn_get_app_details(ctx, params: AppIdParams) -> ActionResult:
    """Fetch full Marketplace metadata for one app id, with long-description truncation."""
    try:
        detail = await get_marketplace_app_details(ctx, params.app_id)
    except Exception as exc:
        log.warning("get_app_details %s: %s", params.app_id, exc, exc_info=True)
        return ActionResult.error(f"Failed to fetch details for '{params.app_id}'.")

    if not detail:
        return ActionResult.error(f"App '{params.app_id}' was not found in the Marketplace.")

    detail = dict(detail)
    detail.pop("icon_svg", None)
    long_desc = detail.pop("long_description", None)
    if long_desc and len(str(long_desc)) > 1500:
        detail["long_description"] = str(long_desc)[:1500] + "…"
    elif long_desc:
        detail["long_description"] = long_desc

    return ActionResult.success(
        data=detail,
        summary=(
            f"{detail.get('display_name') or params.app_id}: "
            f"{detail.get('short_description') or ''}"
        )[:200],
    )
