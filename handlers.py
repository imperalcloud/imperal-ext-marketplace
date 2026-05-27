"""Marketplace · @chat.function handlers.

MVP1 (discovery, read-only): search / details / list_my_installed.
MVP2 (install/uninstall + recommend): install_app / uninstall_app /
recommend_for_intent — all 3 added 2026-05-24 per Valentin scope-C.

Permission model (federal): every user may install/uninstall their own
apps. Federal confirmation gate auto-fires on action_type='write' and
action_type='destructive' before dispatch.
"""
from __future__ import annotations

import json
import logging

from imperal_sdk.chat import ActionResult

from app import chat
from api import (
    _invalidate_kernel_caches,
    get_installed_apps_for_user,
    get_marketplace_app_details,
    post_install_app,
    post_uninstall_app,
    search_marketplace_apps,
)
from models import AppIdParams, RecommendParams, SearchAppsParams

log = logging.getLogger("marketplace.handlers")


_APP_PROJECTION_KEYS = (
    "app_id", "display_name", "short_description", "category",
    "version", "install_count", "avg_rating", "review_count",
    "featured", "tags", "system", "is_installed",
)


def _project_app(app: dict) -> dict:
    return {k: app.get(k) for k in _APP_PROJECTION_KEYS if k in app}


# ─── Read ─────────────────────────────────────────────────────────────── #

@chat.function(
    "search_marketplace",
    action_type="read",
    description=(
        "Search apps in the Imperal Marketplace by free-text query. "
        "Use when user asks 'do you have X', 'is there an app for Y', "
        "'find me an extension for Z'."
    ),
)
async def fn_search_marketplace(ctx, params: SearchAppsParams) -> ActionResult:
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
    description=(
        "Get full details for a specific Marketplace app — description, "
        "version, install count, average rating, install status for "
        "current user, tags. Use after search_marketplace."
    ),
)
async def fn_get_app_details(ctx, params: AppIdParams) -> ActionResult:
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


@chat.function(
    "list_my_installed",
    action_type="read",
    description=(
        "List apps currently installed for the user (excludes system apps "
        "like admin/billing which are always on)."
    ),
)
async def fn_list_my_installed(ctx) -> ActionResult:
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


# ─── Recommend (MVP2) ─────────────────────────────────────────────────── #

