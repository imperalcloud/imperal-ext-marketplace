"""Marketplace · bulk lifecycle + reviews + full-catalog browse.

Split per the federal handlers-<300 LOC rule:
  * handlers.py            — search_marketplace, get_app_details
  * handlers_installed.py  — list_my_installed, recommend_for_intent
  * handlers_lifecycle.py  — install_app, uninstall_app  (single app)
  * handlers_bulk.py       — THIS FILE: bulk install/uninstall, reviews,
                             browse_marketplace, apps_by_developer

Why bulk exists: asking to remove three apps used to mean three separate
confirmation gates and three round trips, and a single unresolvable name
aborted the whole intent. These handlers resolve EVERY name first, report
per-app outcomes, and never let one bad name cancel the rest.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat import ActionResult

from api import (
    _invalidate_kernel_caches,
    get_app_reviews,
    get_installed_apps_for_user,
    list_all_marketplace_apps,
    post_app_review,
    post_install_app,
    post_uninstall_app,
    resolve_app_id,
)
from app import chat
from models import (
    AppReviewsParams,
    BrowseParams,
    BulkAppIdsParams,
    BulkResult,
    DeveloperAppsParams,
    ReviewParams,
    ReviewsResult,
    SearchAppsResult,
)

log = logging.getLogger("marketplace.handlers_bulk")


async def _resolve_many(ctx, terms: list[str], *, prefer_installed: bool):
    """Resolve every user-supplied name up front.

    Returns ``(resolved, failures)`` where resolved is a list of
    ``(original_term, app_id)`` and failures is a list of ready-to-show
    strings. De-dupes on the RESOLVED id, so 'Spotify' and 'spotify' in one
    call act once rather than acting twice and reporting a phantom failure.
    """
    resolved: list[tuple[str, str]] = []
    failures: list[str] = []
    seen: set[str] = set()
    for term in terms:
        app_id, candidates = await resolve_app_id(
            ctx, term, prefer_installed=prefer_installed,
        )
        if app_id is None:
            if candidates:
                failures.append(
                    f"{term} — ambiguous ({', '.join(candidates[:4])})"
                )
            else:
                failures.append(f"{term} — no match")
            continue
        if app_id in seen:
            continue
        seen.add(app_id)
        resolved.append((term, app_id))
    return resolved, failures


def _bulk_result(action: str, ok: list[str], failed: list[str]) -> ActionResult:
    """One uniform shape + summary for both bulk lifecycle tools."""
    data = {
        "action": action,
        "succeeded": ok,
        "failed": failed,
        "total": len(ok) + len(failed),
        "success_count": len(ok),
        "failure_count": len(failed),
    }
    if ok and not failed:
        summary = f"{action.capitalize()}ed {len(ok)}: {', '.join(ok)}."
    elif ok and failed:
        summary = (
            f"{action.capitalize()}ed {len(ok)} ({', '.join(ok)}); "
            f"{len(failed)} failed — {'; '.join(failed)}."
        )
    else:
        summary = f"Nothing {action}ed — {'; '.join(failed)}."
    # Partial success is still success: the caller must see what DID happen,
    # not a bare error that hides the apps actually acted on.
    return ActionResult.success(data=data, summary=summary)


# ─── Bulk lifecycle ───────────────────────────────────────────────────── #

@chat.function(
    "bulk_install_apps",
    action_type="write",
    effects=["create:install"],
    data_model=BulkResult,
    description=(
        "Install SEVERAL Marketplace apps in ONE call. Use whenever the user "
        "names more than one app to install ('install spotify and notes', "
        "'add these three apps') — do NOT call install_app repeatedly. "
        "Accepts ids, display names or partial names. Confirmation gate "
        "fires once for the whole batch."
    ),
)
async def fn_bulk_install_apps(ctx, params: BulkAppIdsParams) -> ActionResult:
    """Install every named app, reporting per-app outcomes."""
    resolved, failures = await _resolve_many(
        ctx, params.app_ids, prefer_installed=False,
    )
    installed: list[str] = []
    for _term, app_id in resolved:
        try:
            await post_install_app(ctx, app_id)
        except Exception as exc:
            log.warning("bulk install %s: %s", app_id, exc, exc_info=True)
            failures.append(f"{app_id} — {str(exc)[:120]}")
            continue
        installed.append(app_id)
        await _invalidate_kernel_caches(ctx, app_id)
    return _bulk_result("install", installed, failures)


@chat.function(
    "bulk_uninstall_apps",
    action_type="destructive",
    effects=["delete:install"],
    data_model=BulkResult,
    description=(
        "Uninstall SEVERAL apps in ONE call — the batch version of "
        "uninstall_app. Use whenever the user names more than one app to "
        "remove ('uninstall spotify, google drive and mysql', 'remove these "
        "apps', 'get rid of them'). Resolves against the user's INSTALLED "
        "apps, so delisted apps still uninstall. Confirmation gate fires "
        "once for the whole batch."
    ),
)
async def fn_bulk_uninstall_apps(ctx, params: BulkAppIdsParams) -> ActionResult:
    """Uninstall every named app, reporting per-app outcomes."""
    resolved, failures = await _resolve_many(
        ctx, params.app_ids, prefer_installed=True,
    )
    removed: list[str] = []
    for _term, app_id in resolved:
        try:
            await post_uninstall_app(ctx, app_id)
        except Exception as exc:
            log.warning("bulk uninstall %s: %s", app_id, exc, exc_info=True)
            failures.append(f"{app_id} — {str(exc)[:120]}")
            continue
        removed.append(app_id)
        await _invalidate_kernel_caches(ctx, app_id)
    return _bulk_result("uninstall", removed, failures)


# ─── Reviews ──────────────────────────────────────────────────────────── #

@chat.function(
    "get_app_reviews",
    action_type="read",
    data_model=ReviewsResult,
    description=(
        "Read the user reviews of one app — star rating, review text, who "
        "wrote it, whether they actually use the app, and the 1-5 star "
        "distribution. Use for 'what do people say about X', 'is X any "
        "good', 'show reviews for X'."
    ),
)
async def fn_get_app_reviews(ctx, params: AppReviewsParams) -> ActionResult:
    """Return the reviews + rating distribution for one app."""
    app_id, candidates = await resolve_app_id(ctx, params.app_id)
    if app_id is None:
        if candidates:
            return ActionResult.error(
                f"'{params.app_id}' matches several apps: "
                f"{', '.join(candidates)}. Which one?"
            )
        return ActionResult.error(f"No app matches '{params.app_id}'.")

    payload = await get_app_reviews(ctx, app_id, limit=params.limit)
    reviews = payload.get("reviews") or []
    dist = payload.get("distribution") or {}
    total = payload.get("total", len(reviews))
    if not reviews:
        return ActionResult.success(
            data={"app_id": app_id, "items": [], "total": 0,
                  "distribution": dist},
            summary=f"'{app_id}' has no reviews yet.",
        )
    stars = [int(r.get("rating") or 0) for r in reviews if r.get("rating")]
    avg = round(sum(stars) / len(stars), 2) if stars else None
    return ActionResult.success(
        data={"app_id": app_id, "items": reviews, "total": total,
              "distribution": dist, "avg_rating": avg},
        summary=(
            f"'{app_id}' — {total} review(s)"
            + (f", average {avg}★." if avg is not None else ".")
        ),
    )


@chat.function(
    "review_app",
    action_type="write",
    effects=["create:review"],
    id_projection="app_id",
    data_model=ReviewsResult,
    description=(
        "Leave (or update) YOUR review of an app — star rating 1-5 plus the "
        "review text. Use for 'review X', 'rate X 5 stars', 'leave a review "
        "saying ...'. Pass the user's wording VERBATIM as body. Posting "
        "again on the same app updates the existing review."
    ),
)
async def fn_review_app(ctx, params: ReviewParams) -> ActionResult:
    """Post the caller's review of one app."""
    # Reviewing something you own is the common case, so resolve installed
    # apps first -- and it makes a delisted-but-installed app reviewable.
    app_id, candidates = await resolve_app_id(
        ctx, params.app_id, prefer_installed=True,
    )
    if app_id is None:
        if candidates:
            return ActionResult.error(
                f"'{params.app_id}' matches several apps: "
                f"{', '.join(candidates)}. Which one?"
            )
        return ActionResult.error(f"No app matches '{params.app_id}'.")

    try:
        result = await post_app_review(
            ctx, app_id,
            rating=params.rating, body=params.body, title=params.title,
        )
    except Exception as exc:
        log.warning("review_app %s: %s", app_id, exc, exc_info=True)
        return ActionResult.error(
            f"Couldn't post the review for '{app_id}': {str(exc)[:200]}"
        )
    return ActionResult.success(
        data={"app_id": app_id, "review": result},
        summary=f"Posted your {params.rating}★ review of '{app_id}'.",
    )


