"""Marketplace · The Store Engine, server side.

WHY THIS FILE EXISTS
--------------------
Search in this extension forwarded the user's words to the gateway as
``?search=`` and let a SQL ``LIKE`` decide. That finds an app when you already
know its name and fails at everything else: "todo list", "kanban", "task
tracker" and "vikuna" all returned nothing while the app's own description
said exactly those words. The catalog knew the answer; the query could not
read it.

The Panel's launchpad solved this with a real ranking engine
(``src/lib/shell/store-search.ts``): it reads everything an app publishes --
name, id, tags, category, summary, description, tool names, developer -- and
scores it with field weights, prefix/infix tiers, synonyms, acronyms, typo
tolerance and IDF-weighted coverage. That is why the Start menu finds things
this extension could not.

This is that engine, in Python, rule for rule. Not "inspired by": the same
phrase list, the same synonym groups, the same weights, the same match
multipliers, the same Damerau-Levenshtein budget, the same IDF coverage pass.
A user must not get different answers for the same words depending on whether
they typed them in the launcher or in the Marketplace app -- two ranking
systems that disagree is a worse outcome than one imperfect ranking system.

WHY IT IS DUPLICATED RATHER THAN SHARED
---------------------------------------
The launchpad's copy is TypeScript running in the browser; this one is Python
running in the worker. There is no runtime either can import from the other.
The honest options were: keep them in sync by hand (this), or expose ranking
as a network call (a hop per keystroke, and a hard dependency between the
Panel and this extension). The rules are stable and small enough that the
first is the cheaper truth. Every constant below is annotated with WHY it has
its value, so a future edit to either copy can be reasoned about rather than
guessed at.

PURE BY CONSTRUCTION: no ctx, no http, no SDK imports. That is what makes it
testable in isolation and reusable by any handler here.
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Iterable, Sequence

# ── normalisation ─────────────────────────────────────────────────────────
#
# Case, accents and punctuation are noise for matching. "Café", "cafe" and
# "CAFE" are one word; "e-mail", "e mail" and "email" are one word. Doing this
# once, in one place, is what keeps every comparison below honest -- a scorer
# that lowercases in some branches and not others fails in ways that look
# random from the outside.

_PUNCT_RE = re.compile(r"[_\-.,/\\|:;()\[\]{}'\"`!?*+@#$%^&~<>]")
_SPACE_RE = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Fold accents, lowercase, and turn punctuation into spaces."""
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = _PUNCT_RE.sub(" ", stripped.lower())
    return _SPACE_RE.sub(" ", spaced).strip()


# ── phrases ───────────────────────────────────────────────────────────────
#
# Multi-word expressions that mean ONE thing, collapsed before ranking.
#
# MEASURED, NOT THEORETICAL. "todo list" ranked the actual task manager FOURTH
# on the live catalog. Not a weighting bug: the tokenizer split a single idea
# into two words and scored them as independent evidence. The word "list"
# appears in 16 of 77 apps -- almost always inside "listings", where it says
# nothing about lists -- so three apps that merely contain the substring
# outvoted the one app that IS a todo list.
#
# Weights cannot fix that and IDF only softens it: as long as "todo list" is
# two facts instead of one, matching both halves of an accident beats matching
# the whole meaning.
#
# Deliberately narrow. Only expressions whose parts mislead on their own go
# here. "task manager" is left alone precisely because it already works. A
# phrase list that grows past the failures it was measured against becomes a
# second, invisible ranking system nobody can debug.

_PHRASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("todo", "list"), "todo"),
    (("to", "do", "list"), "todo"),
    (("to", "do"), "todo"),
    (("task", "list"), "todo"),
    (("e", "mail"), "email"),
    (("data", "base"), "database"),
    (("word", "press"), "wordpress"),
    (("spread", "sheet"), "spreadsheet"),
    (("help", "desk"), "helpdesk"),
)


def tokenize(s: str) -> list[str]:
    """Split into words, collapsing known phrases into their meaning."""
    words = [w for w in normalize(s).split(" ") if w]
    if len(words) < 2:
        return words

    out: list[str] = []
    i = 0
    while i < len(words):
        matched = False
        # Longest phrase first, so "to do list" is not eaten by "to do".
        for phrase, meaning in _PHRASES:
            if len(phrase) > len(words) - i:
                continue
            if all(words[i + k] == w for k, w in enumerate(phrase)):
                out.append(meaning)
                i += len(phrase)
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return out


