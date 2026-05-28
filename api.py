"""Marketplace · auth-gw HTTP wrapper.

Все endpoints уже существуют в auth-gw:
  GET  /v1/marketplace/apps?...        — search by query/category
  GET  /v1/marketplace/apps/{app_id}   — full app detail (incl. is_installed)
  GET  /v1/marketplace/categories       — categories list
  GET  /v1/marketplace/featured?limit=N — featured apps
  POST /v1/marketplace/apps/{app_id}/install
  POST /v1/marketplace/apps/{app_id}/uninstall

Federal-clean transport: every call goes through ``ctx.http`` (live
HTTPClient instance — `await ctx.http.get(...)` directly, no factory).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger("marketplace.api")

_AUTH_GW = os.getenv("AUTH_GATEWAY_URL", "http://104.224.88.155:8085")


def _user_jwt_headers(ctx) -> dict:
    """Build auth headers from kernel-provided user context.

    When the user has an access_token in their kernel context, prefer the
    user JWT (RBAC fully evaluated server-side). When access_token is
    missing (skeleton refresh / background activity / system-actor turn),
    fall back to the service token PLUS the `X-Acting-User` header so
    auth-gw can enforce per-user policies server-side.
    """
    headers: dict = {}
    tok = getattr(ctx.user, "access_token", "") or ""
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    svc = os.getenv("AUTH_GATEWAY_SERVICE_TOKEN", "") or os.getenv(
        "IMPERAL_SERVICE_TOKEN", ""
    )
    if svc:
        headers["X-Service-Token"] = svc
    # Required by auth-gw write endpoints when using service-token auth.
    # Federal: every per-user action MUST surface the acting user so
    # marketplace_installs / user_extensions rows attribute correctly.
    user_id = getattr(ctx.user, "imperal_id", "") or ""
    if user_id:
        headers["X-Acting-User"] = user_id
    return headers


async def search_marketplace_apps(
    ctx,
    *,
    query: str = "",
    category: str = "",
    limit: int = 20,
) -> list[dict]:
    # Auth-gw GET /v1/marketplace/apps reads `search` + `per_page` (NOT `q` /
    # `limit`). Sending the wrong names made every query return the FULL
    # catalog unfiltered (the param never reached the LIKE filter). Match the
    # endpoint's contract so name/description/tag filtering actually applies.
    params: dict[str, Any] = {"per_page": min(max(int(limit), 1), 50)}
    if query:
        params["search"] = query
    if category:
        params["category"] = category
    resp = await ctx.http.get(
        f"{_AUTH_GW}/v1/marketplace/apps",
        params=params,
        headers=_user_jwt_headers(ctx),
        timeout=10.0,
    )
    if resp.status_code != 200:
        log.warning("search_marketplace_apps: HTTP %s body=%s", resp.status_code, resp.text()[:200])
        return []
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("apps") or data.get("results") or []
    return []


async def get_marketplace_app_details(ctx, app_id: str) -> dict | None:
    resp = await ctx.http.get(
        f"{_AUTH_GW}/v1/marketplace/apps/{app_id}",
        headers=_user_jwt_headers(ctx),
        timeout=10.0,
    )
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.warning("get_app_details %s: HTTP %s", app_id, resp.status_code)
        return None
    return resp.json()


async def get_installed_apps_for_user(ctx) -> list[dict]:
    user_id = ctx.user.imperal_id
    resp = await ctx.http.get(
        f"{_AUTH_GW}/v1/users/{user_id}/extensions",
        headers=_user_jwt_headers(ctx),
        timeout=10.0,
    )
    if resp.status_code != 200:
        log.warning("list_user_extensions: HTTP %s", resp.status_code)
        return []
    data = resp.json()
    if isinstance(data, dict):
        return data.get("extensions", []) or []
    return data if isinstance(data, list) else []


async def get_marketplace_categories(ctx) -> list[str]:
    resp = await ctx.http.get(
        f"{_AUTH_GW}/v1/marketplace/categories",
        headers=_user_jwt_headers(ctx),
        timeout=5.0,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    if isinstance(data, list):
        return [str(c) for c in data if c]
    if isinstance(data, dict):
        cats = data.get("categories", [])
        return [str(c) for c in cats if c]
    return []


async def get_featured_apps(ctx, limit: int = 10) -> list[dict]:
    resp = await ctx.http.get(
        f"{_AUTH_GW}/v1/marketplace/featured",
        params={"limit": limit},
        headers=_user_jwt_headers(ctx),
        timeout=5.0,
    )
    if resp.status_code != 200:
        return []
    data = resp.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("featured") or data.get("apps") or []
    return []


def _norm_app_token(s) -> str:
    """Normalize an app reference for matching: lowercase, alphanumerics only.
    'Microsoft Ads' / 'microsoft-ads' / 'MICROSOFT' -> 'microsoftads' / 'microsoft'."""
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


async def resolve_app_id(ctx, term: str) -> tuple[str | None, list[str]]:
    """Resolve a user-supplied app reference to a canonical Marketplace app_id.

    The classifier/step-extractor frequently passes the user's literal word
    ('microsoft') or the display name ('TG Bot Builder') rather than the
    canonical id ('microsoft-ads' / 'tg-bot'); the install/uninstall endpoints
    then reject it with HTTP 400 "App not found or not active". Resolve against
    the catalog before dispatching.

    Returns ``(app_id, candidates)``:
      * ``(canonical_id, [])``  — unique resolution (or input already canonical)
      * ``(None, [names...])``  — ambiguous (>=2 distinct matches) → caller clarifies
      * ``(None, [])``          — no match → caller reports not found
    """
    term_n = _norm_app_token(term)
    if not term_n:
        return None, []

    apps = await search_marketplace_apps(ctx, query=term, limit=50)
    if not apps:
        apps = await search_marketplace_apps(ctx, query="", limit=50)

    def _aid(a: dict) -> str:
        return a.get("app_id") or a.get("id") or ""

    def _name(a: dict) -> str:
        return a.get("display_name") or a.get("name") or ""

    # 1. exact canonical app_id (also covers the already-correct case)
    for a in apps:
        if _norm_app_token(_aid(a)) == term_n:
            return _aid(a), []
    # 2. exact display name
    for a in apps:
        if _norm_app_token(_name(a)) == term_n:
            return _aid(a), []
    # 3. substring / prefix match — accept only when it resolves uniquely
    matches = []
    for a in apps:
        aid_n = _norm_app_token(_aid(a))
        name_n = _norm_app_token(_name(a))
        if (term_n in aid_n or term_n in name_n
                or aid_n.startswith(term_n) or name_n.startswith(term_n)):
            matches.append(a)
    uniq = {_aid(m) for m in matches if _aid(m)}
    if len(uniq) == 1:
        return next(iter(uniq)), []
    if uniq:
        # ambiguous — surface display names so the caller can disambiguate
        return None, [(_name(m) or _aid(m)) for m in matches][:8]
    # 4. The server already filtered by name/description/tags. If it narrowed
    #    to exactly one app, trust it — covers semantic terms absent from the
    #    app_id/display_name but present in the description (e.g. 'telegram'
    #    → tg-bot whose description mentions Telegram). A broad/no-match query
    #    falls back to the full catalog above, so len==1 here is meaningful.
    if len(apps) == 1:
        return _aid(apps[0]), []
    return None, []


async def post_install_app(ctx, app_id: str) -> dict:
    """POST /v1/marketplace/apps/{app_id}/install — install for current user.

    Returns {"app_id", "installed": True, "install_count": N} on success.
    Raises on HTTP error so the @chat.function handler can surface a
    clean ActionResult.error with the actual message.
    """
    resp = await ctx.http.post(
        f"{_AUTH_GW}/v1/marketplace/apps/{app_id}/install",
        headers=_user_jwt_headers(ctx),
        timeout=15.0,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"install_app HTTP {resp.status_code}: {resp.text()[:300]}"
        )
    return resp.json()


async def _invalidate_kernel_caches(ctx, app_id: str) -> None:
    """Force Hub/sidebar/classifier to see the install/uninstall change
    immediately. Best-effort — never breaks the calling handler on Redis
    or Temporal failure.

    Three operations:
      1. DEL `imperal:user_accessible_apps:{user_id}` — set source cache
         that delivery.py + classifier read every turn.
      2. DEL `imperal:user_accessible_exts:{user_id}` — full ext dict
         cache that the cap UI reads (`/v1/users/{user_id}/extensions`
         kernel-side mirror).
      3. Temporal signal `update_config` to `skeleton-imperal-hub-{user_id}`
         — forces the user's skeleton workflow to reload sections from
         load_all_user_extensions (which honours fresh marketplace_installs).

    Auth-gw's service.install_app + service.uninstall_app already
    invalidate `imperal:user_extensions:{user_id}` (the per-extensions
    list cache). What they do NOT touch is the kernel-side accessible_*
    caches and the running skeleton workflow. This helper closes that
    gap from extension-side.
    """
    import os
    user_id = getattr(ctx.user, "imperal_id", "") or ""
    if not user_id:
        return
    try:
        # ctx.cache lives in the kernel-side Redis (the same instance the
        # kernel uses for user_accessible_* caches).
        cache = getattr(ctx, "cache", None)
        if cache is not None:
            await cache.delete(f"imperal:user_accessible_apps:{user_id}")
            await cache.delete(f"imperal:user_accessible_exts:{user_id}")
    except Exception as e:
        log.debug("cache invalidate non-fatal: %s", e)
    # Temporal signal to skeleton workflow — pure no-op if not running.
    try:
        from temporalio.client import Client  # type: ignore
        host = os.getenv("TEMPORAL_HOST", "104.224.88.156")
        port = os.getenv("TEMPORAL_PORT", "7233")
        ns = os.getenv("TEMPORAL_NAMESPACE_HUB", "imperal-hub")
        cli = await Client.connect(f"{host}:{port}", namespace=ns)
        handle = cli.get_workflow_handle(f"skeleton-imperal-hub-{user_id}")
        try:
            desc = await handle.describe()
            if desc.status.name == "RUNNING":
                await handle.signal("update_config")
        except Exception:
            pass
    except Exception as e:
        log.debug("temporal signal non-fatal: %s", e)


async def post_uninstall_app(ctx, app_id: str) -> dict:
    """DELETE /v1/marketplace/apps/{app_id}/install — uninstall for current user.

    Note the verb: auth-gw maps INSTALL = `POST /install` and UNINSTALL =
    `DELETE /install` on the SAME path. Not `POST /uninstall`.

    Triggers the federal _clear_user_skeleton_state cascade in auth-gw
    (DEL skeleton/ext_version/user_accessible Redis caches + Temporal
    signal update_config to skeleton workflow).
    """
    resp = await ctx.http.delete(
        f"{_AUTH_GW}/v1/marketplace/apps/{app_id}/install",
        headers=_user_jwt_headers(ctx),
        timeout=15.0,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(
            f"uninstall_app HTTP {resp.status_code}: {resp.text()[:300]}"
        )
    return resp.json()
