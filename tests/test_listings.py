"""Pins + parsing for the active-listings fetch.

Fixtures here are SYNTHETIC but mirror the real field nesting exactly (they
are built from the recording's field names — node.first_listing.id,
formatted_price.text, mi_vh_item_verification_state.is_approved,
listing_is_rejected — never copied from the redacted dump bytes).
Live proof is a real run against /marketplace/you/selling/.
"""
import asyncio
import json

import pytest

from poster.config import Config
from poster.listings import (
    DOC_ID_LISTINGS,
    FETCH_PAGE_JS,
    FRIENDLY_NAME,
    PER_PAGE,
    RELAY_PROVIDER_VAR,
    ListingsFetchError,
    extract_active_listings,
    fetch_active_listings,
    parse_graphql_body,
    snapshot_path,
    strip_anti_json,
)

TAHOE = "1923574311937261"        # real: '2019 Chevrolet Tahoe LT' $340.000
SECOND = "1612551696975361"       # real: the account's other active listing


def _listing(lid, title, price, *, approved=True, rejected=False,
             state=True, price_key="text"):
    item = {
        "__typename": "GroupCommerceProductItem",
        "__isMarketplaceListingRenderable": "GroupCommerceProductItem",
        "id": lid,
        "marketplace_listing_title": title,
        "listing_is_rejected": rejected,
        "is_viewer_seller": True,
        "primary_listing_photo": {"__typename": "ProductImage", "id": "2600796380345528"},
    }
    if price is not None:
        item["formatted_price"] = {"__typename": "TextWithEntities", price_key: price}
    if state:
        item["mi_vh_item_verification_state"] = {
            "__typename": "MiVhItemEvaluationResults", "is_approved": approved}
    return item


def _set_node(item):
    """A real edge node: the SET id is NOT the listing id (regression guard)."""
    return {"node": {"__typename": "MarketplaceIndexedListingSet",
                     "id": "9" + item["id"], "first_listing": item}}


def _payload(items, *, is_empty=False, final=True):
    sets = {"edges": [_set_node(i) for i in items], "is_empty": is_empty}
    return json.dumps({"data": {"viewer": {
        "inactive_sets": {"is_empty": True},
        "marketplace_listing_sets": sets,
    }}, "extensions": {"is_final": final}})


def _defer(path, data, final=False):
    return json.dumps({"label": "listings$defer$page_info", "path": path,
                       "data": data, "extensions": {"is_final": final}})


def _multipart(items, cursor, has_next, *, is_empty=False):
    """First chunk + the deferred page_info chunk the recording shows (the
    paths are root-relative: ['viewer','marketplace_listing_sets'])."""
    body = _payload(items, is_empty=is_empty) + "\r\n"
    body += _defer(["viewer", "inactive_listing_sets"],
                   {"page_info": {"end_cursor": None, "has_next_page": False}})
    body += "\r\n" + _defer(["viewer", "marketplace_listing_sets"],
                            {"page_info": {"end_cursor": cursor,
                                           "has_next_page": has_next}}, final=True)
    return body


class FakePage:
    """Duck-typed Page: token probe + one queued graphql reply per evaluate."""

    def __init__(self, replies, url="https://www.facebook.com/marketplace/you/selling/"):
        self.replies = list(replies)
        self.url = url
        self.calls: list[dict] = []
        self.gotos: list[str] = []

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        self.url = url

    async def evaluate(self, js, arg=None):
        if arg is None:  # TOKEN_PROBE_JS
            return {"dtsg": "dtsg-token", "lsd": "lsd-token"}
        self.calls.append(arg)
        return self.replies.pop(0)

    async def wait_for_timeout(self, ms):
        return None


def _cfg(tmp_path):
    return Config(screenshot_dir=tmp_path / "shots", post_as="profile",
                  fb_main_user="61592579496197")


# ---- pins ------------------------------------------------------------------

def test_doc_id_and_friendly_pinned_to_recording():
    # recording 20260923T021450Z — if these change, re-verify against a fresh
    # recording (or a live sniff of FB's own request), don't blind-edit
    assert DOC_ID_LISTINGS == "27658604553835653"
    assert FRIENDLY_NAME == "CometMarketplaceYouSellingFastContentContainerQuery"
    assert PER_PAGE == 5
    #: FB REQUIRES the relay provider variable [live 2026-09-23: without it
    #: the answer is 'missing_required_variable_value']; its name comes
    #: VERBATIM from the unscrubbed trace postData of the recording.
    assert RELAY_PROVIDER_VAR == ("__relay_internal__pv__ShouldUpdate"
                                  "MarketplaceBoostListingBoostedStatus"
                                  "relayprovider")
    assert "[relayPv]: false" in FETCH_PAGE_JS


