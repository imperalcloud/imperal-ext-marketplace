"""Marketplace · skeleton refresh — featured + categories + user's installed apps.

Surfaces a compact catalog snapshot AND the user's current install list
in the per-turn LLM envelope, so Webbee always knows:
  * which apps the user already has → no need to call list_my_installed
  * what's featured / popular → discovery offers grounded in real apps
  * available categories → can filter searches

Refreshes every 10 min (TTL 600s). On user install/uninstall, the
marketplace ext's `_invalidate_kernel_caches` sends Temporal
`update_config` signal which forces immediate reload — so the snapshot
stays in sync with reality without waiting for the TTL.
"""
from __future__ import annotations

import logging

from app import ext
from api import (
    get_featured_apps,
    get_installed_apps_for_user,
    get_marketplace_categories,
)

log = logging.getLogger("marketplace.skeleton")


@ext.skeleton(
    "marketplace_status",
    description=(
        "Marketplace catalog snapshot + user's currently installed apps. "
        "Read by LLM each turn so it knows what's available AND what the "
        "user already has without calling search_marketplace or "
        "list_my_installed first."
    ),
    ttl=600,
)
async def skeleton_refresh_marketplace(ctx) -> dict:
    # 1. User's installed apps — most important field. LLM reads this
    # to avoid recommending apps the user already has, and to know
    # which apps are uninstall candidates.
    try:
        installed = await get_installed_apps_for_user(ctx)
    except Exception as exc:
        log.warning("installed apps fetch failed: %s", exc)
        installed = []

    installed_apps = [
        {
            "app_id": e.get("app_id"),
            "display_name": e.get("name") or e.get("display_name"),
            "category": e.get("category"),
            "system": bool(e.get("system", False)),
        }
        for e in installed
        if isinstance(e, dict) and e.get("app_id")
    ]
    # Split into "user-installed" (real opt-in) vs "system" (always-on
    # admin/billing/etc.) so LLM knows which it can offer to uninstall.
    user_installed = [a for a in installed_apps if not a["system"]]
    system_installed = [a for a in installed_apps if a["system"]]

    # 2. Featured catalog (general, not user-specific) — for discovery.
    try:
        featured = await get_featured_apps(ctx, limit=8)
    except Exception as exc:
        log.warning("featured fetch failed: %s", exc)
        featured = []

    installed_ids = {a["app_id"] for a in installed_apps}
    trimmed_featured = [
        {
            "app_id": a.get("app_id"),
            "display_name": a.get("display_name"),
            "short_description": (a.get("short_description") or "")[:120],
            "category": a.get("category"),
            "install_count": a.get("install_count", 0),
            "already_installed": a.get("app_id") in installed_ids,
        }
        for a in featured
        if isinstance(a, dict) and a.get("app_id")
    ]

    # 3. Categories — for category-filtered search.
    try:
        categories = await get_marketplace_categories(ctx)
    except Exception as exc:
        log.warning("categories fetch failed: %s", exc)
        categories = []

    return {
        "response": {
            "user_installed_count": len(user_installed),
            "user_installed": user_installed,
            "system_apps": system_installed,
            "featured_count": len(trimmed_featured),
            "featured": trimmed_featured,
            "categories": categories[:20],
        }
    }
