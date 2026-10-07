"""Tests for the dashboard's render-ready groups-matrix data.

`poster/gui/state.py` turns the committed `poster.groups_matrix` store into the
exact shape the already-shipped page expects: a top-level `state["matrix"]`
summary plus a `row["matrix"]` checkbox view on every marketplace listing. The
FOLDING happens here, server-side, so the browser never reproduces Python's
`casefold` (page.py ships no folding of its own).

Fixtures are synthetic tmp trees (same style as tests/test_gui.py): no browser,
no network, no real .local-capture artifacts.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from poster import groups_matrix as gm
from poster.gui.runner import RingBuffer
from poster.gui.server import create_server
from poster.gui.state import (
    GuiPaths,
    build_state,
    matrix_rows_for,
    read_matrix_view,
)

L1 = "1923574311937261"
L2 = "1923574311937262"


# --------------------------------------------------------------------------
# fixtures on disk
# --------------------------------------------------------------------------

def write_groups_snapshot(root: Path, stamp: str, rows: list[dict]) -> Path:
    out = root / "groups" / f"joined_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return out


def write_listings(root: Path, rows: list[dict]) -> Path:
    out = root / "cache" / "listings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows), encoding="utf-8")
    return out


# --------------------------------------------------------------------------
# matrix_rows_for
# --------------------------------------------------------------------------

def test_matrix_rows_for_collapses_duplicates_into_one_disabled_checkbox():
    """Two joined rows that fold to the same key become ONE checkbox (the
    picker cannot tell those twins apart by name either); `count` surfaces it."""
    rows = [{"id": "1", "name": "Venta de Carros Chihuahua"},
            {"id": "2", "name": "  venta  DE carros  chihuahua "}]
    out = matrix_rows_for(rows, {"venta de carros chihuahua"})
    assert len(out) == 1
    entry = out[0]
    assert entry["key"] == "venta de carros chihuahua"
    assert entry["count"] == 2
    assert entry["name"] == "Venta de Carros Chihuahua"   # first occurrence
    assert entry["disabled"] is True

    # same rows, nothing off -> the checkbox is ON
    assert matrix_rows_for(rows, set())[0]["disabled"] is False


def test_matrix_rows_for_collapses_accent_normalisation():
    """NFC and NFD spellings of one name fold together (fold_name normalises)."""
    nfc = "Ciudad Ju\u00e1rez autos"
    nfd = "Ciudad Ju\u0061\u0301rez autos"
    out = matrix_rows_for([{"id": "1", "name": nfc}, {"id": "2", "name": nfd}], set())
    assert len(out) == 1 and out[0]["count"] == 2


def test_matrix_rows_for_sorts_distinct_names_by_name():
    rows = [{"id": "3", "name": "Carros Chihuahua"},
            {"id": "1", "name": "autos juarez"},
            {"id": "2", "name": "Venta de autos"}]
    out = matrix_rows_for(rows, set())
    assert [r["name"] for r in out] == ["autos juarez", "Carros Chihuahua",
                                        "Venta de autos"]


def test_matrix_rows_for_is_empty_for_no_groups():
    assert matrix_rows_for([], {"whatever"}) == []


# --------------------------------------------------------------------------
# read_matrix_view
# --------------------------------------------------------------------------

def test_read_matrix_view_missing_store_is_available_and_empty(tmp_path):
    """A never-written store is the legitimate 'everything is ON' default:
    available and empty, never an error."""
    view = read_matrix_view(GuiPaths.from_root(tmp_path))
    assert view["available"] is True
    assert view["listings"] == {}
    assert view["updated_at"] == ""
    assert view["error"] is None
    assert view["path"] == str(gm.matrix_path(tmp_path))


def test_read_matrix_view_corrupt_store_reports_error_without_raising(tmp_path):
    p = gm.matrix_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    view = read_matrix_view(GuiPaths.from_root(tmp_path))   # must not raise
    assert view["available"] is False
    assert isinstance(view["error"], str) and view["error"]
    assert view["listings"] == {}


def test_read_matrix_view_folds_hand_written_names(tmp_path):
    """A hand-edited file may carry raw names; the view ships folded keys so
    the page's checkbox round-trips to /api/group-matrix match the store."""
    p = gm.matrix_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"version": 1, "updated_at": "2026-10-07T00:00:00+00:00",
         "listings": {L1: {"disabled": ["Dos  Grupo"]}}}), encoding="utf-8")
    view = read_matrix_view(GuiPaths.from_root(tmp_path))
    assert view["available"] is True
    assert view["listings"][L1]["disabled"] == ["dos grupo"]
    assert view["updated_at"] == "2026-10-07T00:00:00+00:00"