# ── synonyms ──────────────────────────────────────────────────────────────
#
# Words people type vs. words publishers write.
#
# Deliberately SMALL and hand-picked. A big auto-generated thesaurus makes a
# store search feel drunk -- it starts returning "close enough" results for
# precise queries, which is worse than returning nothing. Every entry is a
# real vocabulary gap between how the catalog describes itself and how
# someone looking for it would say it.
#
# Bidirectional by construction: each group's members expand to each other, so
# the direction of the gap never has to be predicted.

_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("task", "tasks", "todo", "todos", "kanban", "board", "project", "projects", "tracker"),
    ("mail", "email", "inbox", "smtp", "imap", "newsletter"),
    ("analytics", "analytic", "stats", "statistics", "metrics", "traffic", "insights", "reporting", "reports"),
    ("chat", "messaging", "messenger", "message", "messages"),
    ("ai", "llm", "gpt", "assistant", "agent", "agents", "automation", "automations"),
    ("storage", "files", "file", "drive", "bucket", "s3", "backup", "backups"),
    ("db", "database", "sql", "postgres", "postgresql", "mysql", "mariadb", "redis"),
    ("vpn", "wireguard", "tunnel", "network", "networking"),
    ("dns", "domain", "domains", "nameserver"),
    ("monitor", "monitoring", "uptime", "observability", "alerts", "alerting"),
    ("docs", "documentation", "wiki", "notes", "knowledge"),
    ("crm", "customers", "contacts", "leads", "sales"),
    ("billing", "invoice", "invoices", "payments", "payment", "subscription", "pricing"),
    ("calendar", "schedule", "scheduling", "events", "meetings"),
    ("social", "facebook", "instagram", "meta", "twitter", "x"),
    ("seo", "search", "ranking", "keywords"),
    ("git", "github", "gitlab", "repo", "repository", "repositories", "code"),
    ("security", "auth", "authentication", "sso", "oauth", "permissions", "rbac"),
    ("support", "helpdesk", "tickets", "ticketing"),
    ("cms", "blog", "wordpress", "website", "site", "pages"),
)


def _build_synonyms() -> dict[str, tuple[str, ...]]:
    m: dict[str, list[str]] = {}
    for group in _SYNONYM_GROUPS:
        for word in group:
            m.setdefault(word, []).extend(w for w in group if w != word)
    return {k: tuple(v) for k, v in m.items()}


_SYNONYMS = _build_synonyms()


def expand_synonyms(token: str) -> tuple[str, ...]:
    return _SYNONYMS.get(token, ())


# ── typo tolerance ────────────────────────────────────────────────────────
#
# Damerau-Levenshtein (edits + TRANSPOSITION) rather than plain Levenshtein,
# because the overwhelmingly common real-world typo is two swapped letters --
# "vikjuna", "anaytlics". Plain Levenshtein charges 2 for a swap and would
# reject exactly the mistakes people actually make.
#
# Bounded on purpose: the loop exits as soon as the whole row exceeds `max`,
# so a long word never costs a full matrix.