# ─── Full-catalog browse ──────────────────────────────────────────────── #

def _matches_developer(app: dict, needle: str) -> bool:
    """True if the app's author matches by REAL id, nickname or name."""
    n = needle.strip().lower().lstrip("@")
    if not n:
        return True
    for key in ("developer_id", "developer_nickname", "developer_name"):
        val = str(app.get(key) or "").lower()
        if val and (val == n or n in val):
            return True
    return False


@chat.function(
    "browse_marketplace",
    action_type="read",
    data_model=SearchAppsResult,
    description=(
        "List the WHOLE Marketplace catalog with optional filters — every "
        "app, not just the first page. Filters (all optional, combined with "
        "AND): category, developer (real imperal_id, nickname or name), "
        "min_rating, installed_only, free_only. Sort by installs, rating, "
        "name or newest. Use for 'show me everything in the marketplace', "
        "'all apps by X', 'top rated apps', 'what's in productivity'."
    ),
)
async def fn_browse_marketplace(ctx, params: BrowseParams) -> ActionResult:
    """Return the full catalog, filtered and sorted."""
    try:
        apps = await list_all_marketplace_apps(ctx, category=params.category)
    except Exception as exc:
        log.warning("browse_marketplace: %s", exc, exc_info=True)
        return ActionResult.error("Failed to load the Marketplace catalog.")

    total_catalog = len(apps)

    if params.developer:
        apps = [a for a in apps if _matches_developer(a, params.developer)]
    if params.min_rating:
        apps = [a for a in apps
                if float(a.get("avg_rating") or 0) >= params.min_rating]
    if params.installed_only:
        apps = [a for a in apps if a.get("is_installed")]
    if params.free_only:
        apps = [a for a in apps
                if str(a.get("pricing_model") or "free").lower() == "free"]

    sort = (params.sort or "installs").lower()
    if sort == "rating":
        apps.sort(key=lambda a: (float(a.get("avg_rating") or 0),
                                 int(a.get("review_count") or 0)), reverse=True)
    elif sort == "name":
        apps.sort(key=lambda a: str(a.get("display_name") or "").lower())
    elif sort == "newest":
        apps.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
    else:
        apps.sort(key=lambda a: int(a.get("install_count") or 0), reverse=True)

    from handlers import _project_app  # local import avoids a cycle
    items = [_project_app(a) for a in apps]

    bits = []
    if params.category:
        bits.append(f"category '{params.category}'")
    if params.developer:
        bits.append(f"developer '{params.developer}'")
    if params.min_rating:
        bits.append(f"rated {params.min_rating}★+")
    if params.installed_only:
        bits.append("installed only")
    if params.free_only:
        bits.append("free only")
    scope = f" matching {', '.join(bits)}" if bits else ""

    return ActionResult.success(
        data={"items": items, "total": len(items),
              "catalog_total": total_catalog, "sort": sort},
        summary=(
            f"{len(items)} app(s){scope}"
            + (f" out of {total_catalog} in the Marketplace." if scope
               else " in the Marketplace.")
        ),
    )


