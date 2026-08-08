"""Marketplace · behaviour tests.

The repo had NO tests before this file. Each test pins a behaviour that was
measured broken against production, so a regression fails here instead of in
the user's chat.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── fakes ─────────────────────────────────────────────────────────────── #

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def text(self):
        return str(self._payload)


class _HTTP:
    """Fake ctx.http reproducing the auth-gw quirks measured live."""

    def __init__(self, *, catalog=None, installed=None, per_page_cap=50):
        self.catalog = catalog or []
        self.installed = installed or []
        self.per_page_cap = per_page_cap
        self.calls: list[tuple[str, dict]] = []
        self.posted: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))

        if url.endswith("/extensions"):
            return _Resp(200, {"extensions": self.installed})

        if "/marketplace/apps/" in url and url.endswith("/reviews"):
            return _Resp(200, {"reviews": [], "total": 0, "distribution": {}})

        if "/marketplace/categories" in url:
            return _Resp(200, [{"category": "tools", "count": 1}])

        if "/marketplace/featured" in url:
            return _Resp(200, {"apps": self.catalog[:5]})

        if "/marketplace/apps/" in url:                      # detail
            app_id = url.rsplit("/", 1)[-1]
            for a in self.catalog:
                if a.get("app_id") == app_id:
                    return _Resp(200, a)
            return _Resp(404, {})                            # delisted app

        if url.endswith("/marketplace/apps"):                # search
            per_page = int(params.get("per_page", 20))
            # THE production quirk: over the cap the server returns nothing.
            if per_page > self.per_page_cap:
                return _Resp(200, {"apps": []})
            rows = self.catalog
            if params.get("search"):
                q = params["search"].lower()
                rows = [
                    a for a in rows
                    if q in (a.get("app_id", "") + a.get("display_name", "")
                             + (a.get("short_description") or "")).lower()
                ]
            if params.get("category"):
                c = params["category"].lower()
                rows = [a for a in rows if (a.get("category") or "").lower() == c]
            page = int(params.get("page", 1))
            start = (page - 1) * per_page
            return _Resp(200, {"apps": rows[start:start + per_page]})

        return _Resp(404, {})

    async def post(self, url, json=None, headers=None, timeout=None):
        self.posted.append((url, json or {}))
        if url.endswith("/install"):
            return _Resp(200, {"app_id": url.split("/")[-2], "installed": True})
        if url.endswith("/reviews"):
            return _Resp(201, {"id": 7, "rating": (json or {}).get("rating")})
        return _Resp(404, {})

    async def delete(self, url, headers=None, timeout=None):
        # auth-gw maps uninstall to DELETE on the SAME /install path --
        # not POST /uninstall. Mirror that exactly.
        self.deleted.append(url)
        if url.endswith("/install"):
            return _Resp(200, {"app_id": url.split("/")[-2], "uninstalled": True})
        return _Resp(404, {})


class _User:
    imperal_id = "imp_u_tE-J9c_NxX"
    access_token = "tok"


class _Ctx:
    def __init__(self, http):
        self.http = http
        self.user = _User()


def _app(app_id, name=None, **kw):
    row = {
        "app_id": app_id,
        "display_name": name or app_id.title(),
        "short_description": kw.pop("desc", ""),
        "category": kw.pop("category", "tools"),
        "install_count": kw.pop("install_count", 0),
        "avg_rating": kw.pop("avg_rating", 0.0),
        "review_count": kw.pop("review_count", 0),
        "developer_id": kw.pop("developer_id", "imp_u_dev"),
        "developer_name": kw.pop("developer_name", "Dev"),
        "developer_nickname": kw.pop("developer_nickname", "dev"),
        "version": "1.0.0",
    }
    row.update(kw)
    return row


# ── the bug that started this: uninstalling a delisted app ────────────── #

def test_uninstall_resolves_an_installed_app_missing_from_the_catalog():
    """'uninstall spotify' must work even though spotify 404s in the catalog.

    Measured on production: spotify and google-drive-connector are installed
    for this user, yet GET /v1/marketplace/apps/spotify returns 404 and the
    app is absent from the catalog listing. Resolving uninstall against the
    catalog alone answered "no Marketplace app matches 'spotify'" while the
    app sat right there in the user's own list.
    """
    import api

    http = _HTTP(
        catalog=[_app("notes"), _app("mail")],           # NO spotify
        installed=[{"app_id": "spotify", "name": "Spotify"},
                   {"app_id": "notes", "name": "Notes"}],
    )
    app_id, candidates = asyncio.run(
        api.resolve_app_id(_Ctx(http), "spotify", prefer_installed=True))

    assert app_id == "spotify", (
        f"installed-but-delisted app must resolve; got {app_id!r} / {candidates!r}")


def test_uninstall_resolves_by_display_name_too():
    """'uninstall Google Drive' → google-drive-connector, delisted or not."""
    import api

    http = _HTTP(
        catalog=[_app("notes")],
        installed=[{"app_id": "google-drive-connector",
                    "name": "Google Drive Connector"}],
    )
    app_id, _ = asyncio.run(
        api.resolve_app_id(_Ctx(http), "Google Drive", prefer_installed=True))
    assert app_id == "google-drive-connector"


def test_install_still_resolves_against_the_catalog():
    """The uninstall fix must not change install: it resolves catalog-first."""
    import api

    http = _HTTP(catalog=[_app("spotify", "Spotify")], installed=[])
    app_id, _ = asyncio.run(api.resolve_app_id(_Ctx(http), "Spotify"))
    assert app_id == "spotify"


def test_ambiguous_name_asks_instead_of_guessing():
    """Two plausible matches must return candidates, never a coin flip."""
    import api

    http = _HTTP(catalog=[_app("google-drive-connector", "Google Drive"),
                          _app("google-search-console", "Google Search Console")],
                 installed=[])
    app_id, candidates = asyncio.run(api.resolve_app_id(_Ctx(http), "google"))
    assert app_id is None and len(candidates) >= 2


# ── the catalog was invisible past 50 ─────────────────────────────────── #

def test_asking_for_the_whole_catalog_does_not_return_zero():
    """per_page>50 yields an EMPTY list on the real server — page instead.

    Measured: per_page=51/100/200 all returned 0 apps, so "show me everything"
    reported an empty Marketplace while 22 apps existed.
    """
    import api

    catalog = [_app(f"app-{i:03d}") for i in range(120)]
    http = _HTTP(catalog=catalog, installed=[])

    apps = asyncio.run(api.list_all_marketplace_apps(_Ctx(http)))

    assert len(apps) == 120, f"expected the whole catalog, got {len(apps)}"
    assert len({a["app_id"] for a in apps}) == 120, "must not duplicate rows"
    assert all(int(p.get("per_page", 0)) <= 50 for _, p in http.calls
               if "per_page" in p), "must never exceed the server ceiling"


def test_a_single_page_request_is_clamped_not_emptied():
    """limit=200 on search must return rows, not silently nothing."""
    import api

    http = _HTTP(catalog=[_app(f"a{i}") for i in range(60)], installed=[])
    rows = asyncio.run(api.search_marketplace_apps(_Ctx(http), limit=200))
    assert rows, "over-large limit must be clamped, not turned into zero rows"


# ── identity: real ids and real authors ───────────────────────────────── #

def test_search_results_carry_the_real_developer_id_and_rating():
    """Author + id + rating must survive the projection, not be dropped."""
    from handlers import _project_app

    row = _app("notes", "Notes", developer_id="imp_u_RogDc6K4_L",
               developer_name="Dmitrii", developer_nickname="dmitrii",
               avg_rating=5.0, review_count=1, tool_count=31)
    out = _project_app(row)

    assert out["app_id"] == "notes"
    assert out["developer_id"] == "imp_u_RogDc6K4_L", "real author id must be kept"
    assert out["developer_nickname"] == "dmitrii"
    assert out["avg_rating"] == 5.0
    assert out["tool_count"] == 31


def test_projection_never_masks_a_value_behind_an_at_handle():
    """No field may present an '@handle' in place of the real value."""
    from handlers import _project_app

    out = _project_app(_app("notes", developer_nickname="dmitrii"))
    for key, value in out.items():
        assert not (isinstance(value, str) and value.startswith("@")), \
            f"{key} masks the real value behind an @handle: {value!r}"


# ── bulk ──────────────────────────────────────────────────────────────── #

def test_bulk_uninstall_removes_every_named_app_in_one_call():
    import handlers_bulk
    from models import BulkAppIdsParams

    http = _HTTP(
        catalog=[_app("notes")],
        installed=[{"app_id": "spotify", "name": "Spotify"},
                   {"app_id": "google-drive-connector",
                    "name": "Google Drive Connector"},
                   {"app_id": "sql-db", "name": "SQL Database"}],
    )
    res = asyncio.run(handlers_bulk.fn_bulk_uninstall_apps(
        _Ctx(http),
        BulkAppIdsParams(app_ids=["spotify", "Google Drive", "sql-db"]),
    ))

    assert sorted(res.data["succeeded"]) == [
        "google-drive-connector", "spotify", "sql-db"]
    assert len(http.deleted) == 3, "each app must be uninstalled exactly once"


def test_one_bad_name_does_not_cancel_the_rest():
    """Partial success is reported, not an all-or-nothing abort."""
    import handlers_bulk
    from models import BulkAppIdsParams

    http = _HTTP(catalog=[], installed=[{"app_id": "spotify", "name": "Spotify"}])
    res = asyncio.run(handlers_bulk.fn_bulk_uninstall_apps(
        _Ctx(http),
        BulkAppIdsParams(app_ids=["spotify", "definitely-not-an-app"]),
    ))

    assert res.data["succeeded"] == ["spotify"], "the good one must still run"
    assert res.data["failure_count"] == 1, "the bad one must be reported"


def test_bulk_does_not_act_twice_on_the_same_app():
    """'Spotify' and 'spotify' in one call = one uninstall, not two."""
    import handlers_bulk
    from models import BulkAppIdsParams

    http = _HTTP(catalog=[], installed=[{"app_id": "spotify", "name": "Spotify"}])
    asyncio.run(handlers_bulk.fn_bulk_uninstall_apps(
        _Ctx(http), BulkAppIdsParams(app_ids=["Spotify", "spotify"])))

    assert len(http.deleted) == 1, "duplicate names must not act twice"


def test_bulk_install_installs_each_named_app():
    import handlers_bulk
    from models import BulkAppIdsParams

    http = _HTTP(catalog=[_app("notes"), _app("mail")], installed=[])
    res = asyncio.run(handlers_bulk.fn_bulk_install_apps(
        _Ctx(http), BulkAppIdsParams(app_ids=["notes", "mail"])))

    assert sorted(res.data["succeeded"]) == ["mail", "notes"]
    assert len([u for u, _ in http.posted if u.endswith("/install")]) == 2


# ── reviews ───────────────────────────────────────────────────────────── #

def test_review_is_posted_verbatim():
    """The user's words and stars reach the server unchanged."""
    import handlers_bulk
    from models import ReviewParams

    http = _HTTP(catalog=[_app("notes")], installed=[])
    asyncio.run(handlers_bulk.fn_review_app(
        _Ctx(http),
        ReviewParams(app_id="notes", rating=5, body="Отличное приложение!"),
    ))

    _, payload = [p for p in http.posted if p[0].endswith("/reviews")][0]
    assert payload["rating"] == 5
    assert payload["body"] == "Отличное приложение!", "review text must be verbatim"