def edit_distance(a: str, b: str, max_d: int) -> int:
    """Damerau-Levenshtein, abandoning early once `max_d` is exceeded."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_d:
        return max_d + 1

    prev2: list[int] = []
    prev: list[int] = list(range(len(b) + 1))

    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        cur[0] = i
        row_min = cur[0]

        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            # transposition: "ab" -> "ba" costs 1, not 2
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v

        if row_min > max_d:
            return max_d + 1  # cannot recover: bail out early
        prev2 = prev
        prev = cur

    return prev[len(b)]


def typo_budget(length: int) -> int:
    """How many edits a word of this length may absorb and stay the same word.

    Tuned conservatively: precision beats generosity in a store. At three
    letters one edit is a DIFFERENT word ("dns" vs "dna"), so short words get
    no allowance at all.
    """
    if length <= 3:
        return 0
    if length <= 5:
        return 1
    return 2


# ── field weights ─────────────────────────────────────────────────────────
#
# A hit in a name is not a hit in a paragraph. These numbers encode one
# belief: the more deliberately a publisher had to choose a word, the more it
# means. A name is chosen once and agonised over; a description is prose, and
# every app in the store says "simple", "powerful" and "your data".
#
# The spread is wide on purpose. If description matches scored anywhere near
# name matches, a long-winded listing would outrank the app whose name you
# typed -- the single most common way store search loses trust.

WEIGHTS: dict[str, int] = {
    "name": 100,
    "id": 70,
    "keyword": 55,
    "tool": 45,
    "category": 30,
    "developer": 25,
    "summary": 22,
    "description": 9,
}

# How a token matched a field, as a multiplier on that field's weight.
MATCH_EXACT = 1.0     # the whole field, or a whole word in it
MATCH_PREFIX = 0.78   # "anal" -> "analytics": what live typing looks like
MATCH_INFIX = 0.42    # "lytic" -> "analytics": real, but much weaker
MATCH_FUZZY = 0.34    # within the typo budget
MATCH_SYNONYM = 0.6   # matched through the vocabulary map

# Where a TYPO may be forgiven: only in fields that are essentially NAMES a
# person types FROM MEMORY and can misremember.
#
# Not in prose. Measured on the live catalog: allowing edit distance inside
# summaries made "chart" match the word "chat" (distance 1) and answered a
# question about graphs with three messaging apps. A description is hundreds
# of words the user never typed, so every one is another chance to be
# accidentally within one keystroke of the query -- fuzzy matching over prose
# is not a feature with a tuning problem, it is noise with a probability.
#
# Not in `category` either, for a different reason: categories are not typed,
# they are clickable pills. Nobody misspells a button.
FUZZY_FIELDS = frozenset({"name", "id", "keyword", "tool", "developer"})

# Where a SYNONYM may be believed -- same argument, and it was the missing
# half of it. A synonym is an INFERENCE: the user typed one word, we search
# for a different one they did not type. Safe against a name; not against
# prose, where an expanded token eventually appears in almost any listing.
#
# Measured on the live catalog: "crm" returned Mail Client, Stripe Connector
# and WordPress Hub -- none of them a CRM -- because ['crm','customers',
# 'contacts','leads','sales'] expanded into their descriptions, where
# "customers" and "sales" are ordinary business words.
#
# `category` stays IN: a controlled vocabulary, not prose. `tool` is OUT --
# tool names are internal API identifiers, so expanding a typed word into a
# synonym and matching THAT against a function name stacks two inferences.
# Typing a tool name directly still works; this restricts inference only.
SYNONYM_FIELDS = frozenset({"name", "id", "keyword", "category"})


# ── scoring ───────────────────────────────────────────────────────────────


def _score_token(token: str, field_words: Sequence[str], field_text: str,
                 allow_fuzzy: bool) -> float:
    """Score ONE query token against ONE field's text.

    Returns the BEST evidence found, not the sum: a token that appears eight
    times in a description is not eight times more relevant, and rewarding
    repetition is how keyword-stuffed listings float to the top of a store.
    This is the anti-spam property, and it is structural rather than a filter.
    """
    if not token or not field_text:
        return 0.0

    # Whole-field or whole-word equality: the strongest signal there is.
    if field_text == token:
        return MATCH_EXACT
    if token in field_words:
        return MATCH_EXACT

    best = 0.0
    for w in field_words:
        if w.startswith(token):
            best = max(best, MATCH_PREFIX)
            continue
        if len(token) >= 3 and token in w:
            best = max(best, MATCH_INFIX)
    if best >= MATCH_PREFIX:
        return best

    # Acronyms: "ga" -> "Google Analytics", "wp" -> "WordPress Blogger".
    if len(token) >= 2 and len(field_words) >= 2:
        initials = "".join(w[0] for w in field_words if w)
        if initials == token:
            return MATCH_EXACT
        if initials.startswith(token):
            return MATCH_PREFIX

    # Typos, last: only when nothing cleaner matched, only within budget, and
    # only in name-like fields (see FUZZY_FIELDS).
    budget = typo_budget(len(token)) if allow_fuzzy else 0
    if budget > 0:
        for w in field_words:
            if abs(len(w) - len(token)) > budget:
                continue
            if edit_distance(token, w, budget) <= budget:
                best = max(best, MATCH_FUZZY)
                break

    return best


def _prepare(doc: dict[str, Any]) -> list[tuple[str, list[str], str]]:
    """Pre-split a document's fields once per search, not once per token."""
    out: list[tuple[str, list[str], str]] = []

    def push(field: str, raw: Any) -> None:
        text = normalize(str(raw or ""))
        if text:
            out.append((field, text.split(" "), text))

    push("name", doc.get("name"))
    push("id", doc.get("id"))
    push("category", doc.get("category"))
    push("developer", doc.get("developer"))
    push("summary", doc.get("summary"))
    # Long text is capped: past a few hundred characters a description is
    # boilerplate, and scanning all of it buys nothing.
    push("description", str(doc.get("description") or "")[:600])
    for k in doc.get("keywords") or []:
        push("keyword", k)
    for t in doc.get("tools") or []:
        push("tool", t)

    return out