@chat.function(
    "apps_by_developer",
    action_type="read",
    data_model=SearchAppsResult,
    description=(
        "Every app published by one developer, found by their REAL "
        "imperal_id (imp_u_*), nickname or display name. Use for 'what else "
        "did they write', 'apps by seeu', 'show everything from imp_u_...'."
    ),
)
async def fn_apps_by_developer(ctx, params: DeveloperAppsParams) -> ActionResult:
    """Return every catalog app authored by the named developer."""
    try:
        apps = await list_all_marketplace_apps(ctx)
    except Exception as exc:
        log.warning("apps_by_developer: %s", exc, exc_info=True)
        return ActionResult.error("Failed to load the Marketplace catalog.")

    hits = [a for a in apps if _matches_developer(a, params.developer)]
    if not hits:
        known = sorted({
            str(a.get("developer_nickname") or a.get("developer_name") or "")
            for a in apps
        } - {""})
        return ActionResult.error(
            f"No apps by '{params.developer}'. Known developers: "
            f"{', '.join(known[:12])}."
        )

    hits.sort(key=lambda a: int(a.get("install_count") or 0), reverse=True)
    from handlers import _project_app
    items = [_project_app(a) for a in hits]
    first = hits[0]
    who = (first.get("developer_name")
           or first.get("developer_nickname") or params.developer)
    total_installs = sum(int(a.get("install_count") or 0) for a in hits)
    return ActionResult.success(
        data={"items": items, "total": len(items),
              "developer_id": first.get("developer_id"),
              "developer_name": first.get("developer_name"),
              "developer_nickname": first.get("developer_nickname"),
              "total_installs": total_installs},
        summary=(
            f"{who} ({first.get('developer_id')}) published {len(items)} "
            f"app(s), {total_installs} installs total."
        ),
    )