# ---- body parsing ----------------------------------------------------------

def test_anti_json_prefixes_are_stripped():
    assert strip_anti_json("for(;;);{\"a\":1}") == '{"a":1}'
    assert strip_anti_json(")]}'\n{\"a\":1}") == '{"a":1}'
    assert strip_anti_json('{"a":1}') == '{"a":1}'


@pytest.mark.parametrize("prefix", ["", "for(;;);", ")]}'\n"])
def test_single_document_body_parses(prefix):
    payload = parse_graphql_body(prefix + _payload([_listing(TAHOE, "T", "$1")]))
    rows, info = extract_active_listings(payload)
    assert [r["id"] for r in rows] == [TAHOE]
    assert info["has_next"] is False


def test_incremental_parts_are_merged_so_page_info_survives():
    payload = parse_graphql_body(_multipart([_listing(TAHOE, "T", "$1")],
                                            "CURSOR1", True))
    rows, info = extract_active_listings(payload)
    assert [r["id"] for r in rows] == [TAHOE]
    assert info == {"cursor": "CURSOR1", "has_next": True, "is_empty": False}


def test_unparsable_body_raises():
    with pytest.raises(ListingsFetchError):
        parse_graphql_body("for(;;);<html>nope</html>")
    with pytest.raises(ListingsFetchError):
        parse_graphql_body("   ")


# ---- extraction ------------------------------------------------------------

def test_fields_and_order_match_the_recording_shape():
    payload = parse_graphql_body(_payload([
        _listing(TAHOE, "2019 Chevrolet Tahoe LT", "$340.000"),
        _listing(SECOND, "2014 Jeep Gran Cherokee summi", "$185.000", approved=False),
    ]))
    rows, _ = extract_active_listings(payload)
    assert rows == [
        {"id": TAHOE, "title": "2019 Chevrolet Tahoe LT", "price": "$340.000",
         "approved": True, "rejected": False},
        {"id": SECOND, "title": "2014 Jeep Gran Cherokee summi", "price": "$185.000",
         "approved": False, "rejected": False},
    ]
    # the SET id must never be mistaken for a listing id
    assert all(not r["id"].startswith("9" + TAHOE[:1]) for r in rows)


def test_missing_verification_fields_stay_none():
    payload = parse_graphql_body(_payload([
        _listing(TAHOE, "T", "$1", state=False)]))
    rows, _ = extract_active_listings(payload)
    assert rows[0]["approved"] is None
    assert rows[0]["rejected"] is False
    item = _listing(SECOND, "T", None)
    del item["listing_is_rejected"]
    payload = parse_graphql_body(_payload([item]))
    rows, _ = extract_active_listings(payload)
    assert rows[0]["rejected"] is None
    assert rows[0]["price"] == ""


def test_price_fallbacks_and_node_level_listings():
    item = _listing(TAHOE, "T", "$9", price_key="formatted_amount")
    payload = parse_graphql_body(_payload([item]))
    assert extract_active_listings(payload)[0][0]["price"] == "$9"
    # tolerant walk: an edge whose node IS the listing (no first_listing)
    body = json.dumps({"data": {"viewer": {"marketplace_listing_sets": {"edges": [
        {"node": _listing(SECOND, "Direct node", "$7")}]}}}})
    rows, _ = extract_active_listings(parse_graphql_body(body))
    assert [r["id"] for r in rows] == [SECOND]


def test_wrong_shape_yields_no_listings_without_raising():
    rows, info = extract_active_listings({"data": {"viewer": {"all_joined_groups": {}}}})
    assert rows == [] and info["has_next"] is False


# ---- fetch loop ------------------------------------------------------------

def test_fetch_paginates_dedupes_and_saves_snapshot(tmp_path):
    page = FakePage([
        {"status": 200, "text": _multipart([_listing(TAHOE, "Tahoe", "$340.000")],
                                           "CURSOR1", True)},
        {"status": 200, "text": _multipart([_listing(TAHOE, "Tahoe", "$340.000"),
                                            _listing(SECOND, "Jeep", "$185.000")],
                                           None, False)},
    ])
    cfg = _cfg(tmp_path)
    logs: list[str] = []
    rows = asyncio.run(fetch_active_listings(page, cfg, logs.append))
    assert [r["id"] for r in rows] == [TAHOE, SECOND]   # FB order, deduped
    # page 2 re-issued the SAME doc with the cursor from the deferred part
    # (page 1 sends cursor=None -> the JS omits the field, like the recording)
    assert len(page.calls) == 2
    assert page.calls[0]["cursor"] is None
    assert page.calls[1]["cursor"] == "CURSOR1"
    assert page.calls[0]["docId"] == DOC_ID_LISTINGS
    assert page.calls[1]["friendly"] == FRIENDLY_NAME
    assert page.calls[0]["av"] == "61592579496197"
    saved = json.loads(snapshot_path(cfg).read_text(encoding="utf-8"))
    assert saved == rows
    assert any("2 active listing(s)" in m for m in logs)