def score_doc(doc: dict[str, Any], tokens: Sequence[str]) -> tuple[float, list[str], list[bool], list[float]]:
    """Score one document against one query's tokens.

    Returns (total, ordered_fields, hits, per_token) -- `per_token` is kept
    SEPARATE rather than summed because `search_docs` re-weights each token by
    how rare it is in the catalog, which is impossible once the contributions
    have been added together.
    """
    if not tokens:
        return 0.0, [], [], []

    fields = _prepare(doc)
    hit_fields: dict[str, float] = {}
    hits = [False] * len(tokens)
    per = [0.0] * len(tokens)
    total = 0.0
    covered = 0

    for ti, token in enumerate(tokens):
        best = 0.0
        best_field: str | None = None

        for field, words, text in fields:
            m = _score_token(token, words, text, field in FUZZY_FIELDS)
            if m > 0:
                v = m * WEIGHTS[field]
                if v > best:
                    best, best_field = v, field

        # Synonyms are tried ONLY when the token itself found nothing: they
        # are a safety net for vocabulary gaps, not a way to widen a good hit.
        if best <= 0:
            for syn in expand_synonyms(token):
                for field, words, text in fields:
                    if field not in SYNONYM_FIELDS:
                        continue
                    # allow_fuzzy=False: a synonym is already an inference.
                    # Allowing a TYPO of an inferred word stacks two guesses.
                    m = _score_token(syn, words, text, False)
                    if m > 0:
                        v = m * MATCH_SYNONYM * WEIGHTS[field]
                        if v > best:
                            best, best_field = v, field

        if best > 0 and best_field:
            total += best
            covered += 1
            hits[ti] = True
            per[ti] = best
            hit_fields[best_field] = max(hit_fields.get(best_field, 0.0), best)

    if covered == 0:
        return 0.0, [], hits, per

    ordered = [f for f, _ in sorted(hit_fields.items(), key=lambda kv: -kv[1])]
    return total, ordered, hits, per


def _quality_boost(doc: dict[str, Any]) -> float:
    """Tie-breaker only. Popularity must never outrank relevance -- that is
    how a store starts answering "what everyone installs" instead of "what you
    asked for" -- but between two equally good matches, the one 400 people use
    and rate 4.8 is the better suggestion. Deliberately tiny and logarithmic.
    """
    installs = math.log10(1 + max(0, int(doc.get("installs") or 0))) * 1.6
    rating = (float(doc.get("rating") or 0) - 3) * 0.8 if doc.get("rating") else 0.0
    featured = 1.2 if doc.get("featured") else 0.0
    return installs + rating + featured