@chat.function(
    "recommend_for_intent",
    action_type="read",
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
    try:
        candidates = await search_marketplace_apps(
            ctx, query=params.user_need, limit=30,
        )
        if not candidates:
            # No keyword hits — fall back to whole catalog (cap 30)
            candidates = await search_marketplace_apps(ctx, query="", limit=30)
    except Exception as exc:
        log.warning("recommend candidates fetch: %s", exc, exc_info=True)
        return ActionResult.error("Failed to fetch Marketplace candidates.")

    if not candidates:
        return ActionResult.success(
            data={"user_need": params.user_need, "picks": []},
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
    ][:30]

    # LLM rerank — via the kernel-managed `ai:complete` capability.
    # Fail-soft: on any LLM error fall back to install_count-ranked top 3.
    try:
        prompt = (
            "User says they need: "
            + json.dumps(params.user_need)
            + "\n\nHere are Marketplace apps to pick from:\n"
            + json.dumps(catalog_summary, ensure_ascii=False)
            + "\n\nReturn STRICT JSON: "
            '{"picks":[{"app_id":"...","reason":"..."}]} '
            "with the 3 best matches. Pick by semantic relevance to the "
            "user's need, not popularity. Each `reason` is 1 short "
            "sentence why this app fits. JSON only, no prose, no markdown "
            "fences."
        )
        completion = await ctx.ai.complete(prompt)
        text = completion.text if hasattr(completion, "text") else str(completion)
        # Extract first {...} block
        start = text.find("{")
        end = text.rfind("}")
        picks = []
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
            picks = parsed.get("picks", []) if isinstance(parsed, dict) else []
        # Cross-check picked app_ids against candidate set
        valid_ids = {c["app_id"] for c in catalog_summary}
        picks = [p for p in picks if isinstance(p, dict) and p.get("app_id") in valid_ids][:3]
    except Exception as llm_err:
        log.warning("recommend LLM rerank failed: %s — fallback to install_count", llm_err)
        sorted_apps = sorted(
            candidates, key=lambda a: int(a.get("install_count", 0) or 0), reverse=True
        )[:3]
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


# ─── Write / Destructive (MVP2 — federal confirmation gate auto-fires) ──#

@chat.function(
    "install_app",
    action_type="write",
    effects=["create:install"],
    id_projection="app_id",
    description=(
        "Install a Marketplace app for the current user. Use when user "
        "says 'install X', 'add X', 'I want X' OR when they accept a "
        "prior offer with anaphoric reference like 'yes', 'go ahead', "
        "'install it', 'do it'. "
        "REQUIRED arg: app_id (string). When the user message uses an "
        "anaphoric pronoun ('it', 'that one'), resolve app_id from the "
        "MOST RECENT app mentioned in conversation — typically the "
        "subject of the prior search_marketplace / get_app_details / "
        "recommend_for_intent result, OR from the previous assistant "
        "offer ('Should I install spotify?' → app_id='spotify'). "
        "Confirmation gate fires automatically; user must confirm."
    ),
)
async def fn_install_app(ctx, params: AppIdParams) -> ActionResult:
    try:
        result = await post_install_app(ctx, params.app_id)
    except Exception as exc:
        log.warning("install_app %s: %s", params.app_id, exc, exc_info=True)
        return ActionResult.error(
            f"Failed to install '{params.app_id}': {str(exc)[:200]}"
        )

    # Force Hub/sidebar/classifier to see the new install immediately —
    # kernel-side accessible_* caches + skeleton workflow reload.
    await _invalidate_kernel_caches(ctx, params.app_id)

    return ActionResult.success(
        data=result,
        summary=(
            f"Installed '{params.app_id}' for you. "
            f"Total installs: {result.get('install_count', '?')}."
        ),
    )


@chat.function(
    "uninstall_app",
    action_type="destructive",
    effects=["delete:install"],
    id_projection="app_id",
    description=(
        "Uninstall a Marketplace app from the current user's account. "
        "Use when user says 'uninstall X', 'remove X', 'I don't need X' "
        "OR when they confirm a prior offer with anaphoric reference like "
        "'yes', 'uninstall it', 'remove it', 'go ahead', 'do it'. "
        "REQUIRED arg: app_id (string). For anaphoric references "
        "('it', 'that one'), resolve app_id from the MOST RECENT app "
        "mentioned in conversation — typically the just-installed app, "
        "the subject of the prior search_marketplace, OR the app named "
        "in the assistant's previous offer ('Should I uninstall spotify?' "
        "→ app_id='spotify'). NEVER request clarification when the prior "
        "turn unambiguously named the app; just dispatch with that "
        "app_id. Confirmation gate fires automatically; user must "
        "confirm before destructive action runs."
    ),
)
async def fn_uninstall_app(ctx, params: AppIdParams) -> ActionResult:
    try:
        result = await post_uninstall_app(ctx, params.app_id)
    except Exception as exc:
        log.warning("uninstall_app %s: %s", params.app_id, exc, exc_info=True)
        return ActionResult.error(
            f"Failed to uninstall '{params.app_id}': {str(exc)[:200]}"
        )

    # Belt-and-suspenders: auth-gw service.uninstall_app already calls
    # _clear_user_skeleton_state; extension-side duplicate is harmless
    # and guarantees Hub picks up the change even if auth-gw cascade was
    # interrupted.
    await _invalidate_kernel_caches(ctx, params.app_id)

    return ActionResult.success(
        data=result,
        summary=(
            f"Uninstalled '{params.app_id}'. "
            f"Active installs remaining for that app: "
            f"{result.get('install_count', '?')}."
        ),
    )
