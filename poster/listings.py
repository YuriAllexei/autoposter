"""ACTIVE marketplace listings of the ACTING identity (id, title, price).

Ground truth: recording 20260923T021450Z_marketplace_article_fetching_and_mass_pu
(manual session on https://www.facebook.com/marketplace/you/selling/ — the
FB Marketplace "Tus anuncios / you selling" surface). That page's own feed is
ONE authenticated GraphQL query:

    POST https://www.facebook.com/api/graphql/
    doc_id = 27658604553835653
    fb_api_req_friendly_name = CometMarketplaceYouSellingFastContentContainerQuery
    variables = {"count":5,"order":"CREATION_TIMESTAMP_DESC","scale":1,
                 "shouldDeferInfoSection":false,"state":null,"title_search":null}
    (+ "cursor": <end_cursor> — the operation declares `cursor`; see below)

    response: data.viewer.marketplace_listing_sets
                .edges[].node { __typename: MarketplaceIndexedListingSet,
                                id: <SET id, NOT a listing id>,
                                first_listing { id: <LISTING id>,
                                                marketplace_listing_title,
                                                formatted_price.text,
                                                mi_vh_item_verification_state
                                                  .is_approved,
                                                listing_is_rejected } }
                .page_info { end_cursor, has_next_page }

    NOTE on ids: edges[].node.id is a MarketplaceIndexedListingSet id (e.g.
    1107451905593250) and `post_id`/`product_item_override.id` (e.g.
    28736573965937248) is the *post/product-item* id — NEITHER is the listing
    id. Only node.first_listing.id (1923574311937261 = '2019 Chevrolet Tahoe
    LT') is the marketplace listing id; that is what we return.

Implementation: we run fetch() INSIDE the logged-in page so cookies and FB's
own anti-CSRF tokens (DTSGInitialData/LSD; jazoest = "2"+sum(charCodes(dtsg)))
come from the page itself — nothing secret is read, copied or logged. The
response is parsed back in Python (tests cover the shapes) and the cursor loop
mirrors what the page's own infinite scroll does.

Two response quirks this module handles (both proven by the recording):
  * anti-JSON prefixes (`for(;;);`, `)]}'`) — stripped defensively;
  * the reply is NOT one JSON document: the first chunk is the query result
    and `page_info` arrives LATER as a newline-separated `@defer`/`@stream`
    incremental payload (`{"label":…,"path":["viewer",
    "marketplace_listing_sets"],"data":{"page_info":{…}}}`) — paths are
    relative to the root `data`. Without merging those parts there is no
    cursor at all (the recording's first chunk carries none).

LIVE-VERIFIED [2026-09-23, Firefox headed, persistent profile]: the replay
above returned 1 active listing — id 1923574311937261, '2019 Chevrolet Tahoe
LT', $340.000, approved=True, rejected=False — with has_next_page=false for
both count=5 and count=50. The account really has ONE active listing: the
other big ids in the dump are edges[].node.id (the listing-SET id
1107451905593250) and post_id / product_item_override.id (28736573965937248),
neither of which is a marketplace listing id.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from playwright.async_api import Page

from .config import Config

# ---- pinned provenance (re-verify against a fresh recording, never blind-edit)

# the "you selling" active-listings feed [recording 20260923T021450Z]
DOC_ID_LISTINGS = "27658604553835653"
FRIENDLY_NAME = "CometMarketplaceYouSellingFastContentContainerQuery"
GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
SELLING_URL = "https://www.facebook.com/marketplace/you/selling/"

#: `count` and `order` are the recorded page-1 values; the operation also
#: declares `cursor` (argumentDefinitions: count, cursor, order, scale, state,
#: status, title_search — read out of the Comet JS bundle in the dump), so
#: page 2+ re-issues the SAME doc with `cursor` = page_info.end_cursor. The
#: recording itself ended at has_next_page=false, so only the terminator is
#: live-proven; the loop below stops on any cursor that fails to advance.
#: [live 2026-09-23] count=50 is accepted too and returned the same single
#: listing (has_next_page=false), so raising this is safe when needed.
PER_PAGE = 5
ORDER = "CREATION_TIMESTAMP_DESC"

#: `shouldDeferInfoSection` is CLIENT-computed in the Comet bundle as
#: `gkx('22986') && ViewportDimensionsServerGuess.width_px <= 1267` — false
#: for any viewport wider than 1267px (ours is 1280), which is exactly what
#: the recording sent. Pinned so the replay is byte-faithful to the recording.
SHOULD_DEFER_INFO_SECTION = False
#: exact trace-recovered name (see provenance note above)
RELAY_PROVIDER_VAR = ("__relay_internal__pv__ShouldUpdateMarketplace"
                      "BoostListingBoostedStatusrelayprovider")

#: The recorded request ALSO carried one relay-internal provided variable.
#: The JSONL recorder scrubs every >=48-char token run (that is why the
#: friendly name looked redacted there) — but the RAW Playwright trace keeps
#: postData.params unscrubbed, so this name is recovered VERBATIM from
#: trace.zip (trace.network snapshot -> postData params of doc
#: 27658604553835653), NOT invented. [live 2026-09-23] sending the request
#: WITHOUT it fails with 'A server error missing_required_variable_value'
#: (CRITICAL severity) — FB requires the variable, so it rides along as
#: recorded value false.

MAX_PAGES = 25  # cursor-loop guard: 25*5 = 125 listings ceiling


class ActiveListing(TypedDict):
    id: str
    title: str
    price: str
    # True/False when the response carries the field, None when this entry
    # simply has no verification/integrity field (never guessed as approved)
    approved: bool | None
    rejected: bool | None


class ListingsFetchError(RuntimeError):
    pass


# Runs in the page context: ONE graphql page of the listings feed. The cursor
# loop lives in Python (parse_graphql_body is unit-tested against fixtures).
# Token resolution mirrors poster/groups_fetch.py: require('DTSGInitialData'/
# 'LSD').token, then the window global, then the boot-load HTML regex.
TOKEN_PROBE_JS = r"""
() => {
  const grab = (mod, re) => {
    try { const m = window.require && window.require(mod);
          if (m && m.token) return m.token; } catch (e) {}
    try { const g = window[mod]; if (g && g.token) return g.token; } catch (e) {}
    const mm = (document.documentElement.innerHTML || '').match(re);
    return mm ? mm[1] : null;
  };
  return {
    dtsg: grab('DTSGInitialData',
      /"DTSGInitialData",\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"/),
    lsd: grab('LSD',
      /"LSD",\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"/),
  };
}
"""

FETCH_PAGE_JS = r"""
async ({ av, docId, friendly, count, order, deferInfo, relayPv, cursor, tokens }) => {
  const { dtsg, lsd } = tokens;
  let jazo = 0; for (const ch of dtsg) jazo += ch.charCodeAt(0);
  jazo = '2' + jazo;
  const variables = {
    count, order, scale: 1,
    shouldDeferInfoSection: deferInfo,
    state: null, title_search: null,
    [relayPv]: false,   // recorded byte-faithful; [live] FB REQUIRES it
  };
  if (cursor) variables.cursor = cursor;
  const form = new URLSearchParams({
    __a: '1', fb_dtsg: dtsg, jazoest: jazo, lsd,
    fb_api_caller_class: 'RelayModern',
    fb_api_req_friendly_name: friendly,
    server_timestamps: 'true',
    __comet_req: '15',
    doc_id: docId,
    variables: JSON.stringify(variables),
  });
  if (av) form.set('av', av);   // acting identity; absent -> session's own actor
  let resp;
  try {
    resp = await fetch('https://www.facebook.com/api/graphql/', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: form.toString(),
    });
  } catch (e) {
    return { error: 'fetch threw: ' + (e && e.message ? e.message : e) };
  }
  let text = '';
  try { text = await resp.text(); }
  catch (e) { return { error: 'body unreadable (HTTP ' + resp.status + ')' }; }
  return { status: resp.status, text };
}
"""

#: FB prefixes graphql bodies with a JS guard loop now and then.
ANTI_JSON_PREFIXES = ("for(;;);", "while(1);", ")]}'", ")]}")

_DECODER = json.JSONDecoder()


def strip_anti_json(text: str) -> str:
    """Drop FB's anti-JSON guard prefix (`for(;;);`, `)]}'`, ...)."""
    stripped = (text or "").lstrip()
    for prefix in ANTI_JSON_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].lstrip()
            break
    return stripped


def _merge_into(target: Any, data: Any) -> Any:
    """Deep-merge `data` into `target` (incremental parts ADD fields — the
    first chunk's edges must survive the deferred page_info part)."""
    if isinstance(target, dict) and isinstance(data, dict):
        for key, value in data.items():
            target[key] = _merge_into(target.get(key), value)
        return target
    return data


def _set_path(root: dict, path: list, data: Any) -> None:
    """Merge an incremental payload's `data` at its (root-relative) `path`."""
    node = root
    for key in path[:-1]:
        nxt = node.setdefault(key, {})
        if not isinstance(nxt, dict):
            return  # conflicting shape — give up on this part
        node = nxt
    node[path[-1]] = _merge_into(node.get(path[-1]), data)


def parse_graphql_body(text: str) -> dict:
    """Parse a graphql reply: one JSON document, or FB's newline-separated
    `@defer`/`@stream` incremental payloads merged into one dict.

    Raises ListingsFetchError when nothing parseable is there (callers must
    treat that as a FAILED fetch, never as '0 listings').
    """
    body = strip_anti_json(text)
    if not body.strip():
        raise ListingsFetchError("empty graphql response body")
    merged: dict = {}
    index, parts = 0, 0
    while index < len(body):
        while index < len(body) and body[index].isspace():
            index += 1
        if index >= len(body):
            break
        try:
            part, index = _DECODER.raw_decode(body, index)
        except ValueError as exc:
            raise ListingsFetchError(
                f"unparsable graphql body at char {index}: {body[index:index + 140]!r}"
            ) from exc
        parts += 1
        if isinstance(part, dict):
            if isinstance(part.get("path"), list) and "path" in part:
                _set_path(merged, ["data", *part["path"]], part.get("data"))
            elif isinstance(part.get("data"), dict):
                merged["data"] = _merge_into(merged.get("data"), part["data"])
            for key, value in part.items():
                if key not in {"data", "path", "label"}:
                    merged[key] = value
    if not parts:
        raise ListingsFetchError("graphql body held no JSON payload")
    return merged


def _iter_dicts(node: Any):
    """Yield every dict inside a nested payload (tolerant response walking)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_dicts(value)


def _dig(payload: dict, path: tuple[str, ...]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _listing_connection(payload: dict) -> dict | None:
    """The `marketplace_listing_sets` connection, by path first, then by shape
    (FB moves these edges around between doc versions — hunt for the one whose
    node carries a listing)."""
    known = _dig(payload, ("data", "viewer", "marketplace_listing_sets"))
    if isinstance(known, dict) and ("edges" in known or "page_info" in known):
        return known
    for node in _iter_dicts(payload.get("data", payload)):
        edges = node.get("edges")
        if not isinstance(edges, list):
            continue
        first = edges[0].get("node") if edges and isinstance(edges[0], dict) else None
        if isinstance(first, dict) and (
            "first_listing" in first or "marketplace_listing_title" in first
        ):
            return node
    return None


def _page_info(payload: dict, connection: dict | None) -> dict:
    """`{end_cursor, has_next_page}` — normally a deferred payload merged into
    the connection, so look there first and then anywhere in the tree."""
    if isinstance(connection, dict) and isinstance(connection.get("page_info"), dict):
        return connection["page_info"]
    fallback: dict = {}
    for node in _iter_dicts(payload.get("data", payload)):
        info = node.get("page_info")
        if isinstance(info, dict) and ("has_next_page" in info or "end_cursor" in info):
            fallback = info
    return fallback


def _price_text(listing: dict) -> str:
    formatted = listing.get("formatted_price")
    if isinstance(formatted, dict):
        for key in ("text", "formatted_amount", "amount"):
            if formatted.get(key):
                return str(formatted[key])
    return ""


def _truthy_field(container: dict, key: str) -> bool | None:
    return bool(container[key]) if key in container else None


def extract_active_listings(payload: dict) -> tuple[list[ActiveListing], dict]:
    """(listings in FB order, {cursor, has_next, is_empty}) from a merged reply.

    Only `node.first_listing` counts as a listing: `node.id` is the
    MarketplaceIndexedListingSet id and must never leak out as a listing id.
    """
    connection = _listing_connection(payload)
    listings: list[ActiveListing] = []
    if connection is None:
        return listings, {"cursor": None, "has_next": False, "is_empty": None}
    for edge in connection.get("edges") or []:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        listing = node.get("first_listing")
        if not isinstance(listing, dict):
            # some feeds hand the listing straight to the node
            listing = node if "marketplace_listing_title" in node else None
        if not isinstance(listing, dict) or listing.get("id") is None:
            continue
        state = listing.get("mi_vh_item_verification_state")
        approved = (_truthy_field(state, "is_approved")
                    if isinstance(state, dict) else None)
        listings.append(ActiveListing(
            id=str(listing["id"]),
            title=str(listing.get("marketplace_listing_title") or ""),
            price=_price_text(listing),
            approved=approved,
            rejected=_truthy_field(listing, "listing_is_rejected"),
        ))
    info = _page_info(payload, connection)
    return listings, {
        "cursor": info.get("end_cursor") or None,
        "has_next": bool(info.get("has_next_page")),
        "is_empty": connection.get("is_empty"),
    }


def snapshot_path(cfg: Config) -> Path:
    """Where the LATEST snapshot lands (the GUI reads this fixed path); the
    timestamped sibling keeps the history, like --list-groups does."""
    return cfg.screenshot_dir.parent / "cache" / "listings.json"


def save_listings_snapshot(cfg: Config, listings: list[ActiveListing],
                           log: Callable[[str], None] | None = None) -> Path:
    """Write the snapshot as JSON (same shape as the joins snapshot: a bare
    list). Written on EVERY fetch so the GUI never shows stale news."""
    out = snapshot_path(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(listings, indent=2, ensure_ascii=False)
    out.write_text(text, encoding="utf-8")
    stamped = out.with_name(
        f"listings_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json")
    stamped.write_text(text, encoding="utf-8")
    if log:
        log(f"listings: snapshot saved -> {out}")
    return out


async def _wait_tokens(page: Page, timeout_ms: int = 20000) -> dict:
    """Poll TOKEN_PROBE_JS until the anti-CSRF tokens resolve (boot-load pages
    take a moment to render them inline). Returns {dtsg, lsd}."""
    import time
    deadline = time.time() + timeout_ms / 1000
    last: dict = {"dtsg": None, "lsd": None}
    while time.time() < deadline:
        try:
            last = await page.evaluate(TOKEN_PROBE_JS) or last
        except Exception:  # noqa: BLE001, S110 - evaluate can throw mid-
            pass  # navigation; retry until deadline, then ListingsFetchError
        if last.get("dtsg"):
            return last
        await page.wait_for_timeout(700)
    return last


async def fetch_active_listings(page: Page, cfg: Config,
                                log: Callable[[str], None] | None = None,
                                max_pages: int = MAX_PAGES) -> list[ActiveListing]:
    """Every ACTIVE listing of the acting identity, in FB's own order.

    Raises ListingsFetchError when the FIRST page cannot be fetched/parsed
    (not logged in, FB changed the doc, unparsable body) — callers must treat
    that as a broken fetch, never as 'this account has 0 listings'. A page
    that parses fine with zero edges IS a confirmed 0 and returns [].
    Later-page failures are logged and return what was already collected.
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    av = (cfg.identity_id or "").strip()
    if not av:
        _log("listings: no identity id configured — sending no av "
             "(FB will use the session's own actor)")

    if "/marketplace/you/selling" not in (page.url or ""):
        await page.goto(SELLING_URL, wait_until="domcontentloaded", timeout=60000)
    tokens = await _wait_tokens(page)
    if not tokens.get("dtsg"):
        raise ListingsFetchError(
            f"DTSG token never appeared at {page.url!r} — not logged in, or "
            "FB changed its boot globals")

    listings: list[ActiveListing] = []
    seen: set[str] = set()
    cursor: str | None = None
    pages = 0
    info: dict = {}
    while pages < max_pages:
        pages += 1
        res = await page.evaluate(FETCH_PAGE_JS, {
            "av": av, "docId": DOC_ID_LISTINGS, "friendly": FRIENDLY_NAME,
            "count": PER_PAGE, "order": ORDER,
            "deferInfo": SHOULD_DEFER_INFO_SECTION,
            "relayPv": RELAY_PROVIDER_VAR, "cursor": cursor,
            "tokens": {"dtsg": tokens["dtsg"], "lsd": tokens.get("lsd") or ""}})
        if not isinstance(res, dict) or "error" in res:
            message = (res or {}).get("error") if isinstance(res, dict) else res
            if pages == 1:
                raise ListingsFetchError(f"listings fetch failed: {message}")
            _log(f"listings: page {pages} failed ({message}) — "
                 f"returning {len(listings)} listing(s) from page(s) 1-{pages - 1}")
            break
        try:
            payload = parse_graphql_body(res.get("text") or "")
        except ListingsFetchError:
            if pages == 1:
                raise
            _log(f"listings: page {pages} unparsable — returning "
                 f"{len(listings)} listing(s) from page(s) 1-{pages - 1}")
            break
        errors = payload.get("errors")
        if errors and _listing_connection(payload) is None:
            brief = json.dumps(errors)[:200]
            if pages == 1:
                raise ListingsFetchError(f"graphql error: {brief}")
            _log(f"listings: page {pages} graphql error: {brief}")
            break
        rows, info = extract_active_listings(payload)
        added = 0
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                listings.append(row)
                added += 1
        previous, cursor, has_next = cursor, info["cursor"], info["has_next"]
        _log(f"listings: page {pages} -> {added} new ({len(listings)} total), "
             f"has_next={has_next}")
        # real stop signal: a page that adds nothing means the cursor is not
        # advancing (same guard as the joins fetch) — never spin to max_pages
        if not added or not has_next or not cursor or cursor == previous:
            break

    if pages >= max_pages and info.get("has_next"):
        _log(f"listings: page cap {max_pages} reached with has_next=True — "
             "more listings may exist")

    if not listings:
        empty = info.get("is_empty")
        _log("listings: confirmed 0 active listings"
             + (f" (marketplace_listing_sets.is_empty={empty})" if empty is not None
                else " (feed parsed, no edges)"))
    else:
        _log(f"listings: {len(listings)} active listing(s) over {pages} page(s) "
             f"for av={av or '<session actor>'}")
    save_listings_snapshot(cfg, listings, log)
    return listings


# ---------------------------------------------------------------------------
#: CLI: `poetry run python -m poster.listings` — READ-ONLY: adopt identity,
#: fetch the ACTIVE listings, print a table, refresh the snapshot the
#: dashboard's listings tile reads. Opens no dialogs, clicks nothing.
#: rc: 0 ok (a confirmed 0 listings is ok) · 1 fetch failed · 2 not logged
#: in · 3 identity switch failed.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from playwright.async_api import async_playwright

    from .config import load_config
    from .fb import adopt_identity, launch, make_log

    ap = argparse.ArgumentParser(
        prog="python -m poster.listings",
        description="Fetch ACTIVE marketplace listings of the posting "
                    "identity and refresh the dashboard snapshot "
                    "(read-only; nothing is published or clicked).")
    ap.add_argument("--env-file", default=None,
                    help="alternate .env path (or set AP_ENV_FILE)")
    args = ap.parse_args(argv)

    cfg = load_config(env_file=args.env_file)
    log = make_log()

    async def run() -> int:
        async with async_playwright() as p:
            ctx, page = await launch(cfg, p)
            try:
                state = await adopt_identity(ctx, page, cfg, log)
                if state == "login":
                    log("abort: not logged in")
                    return 2
                if state == "identity":
                    log("abort: identity switch failed")
                    return 3
                try:
                    listings = await fetch_active_listings(page, cfg, log=log)
                except ListingsFetchError as e:
                    log(f"abort: listings fetch failed: {e}")
                    return 1
                log(f"ACTIVE LISTINGS for {cfg.identity_label!r} "
                    f"({cfg.post_as}):")
                for lst in listings:
                    log(f"  {lst.get('id') or '?':>20}  "
                        f"{str(lst.get('title') or '')[:48]:<48} "
                        f"{lst.get('price') or ''}")
                if not listings:
                    log("  (confirmed 0 active listings)")
                return 0
            finally:
                await ctx.close()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
