"""Marketplace · installed-apps + LLM-reranked recommendation handlers.

Split from handlers.py per the federal handlers-<300 LOC rule (2026-05-27).
Both are read-only — no confirmation gate, no audit-ledger writes.
"""
from __future__ import annotations

import json
import logging

from imperal_sdk.chat import ActionResult

from api import get_installed_apps_for_user, search_marketplace_apps
from app import chat
from models import (
    EmptyParams,
    InstalledAppsResult,
    RecommendParams,
    RecommendResult,
)

log = logging.getLogger("marketplace.handlers_installed")

_RECOMMEND_CANDIDATE_CAP = 30
_RECOMMEND_PICK_TARGET = 3


@chat.function(
    "list_my_installed",
    action_type="read",
    data_model=InstalledAppsResult,
    description=(
        "List apps currently installed for the user (excludes system apps "
        "like admin/billing which are always on)."
    ),
)
async def fn_list_my_installed(ctx, params: EmptyParams) -> ActionResult:
    """Return the current user's non-system installed apps."""
    try:
        exts = await get_installed_apps_for_user(ctx)
    except Exception as exc:
        log.warning("list_my_installed: %s", exc, exc_info=True)
        return ActionResult.error("Failed to fetch your installed apps.")

    user_installed = [
        {
            "app_id": e.get("app_id"),
            "display_name": e.get("name") or e.get("display_name"),
            "category": e.get("category"),
            "version": e.get("version"),
        }
        for e in exts
        if isinstance(e, dict) and not e.get("system")
    ]
    return ActionResult.success(
        data={"total": len(user_installed), "apps": user_installed},
        summary=f"You have {len(user_installed)} app(s) installed.",
    )


async def _llm_pick_top(
    ctx, user_need: str, catalog_summary: list[dict]
) -> list[dict]:
    """Ask the LLM to rerank candidates; on any error return empty list."""
    prompt = (
        "User says they need: "
        + json.dumps(user_need)
        + "\n\nHere are Marketplace apps to pick from:\n"
        + json.dumps(catalog_summary, ensure_ascii=False)
        + "\n\nReturn STRICT JSON: "
        '{"picks":[{"app_id":"...","reason":"..."}]} '
        f"with the {_RECOMMEND_PICK_TARGET} best matches. Pick by semantic "
        "relevance to the user's need, not popularity. Each `reason` is "
        "1 short sentence why this app fits. JSON only, no prose, no "
        "markdown fences."
    )
    completion = await ctx.ai.complete(prompt)
    text = completion.text if hasattr(completion, "text") else str(completion)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    parsed = json.loads(text[start:end + 1])
    picks = parsed.get("picks", []) if isinstance(parsed, dict) else []
    valid_ids = {c["app_id"] for c in catalog_summary}
    return [
        p for p in picks
        if isinstance(p, dict) and p.get("app_id") in valid_ids
    ][:_RECOMMEND_PICK_TARGET]


@chat.function(
    "recommend_for_intent",
    action_type="read",
    data_model=RecommendResult,
    description=(
        "Recommend top Marketplace apps matching a user's stated need. "
        "Fetches up to 30 candidate apps from the catalog by keyword "
        "match, then uses an LLM to pick the top 3 with reasoning. "
        "Use when user describes a use case ('I need something for X', "
        "'help me find an app to do Y'). Returns ranked picks with "
        "explanations."
    ),
)
async def fn_recommend_for_intent(ctx, params: RecommendParams) -> ActionResult:
    """Two-stage recommendation: keyword candidates → LLM rerank top 3."""
    try:
        candidates = await search_marketplace_apps(
            ctx, query=params.user_need, limit=_RECOMMEND_CANDIDATE_CAP,
        )
        if not candidates:
            candidates = await search_marketplace_apps(
                ctx, query="", limit=_RECOMMEND_CANDIDATE_CAP,
            )
    except Exception as exc:
        log.warning("recommend candidates fetch: %s", exc, exc_info=True)
        return ActionResult.error("Failed to fetch Marketplace candidates.")

    if not candidates:
        return ActionResult.success(
            data={"user_need": params.user_need, "picks": [], "considered_count": 0},
            summary="No Marketplace apps available right now.",
        )

    catalog_summary = [
        {
            "app_id": a.get("app_id"),
            "display_name": a.get("display_name"),
            "category": a.get("category"),
            "short_description": a.get("short_description", "")[:200],
        }
        for a in candidates
        if a.get("app_id")
    ][:_RECOMMEND_CANDIDATE_CAP]

    try:
        picks = await _llm_pick_top(ctx, params.user_need, catalog_summary)
    except Exception as llm_err:
        log.warning(
            "recommend LLM rerank failed: %s — fallback to install_count", llm_err
        )
        sorted_apps = sorted(
            candidates,
            key=lambda a: int(a.get("install_count", 0) or 0),
            reverse=True,
        )[:_RECOMMEND_PICK_TARGET]
        picks = [
            {
                "app_id": a.get("app_id"),
                "reason": f"Popular ({a.get('install_count', 0)} installs).",
            }
            for a in sorted_apps if a.get("app_id")
        ]

    return ActionResult.success(
        data={
            "user_need": params.user_need,
            "picks": picks,
            "considered_count": len(catalog_summary),
        },
        summary=f"Top {len(picks)} app(s) for: {params.user_need[:80]}.",
    )