def test_reviews_can_be_read_for_an_app():
    import handlers_bulk
    from models import AppReviewsParams

    http = _HTTP(catalog=[_app("notes")], installed=[])
    res = asyncio.run(handlers_bulk.fn_get_app_reviews(
        _Ctx(http), AppReviewsParams(app_id="notes")))
    assert res.data["app_id"] == "notes"


# ── browse / by developer ─────────────────────────────────────────────── #

def test_browse_filters_by_developer_nickname_and_by_real_id():
    import handlers_bulk
    from models import BrowseParams

    catalog = [
        _app("notes", developer_id="imp_u_A", developer_nickname="dmitrii"),
        _app("mail", developer_id="imp_u_B", developer_nickname="seeu"),
        _app("web-tools", developer_id="imp_u_B", developer_nickname="seeu"),
    ]
    ctx = _Ctx(_HTTP(catalog=catalog, installed=[]))

    by_nick = asyncio.run(handlers_bulk.fn_browse_marketplace(
        ctx, BrowseParams(developer="seeu")))
    assert {a["app_id"] for a in by_nick.data["items"]} == {"mail", "web-tools"}

    by_id = asyncio.run(handlers_bulk.fn_browse_marketplace(
        ctx, BrowseParams(developer="imp_u_A")))
    assert {a["app_id"] for a in by_id.data["items"]} == {"notes"}


def test_browse_with_no_filters_returns_the_whole_catalog():
    import handlers_bulk
    from models import BrowseParams

    ctx = _Ctx(_HTTP(catalog=[_app(f"a{i}") for i in range(75)], installed=[]))
    res = asyncio.run(handlers_bulk.fn_browse_marketplace(ctx, BrowseParams()))
    assert res.data["total"] == 75, "browse must not stop at one page"