def test_fetch_without_av_still_replays(tmp_path):
    page = FakePage([{"status": 200, "text": _multipart([], None, False, is_empty=True)}])
    bare = Config(screenshot_dir=tmp_path / "shots", post_as="profile")
    rows = asyncio.run(fetch_active_listings(page, bare, None))
    assert rows == [] and page.calls[0]["av"] == ""


def test_confirmed_zero_is_not_an_error(tmp_path):
    page = FakePage([{"status": 200, "text": _multipart([], None, False, is_empty=True)}])
    logs: list[str] = []
    assert asyncio.run(fetch_active_listings(page, _cfg(tmp_path), logs.append)) == []
    assert any("confirmed 0" in m for m in logs)


def test_first_page_failure_raises(tmp_path):
    page = FakePage([{"error": "fetch threw: TypeError"}])
    with pytest.raises(ListingsFetchError):
        asyncio.run(fetch_active_listings(page, _cfg(tmp_path), None))
    page = FakePage([{"status": 500, "text": "for(;;);<html>login</html>"}])
    with pytest.raises(ListingsFetchError):
        asyncio.run(fetch_active_listings(page, _cfg(tmp_path), None))


def test_first_page_graphql_error_raises(tmp_path):
    body = json.dumps({"errors": [{"message": "You are not logged in"}],
                       "data": {}})
    page = FakePage([{"status": 200, "text": body}])
    with pytest.raises(ListingsFetchError):
        asyncio.run(fetch_active_listings(page, _cfg(tmp_path), None))


def test_later_page_failure_returns_partial(tmp_path):
    page = FakePage([
        {"status": 200, "text": _multipart([_listing(TAHOE, "Tahoe", "$1")],
                                           "CURSOR1", True)},
        {"error": "fetch threw: aborted"},
    ])
    logs: list[str] = []
    rows = asyncio.run(fetch_active_listings(page, _cfg(tmp_path), logs.append))
    assert [r["id"] for r in rows] == [TAHOE]
    assert any("page 2 failed" in m for m in logs)


def test_missing_dtsg_raises(tmp_path, monkeypatch):
    async def no_tokens(page, timeout_ms=20000):
        return {"dtsg": None, "lsd": None}

    monkeypatch.setattr("poster.listings._wait_tokens", no_tokens)
    with pytest.raises(ListingsFetchError):
        asyncio.run(fetch_active_listings(FakePage([]), _cfg(tmp_path), None))


def test_cursor_loop_is_capped(tmp_path):
    replies = []
    for i in range(PER_PAGE):  # always has_next, always one new listing
        replies.append({"status": 200, "text": _multipart(
            [_listing(f"10000000000000{i}", f"car {i}", "$1")],
            f"CURSOR{i + 1}", True)})
    page = FakePage(replies)
    logs: list[str] = []
    rows = asyncio.run(fetch_active_listings(page, _cfg(tmp_path), logs.append,
                                             max_pages=3))
    assert len(rows) == 3 and len(page.calls) == 3
    assert any("page cap 3 reached" in m for m in logs)


def test_non_advancing_cursor_stops_the_loop(tmp_path):
    same = _multipart([_listing(TAHOE, "Tahoe", "$1"), _listing(SECOND, "J", "$2")],
                      "SAME", True)
    page = FakePage([{"status": 200, "text": same}, {"status": 200, "text": same}])
    rows = asyncio.run(fetch_active_listings(page, _cfg(tmp_path), None))
    assert [r["id"] for r in rows] == [TAHOE, SECOND]
    assert len(page.calls) == 2  # stopped: page 2 added nothing


def test_navigates_to_the_selling_route_first(tmp_path):
    page = FakePage([{"status": 200, "text": _multipart([], None, False)}],
                    url="https://www.facebook.com/")
    asyncio.run(fetch_active_listings(page, _cfg(tmp_path), None))
    assert page.gotos == ["https://www.facebook.com/marketplace/you/selling/"]