# --------------------------------------------------------------------------
# build_state wiring
# --------------------------------------------------------------------------

def test_build_state_wires_the_matrix_into_each_listing_row(tmp_path):
    write_groups_snapshot(tmp_path, "20261007T000000Z", [
        {"id": "1", "name": "Uno", "url": "u1"},
        {"id": "2", "name": "Dos", "url": "u2"},
        {"id": "3", "name": "Tres", "url": "u3"},
    ])
    write_listings(tmp_path, [
        {"id": L1, "title": "Car L1", "price": "$1"},
        {"id": L2, "title": "Car L2", "price": "$2"},
    ])
    gm.save_matrix(tmp_path, L1, ["Dos"])

    state = build_state(GuiPaths.from_root(tmp_path))

    # the top-level store view, folded keys included
    assert state["matrix"]["available"] is True
    assert state["matrix"]["listings"][L1]["disabled"] == ["dos"]
    assert L2 not in state["matrix"]["listings"]

    rows = {r["id"]: r for r in state["listings"]["rows"]}

    # L1: exactly one checkbox OFF, and it is the right one
    l1 = rows[L1]["matrix"]
    assert l1["disabled"] == 1
    assert [r["name"] for r in l1["rows"]] == ["Dos", "Tres", "Uno"]   # by name
    flags = {r["name"]: r["disabled"] for r in l1["rows"]}
    assert flags == {"Dos": True, "Tres": False, "Uno": False}
    assert {r["key"] for r in l1["rows"]} == {"uno", "dos", "tres"}

    # L2: no matrix entry => every group ON
    l2 = rows[L2]["matrix"]
    assert l2["disabled"] == 0
    assert [r["disabled"] for r in l2["rows"]] == [False, False, False]


def test_build_state_shows_a_refreshed_group_enabled(tmp_path):
    """Refresh groups must propagate: a group added by a newer joins snapshot
    appears in every listing's matrix and is ON (nothing caches the old list)."""
    write_groups_snapshot(tmp_path, "20261007T000000Z", [
        {"id": "1", "name": "Uno", "url": "u1"}])
    write_listings(tmp_path, [{"id": L1, "title": "T", "price": "$1"}])
    gm.save_matrix(tmp_path, L1, ["Uno"])
    state = build_state(GuiPaths.from_root(tmp_path))
    assert state["listings"]["rows"][0]["matrix"]["disabled"] == 1

    write_groups_snapshot(tmp_path, "20261007T010000Z", [
        {"id": "1", "name": "Uno", "url": "u1"},
        {"id": "2", "name": "Nuevo", "url": "u2"}])
    state = build_state(GuiPaths.from_root(tmp_path))
    matrix = state["listings"]["rows"][0]["matrix"]
    assert matrix["disabled"] == 1                      # the new group is ON
    flags = {r["name"]: r["disabled"] for r in matrix["rows"]}
    assert flags == {"Nuevo": False, "Uno": True}


def test_build_state_surfaces_a_corrupt_matrix_store(tmp_path):
    write_groups_snapshot(tmp_path, "20261007T000000Z", [
        {"id": "1", "name": "Uno", "url": "u1"}])
    write_listings(tmp_path, [{"id": L1, "title": "T", "price": "$1"}])
    p = gm.matrix_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")

    state = build_state(GuiPaths.from_root(tmp_path))
    assert state["matrix"]["available"] is False
    assert state["matrix"]["error"]
    # fail-safe: an unreadable store reads as everything ON
    assert state["listings"]["rows"][0]["matrix"]["disabled"] == 0


# --------------------------------------------------------------------------
# POST /api/group-matrix (live server)
#
# The already-shipped modal POSTs the WHOLE disabled set for one listing on
# every toggle; the endpoint replaces that listing's row and answers with the
# fresh build_state so the button label repaints without a second round-trip.
# Same live-server style as tests/test_gui.py: a real ThreadingHTTPServer on an
# ephemeral port, backed by a tmp capture root (nothing touches the repo's real
# .local-capture), driven over urllib.
# --------------------------------------------------------------------------

class _StubManager:
    """Just enough RunManager for create_server/build_state — the matrix route
    spawns nothing, so no real python is ever touched."""

    def __init__(self) -> None:
        self.buffer = RingBuffer()

    def status(self) -> dict:
        return {"running": False, "mode": None, "live": False, "pid": None,
                "started": None, "rc": None, "command": []}

    def available(self) -> dict:
        return {"groups": True}