def search_docs(docs: Sequence[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Rank documents against a query.

    An EMPTY query is not "no results": it is BROWSING, and the right order
    for browsing is quality, not alphabet. That is what lets one search box
    serve both a resting catalog view and an active search.

    WHY THE COVERAGE PENALTY LIVES HERE AND NOT IN `score_doc`
    ----------------------------------------------------------
    Multi-word queries should demand ALL their words: for "task tracker", an
    app matching only "task" is a weaker answer than one matching both. That
    rule is right, and applying it PER DOCUMENT made it wrong in one very
    common case.

    Measured on the live catalog: "todo list" failed to surface the task
    manager. Not because "todo" missed -- it hit through the synonym group --
    but because the word "list" appears in NO app, and every candidate was
    then penalised for missing a word that was impossible to match. The
    steepest penalty in the engine was firing for the CATALOG's vocabulary
    gap, not the document's.

    A single document cannot tell "I am missing this word" from "this word
    does not exist here". The corpus can. So: first ask which query words ANY
    document understood, then score coverage against only those.

    Returns a list of {"doc", "score", "fields"} dicts, best first.
    """
    tokens = tokenize(query)

    if not tokens:
        ranked = sorted(
            ((doc, _quality_boost(doc), i) for i, doc in enumerate(docs)),
            key=lambda t: (-t[1], t[2]),
        )
        return [{"doc": d, "score": s, "fields": []} for d, s, _ in ranked]

    # Pass 1: raw per-token scores, plus which tokens each document understood.
    raw = []
    for i, doc in enumerate(docs):
        total, fields, hits, per = score_doc(doc, tokens)
        raw.append({"doc": doc, "i": i, "score": total, "fields": fields,
                    "hits": hits, "per": per})

    # How many documents understood each query word. This is the corpus
    # knowledge a single document cannot have, and it answers two questions:
    #   df == 0  -> nobody knows this word; it must not penalise anyone.
    #   df high  -> everybody knows it; it barely narrows anything, so it must
    #               not outvote the rare word next to it.
    df = [0] * len(tokens)
    for r in raw:
        if r["score"] <= 0:
            continue
        for t in range(len(tokens)):
            if r["hits"][t]:
                df[t] += 1

    n = len(docs) or 1
    # log(1 + n/df) keeps this gentle: a word half the catalog knows still
    # counts, it just stops shouting over the word that identifies the intent.
    idf = [math.log(1 + n / d) if d > 0 else 0.0 for d in df]
    idf_total = sum(idf)

    out = []
    for r in raw:
        if r["score"] <= 0:
            continue
        weighted = 0.0
        got_idf = 0.0
        for t in range(len(tokens)):
            if not r["hits"][t]:
                continue
            weighted += r["per"][t] * idf[t]
            got_idf += idf[t]

        # Coverage measured in MEANING, not word count: missing the word that
        # carries the query costs far more than missing a filler word.
        coverage = (got_idf / idf_total) if idf_total > 0 else 1.0
        score = weighted * coverage * coverage + _quality_boost(r["doc"])
        if score > 0:
            out.append({"doc": r["doc"], "score": score,
                        "fields": r["fields"], "i": r["i"]})

    # Stable ties: equal scores keep input order rather than reshuffling.
    out.sort(key=lambda r: (-r["score"], r["i"]))
    return [{"doc": r["doc"], "score": r["score"], "fields": r["fields"]} for r in out]


# ── catalog adapter ───────────────────────────────────────────────────────


def to_doc(app: dict[str, Any]) -> dict[str, Any]:
    """Map ONE gateway catalog row onto the shape the engine reads.

    WHY AN ADAPTER RATHER THAN RENAMING THE ENGINE'S FIELDS. The engine is a
    copy of the Panel's, rule for rule, and it reads generic document fields
    (`name`, `summary`, `keywords`...). The gateway speaks its own catalog
    vocabulary (`display_name`, `short_description`, `tags`...). Translating
    once, here, keeps the two rankers textually comparable -- the moment the
    engine starts reading gateway-specific keys, verifying that the launcher
    and this app still agree becomes a manual diff of two dialects.

    Silently reading a key that does not exist is the failure mode this
    guards against: the engine would rank every app on an empty document and
    return confident nonsense.
    """
    tags = app.get("tags") or []
    if isinstance(tags, str):
        # Some rows carry tags as a comma-joined string rather than a list.
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    return {
        "id": app.get("app_id") or "",
        "name": app.get("display_name") or app.get("name") or "",
        "keywords": list(tags),
        "category": app.get("category") or "",
        "summary": app.get("short_description") or "",
        "description": app.get("long_description") or app.get("description") or "",
        "tools": app.get("tool_names") or [],
        "developer": (
            app.get("developer_nickname")
            or app.get("developer_name")
            or ""
        ),
        "installs": app.get("install_count") or 0,
        "rating": app.get("avg_rating") or 0,
        "featured": bool(app.get("featured")),
        "installed": bool(app.get("is_installed")),
        # Carried through so the caller can return the ORIGINAL row untouched
        # rather than reconstructing it from the projection.
        "_row": app,
    }


def rank_apps(apps: Sequence[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Rank raw gateway catalog rows, returning the ORIGINAL rows in order.

    The engine's document shape never leaks out of this module: callers get
    their own rows back, so projections, panels and tools keep working
    unchanged.
    """
    docs = [to_doc(a) for a in apps]
    return [r["doc"]["_row"] for r in search_docs(docs, query)]