def _post(url: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


@pytest.fixture
def live_matrix_server(tmp_path):
    write_groups_snapshot(tmp_path, "20261007T000000Z", [
        {"id": "1", "name": "Some Grupo", "url": "u1"},
        {"id": "2", "name": "Otro Grupo", "url": "u2"},
    ])
    write_listings(tmp_path, [
        {"id": L1, "title": "Car L1", "price": "$1"},
        {"id": L2, "title": "Car L2", "price": "$2"},
    ])
    httpd = create_server(GuiPaths.from_root(tmp_path), _StubManager(),  # type: ignore[arg-type]
                          port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def _row_matrix(state: dict, lid: str) -> dict:
    return {r["id"]: r for r in state["listings"]["rows"]}[lid]["matrix"]


def test_live_post_group_matrix_round_trips_the_folded_key(live_matrix_server):
    """(a) The page sends the server's own `key` values; the store folds them
    on the way in, and disabled_for agrees — the picker round-trip holds."""
    base, root = live_matrix_server
    status, state = _post(base + "/api/group-matrix",
                          {"listing_id": L1, "disabled": ["Some Grupo"]})
    assert status == 200
    assert state["matrix"]["listings"][L1]["disabled"] == ["some grupo"]
    assert gm.disabled_for(root, L1) == {"some grupo"}


def test_live_post_group_matrix_returns_the_checkbox_state(live_matrix_server):
    """(b) The SAME response carries the per-listing row view, so the modal's
    checkbox comes back OFF without a second /api/state fetch."""
    base, _root = live_matrix_server
    _status, state = _post(base + "/api/group-matrix",
                           {"listing_id": L1, "disabled": ["Some Grupo"]})
    m = _row_matrix(state, L1)
    assert m["disabled"] >= 1
    flags = {r["key"]: r["disabled"] for r in m["rows"]}
    assert flags == {"some grupo": True, "otro grupo": False}


def test_live_post_group_matrix_replaces_never_unions_the_set(live_matrix_server):
    """(c) A second POST REPLACES the set — the modal resends the whole set on
    each click, so a union here would make an un-check impossible."""
    base, root = live_matrix_server
    _post(base + "/api/group-matrix",
          {"listing_id": L1, "disabled": ["Some Grupo"]})
    _status, state = _post(base + "/api/group-matrix",
                           {"listing_id": L1, "disabled": ["Otro Grupo"]})
    assert gm.disabled_for(root, L1) == {"otro grupo"}      # union would be wrong
    flags = {r["key"]: r["disabled"] for r in _row_matrix(state, L1)["rows"]}
    assert flags == {"some grupo": False, "otro grupo": True}


@pytest.mark.parametrize("payload", [
    {"listing_id": "../../etc", "disabled": ["A"]},   # path traversal attempt
    {"listing_id": "", "disabled": ["A"]},            # empty / non-digit id
    {"listing_id": "abcdef", "disabled": ["A"]},      # non-digit id
    {"listing_id": L1, "disabled": "whatever"},       # non-list disabled
])
def test_live_post_group_matrix_rejects_bad_input(live_matrix_server, payload):
    """(d) Bad input is the operator's mistake: 400 + the reason, and NOTHING
    is written (the store never even creates its config dir)."""
    base, root = live_matrix_server
    status, data = _post(base + "/api/group-matrix", payload)
    assert status == 400
    assert isinstance(data.get("error"), str) and data["error"]
    assert not gm.matrix_path(root).exists()


def test_live_post_group_matrix_empty_list_clears_only_that_listing(live_matrix_server):
    """(e) An empty list turns every group back ON for one listing and leaves
    the OTHER listing's OFF set untouched."""
    base, root = live_matrix_server
    _post(base + "/api/group-matrix",
          {"listing_id": L1, "disabled": ["Some Grupo"]})
    _post(base + "/api/group-matrix",
          {"listing_id": L2, "disabled": ["Otro Grupo"]})
    _status, state = _post(base + "/api/group-matrix",
                           {"listing_id": L1, "disabled": []})
    assert gm.disabled_for(root, L1) == set()
    assert gm.disabled_for(root, L2) == {"otro grupo"}      # untouched
    assert _row_matrix(state, L1)["disabled"] == 0
    assert _row_matrix(state, L2)["disabled"] == 1
