"""Slice A tests: the SHARE ritual (card Compartir -> hub Grupo -> picker ->
composer -> paste -> stage/publish), all self-contained fakes.

Fake conventions copied from tests/test_crosspost.py: no browser, no network,
async mock page/locator objects, monkeypatched human_sleep + dump_evidence.
Selectors themselves are probe-proven (scripts/probe_share_flow.py, recording
20260925T014900Z); these tests pin the CONTROL logic around them.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import poster.flows as fl
from poster.config import Config
from poster.flows import (
    FlowError,
    _tag_ranks,
    fetch_listing_description,
    match_picker_row,
    open_share_hub,
    parse_share_targets,
    pick_share_group,
    stage_or_publish_share,
)

OP = fl.XPOST_GROUPS_OP
MUT = fl.XPOST_MUTATION_OP


@pytest.fixture(autouse=True)
def _fast_step_timeout(monkeypatch):
    """Poll loops use the real 30 s budget; tests keep it tiny (fakes are
    instant so a stale spin would otherwise burn 30 s wall-clock)."""
    monkeypatch.setattr(fl, "SHARE_STEP_TIMEOUT_MS", 60)
    monkeypatch.setattr(fl, "SHARE_SETTLE_POLL_MS", 1)


def _cfg(tmp_path, **kw) -> Config:
    base = {"dry_run": True, "headless": True,
            "screenshot_dir": tmp_path / "shots",
            "delay_min": 0.0, "delay_max": 0.001,
            "crosspost_action_min": 0.0, "crosspost_action_max": 0.001}
    base.update(kw)
    return Config(**base)


# ---------------- fake playwright objects ----------------


class FakeReq:
    def __init__(self, op):
        self.url = "https://www.facebook.com/api/graphql/"
        self.post_data = f"fb_api_req_friendly_name={op}"


class FakeResp:
    def __init__(self, body, op=OP):
        self._body, self.request = body, FakeReq(op)

    async def text(self):
        return self._body


class _RInfo:
    def __init__(self, resp):
        self._resp = resp

    @property
    def value(self):
        async def _get():
            if isinstance(self._resp, Exception):
                raise self._resp
            return self._resp
        return _get()


class FakeExpect:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return _RInfo(self._resp)

    async def __aexit__(self, *a):
        return False


class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def filter(self, **_kw):
        return self

    def nth(self, i):
        return self

    async def count(self):
        return self.page.sel_counts.get(self.sel, 1)

    async def is_visible(self):
        return self.page.sel_visible.get(self.sel, True)

    async def click(self, timeout=None):
        self.page.clicks.append(self.sel)
        self.page._fire_responses()

    async def evaluate(self, expr, *a):
        # _clear_editor's verified-empty probe: fake boxes start empty
        return 0

    async def wait_for(self, state=None, timeout=None):
        self.page.waits.append((self.sel, timeout))
        if self.page.sel_wait_raises.get(self.sel):
            raise fl.PWTimeoutError("waited too long")

    async def inner_text(self):
        if "contenteditable" in self.sel:
            return self.page.buf
        return ""

    async def input_value(self):
        return self.page.buf

    async def get_attribute(self, _a):
        return self.page.attrs.get(self.sel, "")

    async def scroll_into_view_if_needed(self):
        return None


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.presses.append(key)
        if key == "Enter":
            self.page.buf += "\n"


class FakePage:
    """Scripted page: `js` maps a JS constant -> value|list|callable(arg).
    `wf` maps a wait_for_function constant -> bool|list (an Exception raises)."""

    def __init__(self, js=None, wf=None, responses=(), url=""):
        self.js = dict(js or {})
        self.wf = dict(wf or {})
        self.responses = list(responses)
        self.evals, self.clicks, self.presses, self.waits = [], [], [], []
        self.wf_calls, self.get_by_role_calls = [], []
        self.gotos, self.timeouts, self.buf = [], [], ""
        self.url = url
        self.keyboard = FakeKeyboard(self)
        self.sel_counts: dict = {}
        self.sel_visible: dict = {}
        self.sel_wait_raises: dict = {}
        self.attrs: dict = {}
        self.resp_handlers: list = []

    def on(self, event, fn):
        """Only 'response' is used by the flows: since 2026-10-07 the picker
        payload is a BONUS (collected from the sideline, never awaited), so the
        fake replays queued response bodies on the next click."""
        if event == "response":
            self.resp_handlers.append(fn)

    def remove_listener(self, event, fn):
        try:
            self.resp_handlers.remove(fn)
        except ValueError:
            pass

    def _fire_responses(self):
        if not self.resp_handlers or not self.responses:
            return
        queued, self.responses = list(self.responses), []
        for resp in queued:
            for fn in list(self.resp_handlers):
                fn(resp)

    async def goto(self, url, **_kw):
        self.gotos.append(url)
        self.url = url

    async def evaluate(self, expr, arg=None, *a):
        self.evals.append((expr, arg))
        if isinstance(expr, str) and "insertText" in expr:
            self.buf += str(arg or "")
            return None
        val = self.js.get(expr, None)
        if callable(val):
            return val(arg)
        if isinstance(val, list):
            return val.pop(0) if val else None
        return val

    async def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    async def wait_for_function(self, expr, arg=None, timeout=None):
        self.wf_calls.append(expr)
        val = self.wf.get(expr, True)
        if isinstance(val, Exception):
            raise val
        if isinstance(val, list):
            val = val.pop(0) if val else True
        if val is False:
            # real Playwright raises when the predicate keeps returning false
            raise fl.PWTimeoutError("wait_for_function predicate stayed false")
        return val

    def locator(self, sel):
        return FakeLocator(self, sel)

    def get_by_placeholder(self, text):
        self.get_by_role_calls.append(("placeholder", text))
        return FakeLocator(self, f"placeholder:{text}")

    def get_by_role(self, role, name=None, exact=None):
        self.get_by_role_calls.append((role, name, exact))
        return FakeLocator(self, f"role:{role}:{name}")

    def expect_response(self, flt, timeout=None):
        self.wf_calls.append(("expect_response", timeout))
        return FakeExpect(self.responses.pop(0) if self.responses else FakeResp("{}"))

    async def content(self):
        return "<html>fake</html>"


def _listen(monkeypatch, tmp_path, calls=None):
    """Patch dump_evidence (records tags) + human_sleep (records whys)."""
    tags, sleeps = [], calls if calls is not None else []

    async def fake_dump(page, dir_, tag, html="", extra=None):
        tags.append(tag)
        return tmp_path / f"{tag}.png"

    async def fake_sleep(cfg, log, why="", lo=None, hi=None):
        sleeps.append(why)

    monkeypatch.setattr(fl, "dump_evidence", fake_dump)
    monkeypatch.setattr(fl, "human_sleep", fake_sleep)
    return tags, sleeps


def _groups_body(pairs):
    return json.dumps({"data": {"viewer": {"actor": {"groups": {
        "nodes": [{"__isActor": "Group", "__typename": "Group",
                   "id": i, "name": n} for i, n in pairs]}}}}})


# ---------------- payload parser ----------------


def test_parse_share_targets_nodes_order_and_prefix():
    body = "for(;;);" + _groups_body([("g1", "Uno"), ("g2", "Dos")])
    assert parse_share_targets(body) == [{"id": "g1", "name": "Uno"},
                                         {"id": "g2", "name": "Dos"}]


def test_parse_share_targets_edges_fallback_and_garbage_drop():
    body = json.dumps({"data": {"viewer": {"actor": {"groups": {"edges": [
        {"node": {"id": "a", "name": "A"}},
        {"node": {"name": "no-id"}}, {"node": None}, "junk"]}}}}})
    assert parse_share_targets(body) == [{"id": "a", "name": "A"}]


def test_tag_ranks_duplicate_names():
    groups = [{"id": "1", "name": "venta de carros chihuahua"},
              {"id": "2", "name": "otro"},
              {"id": "3", "name": "VENTA  DE CARROS CHIHUAHUA"}]
    _tag_ranks(groups)
    assert [g["rank"] for g in groups] == [0, 0, 1]


def test_tag_row_identity_marks_rank_and_name_total():
    # name_total is the click-time safety guard: it says how many rows with
    # that folded name the plan saw, so a shrunken list FAILS instead of
    # clicking a possibly different twin.
    got = fl.tag_row_identity([
        {"name": "venta de carros chihuahua"},
        {"name": "AUTOS CHIHUAHUA"},
        {"name": "VENTA DE CARROS CHIHUAHUA"},
    ])
    assert [(g["rank"], g["name_total"]) for g in got] == [(0, 2), (0, 1), (1, 2)]


# ---------------- A1: fetch_listing_description ----------------


def _desc(found=True, text="", has_more=False, has_less=False):
    return {"found": found, "text": text, "has_more": has_more,
            "has_less": has_less}


def test_fetch_description_with_ver_mas_clicks_once(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    short, long_ = "CHEVROLET TAHOE LT 2019 🚙 Excelente condición", \
        "CHEVROLET TAHOE LT 2019 🚙 Excelente condición\n📍 Chihuahua"
    page = FakePage(js={fl._SHARE_DESC_JS: [_desc(text=short, has_more=True),
                                            _desc(text=long_)]})
    page.url = "https://www.facebook.com/marketplace/item/2316472202438634"
    out = asyncio.run(fetch_listing_description(
        page, _cfg(tmp_path), lambda *_a: None,
        {"id": "2316472202438634", "title": "2019 Chevrolet Tahoe LT"}))
    assert out == long_
    assert page.clicks.count('[data-ap-share-more="1"]') == 1   # exactly once
    assert tags == []


def test_fetch_description_without_ver_mas_never_clicks(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._SHARE_DESC_JS: _desc(text="linea uno\nlinea dos")})
    page.url = "https://www.facebook.com/marketplace/item/2316472202438634"
    out = asyncio.run(fetch_listing_description(
        page, _cfg(tmp_path), lambda *_a: None, {"id": "2316472202438634"}))
    assert out == "linea uno\nlinea dos"
    assert '[data-ap-share-more="1"]' not in page.clicks


def test_fetch_description_missing_heading_raises_with_evidence(
        monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._SHARE_DESC_JS: _desc(found=False)})
    with pytest.raises(FlowError, match="Descripción del vendedor"):
        asyncio.run(fetch_listing_description(
            page, _cfg(tmp_path), lambda *_a: None, {"id": "1"}))
    assert tags == ["share_desc_missing"]


def test_fetch_description_builds_item_url_from_id(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._SHARE_DESC_JS: _desc(text="hola")})
    asyncio.run(fetch_listing_description(
        page, _cfg(tmp_path), lambda *_a: None, {"id": "999"}))
    assert page.gotos == ["https://www.facebook.com/marketplace/item/999/"]


def test_fetch_description_drops_an_inline_toggle_label(monkeypatch, tmp_path):
    """LIVE 2026-10-07 (poster.listings on the real item page): the block's
    innerText carried the collapse control INLINE at the end of the seller
    text — '... No se aceptan cambios Ver menos'. The recording's rule is
    explicit: the label is NEVER part of the description."""
    _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._SHARE_DESC_JS: _desc(
        text="🚙 NISSAN ROGUE 2017\n🚫 No se aceptan cambios Ver menos",
        has_less=True)})
    page.url = "https://www.facebook.com/marketplace/item/997052559461487"
    out = asyncio.run(fetch_listing_description(
        page, _cfg(tmp_path), lambda *_a: None, {"id": "997052559461487"}))
    assert out == "🚙 NISSAN ROGUE 2017\n🚫 No se aceptan cambios"


def test_strip_toggle_labels_covers_the_observed_shapes():
    f = fl._strip_toggle_labels
    assert f("texto Ver menos") == "texto"            # inline, end of text
    assert f("texto Ver más") == "texto"
    assert f("texto VER MAS") == "texto"              # accent-less spelling
    assert f("texto\nVer menos") == "texto"           # alone on its own line
    assert f("Ver más\ntexto") == "texto"
    assert f("texto") == "texto"
    # a description that merely CONTAINS the words is left untouched
    assert f("Ver más fotos del auto") == "Ver más fotos del auto"
    # intentional paragraph breaks survive
    assert f("linea1\n\nlinea2") == "linea1\n\nlinea2"


# ---------------- A2: open_share_hub ----------------


def _hub_page(groups, responses=None, stamp="ok"):
    return FakePage(
        js={fl._STAMP_SHARE_BTN_JS: "ok:" + stamp,
            fl._STAMP_HUB_GROUP_JS: stamp},
        wf={fl._WAIT_HUB_JS: True, fl._PICKER_OPEN_JS: True},
        responses=responses if responses is not None
        else [FakeResp(_groups_body(groups))])


def test_open_share_hub_returns_ranked_groups(monkeypatch, tmp_path):
    _, sleeps = _listen(monkeypatch, tmp_path)
    logs = []
    page = _hub_page([("g1", "Uno"), ("g2", "Dos")])
    out = asyncio.run(open_share_hub(
        page, {"id": "L1", "title": "2019 Chevrolet Tahoe LT"},
        _cfg(tmp_path), logs.append))
    assert [(g["id"], g["name"], g["rank"], g["name_total"]) for g in out] == \
        [("g1", "Uno", 0, 1), ("g2", "Dos", 0, 1)]
    # armed and clicked: card Compartir + hub Grupo circle
    assert page.clicks == ['[data-ap-share="1"]', '[data-ap-share-group="1"]']
    assert any("hub opened, picker list ready, 2 group(s) in payload" in m
               for m in logs)
    assert len(sleeps) == 2          # one before each sensitive click


def test_open_share_hub_no_compartir_button_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._STAMP_SHARE_BTN_JS: "no-card"})
    with pytest.raises(FlowError, match="no unique card 'Compartir'"):
        asyncio.run(open_share_hub(page, {"id": "L1", "title": "T"},
                                   _cfg(tmp_path), lambda *_a: None))
    assert tags == ["share_no_button"]


def test_open_share_hub_retries_once_when_the_picker_never_opens(
        monkeypatch, tmp_path):
    """Live 2026-10-07: a click that lands mid-transition can leave NO dialog
    behind (evidence shot = the plain selling feed). The hub is reopened once
    before the share is given up."""
    tags, _ = _listen(monkeypatch, tmp_path)
    logs = []
    page = _hub_page([("g1", "Uno")])
    page.wf[fl._PICKER_OPEN_JS] = fl.PWTimeoutError("timeout")
    with pytest.raises(FlowError, match="share picker never opened"):
        asyncio.run(open_share_hub(page, {"id": "L1", "title": "T"},
                                   _cfg(tmp_path), logs.append))
    assert tags == ["share_picker_parse", "share_picker_parse"]   # 2 attempts
    assert any("hub open attempt 1/2 failed" in m for m in logs)
    assert sum(1 for m in logs if "hub open for" in m) == 2      # it re-opened


def test_open_share_hub_without_payload_is_not_fatal(monkeypatch, tmp_path):
    """The graphql answer is optional: readiness is the DIALOG (user rule —
    the picker LIST is the source of truth)."""
    _, _ = _listen(monkeypatch, tmp_path)
    logs = []
    page = _hub_page([], responses=[])
    out = asyncio.run(open_share_hub(
        page, {"id": "L1", "title": "T"}, _cfg(tmp_path), logs.append))
    assert out == []
    assert any("picker list ready, 0 group(s) in payload" in m for m in logs)
    assert page.clicks == ['[data-ap-share="1"]', '[data-ap-share-group="1"]']


# ---------------- A3: pick_share_group ----------------


def _pick_page(rows=None, stamp: object = "ok", composer=True):
    """Row-direct picker page. `rows` = the picker list AS THE READER SEES IT
    ([{i, name, full}]); the row is clicked by index, never by searching.
    `stamp` may also be a LIST of successive stamp answers (list → sequential
    returns, the FakePage convention)."""
    return FakePage(
        js={fl._PICKER_ROWS_JS: [rows or []],
            fl._PICKER_TOP_JS: True,
            fl._SCROLL_PICKER_JS: False,
            fl._STAMP_SHARE_ROW_AT_JS: stamp,   # row-direct stamp
            fl._STAMP_SHARE_ROW_JS: stamp},     # legacy search fallback stamp
        wf={fl._COMPOSER_OPEN_JS: composer})


def _row(i, name):
    return {"i": i, "name": name, "full": name + " · Grupo público"}


def test_pick_share_group_clicks_the_row_without_searching(monkeypatch,
                                                           tmp_path):
    _listen(monkeypatch, tmp_path)
    logs = []
    page = _pick_page([_row(0, "CARROS BARATOS EN CHIHUAHUA")])
    asyncio.run(pick_share_group(page, {"name": "CARROS BARATOS EN CHIHUAHUA",
                                        "rank": 0, "name_total": 1},
                                 _cfg(tmp_path), logs.append))
    assert '[data-ap-share-row="1"]' in page.clicks
    assert page.buf == ""                       # 'Buscar grupos' never typed
    assert page.get_by_role_calls == []         # no search-box lookup at all
    assert any("picker list scanned, 1 row(s) mounted" in m for m in logs)
    assert any("composer open" in m for m in logs)


def test_pick_share_group_duplicate_name_takes_the_ranked_row(monkeypatch,
                                                             tmp_path):
    _listen(monkeypatch, tmp_path)
    page = _pick_page([_row(0, "venta de carros chihuahua"),
                       _row(1, "otra cosa"),
                       _row(2, "VENTA DE CARROS CHIHUAHUA")])
    asyncio.run(pick_share_group(page, {"name": "VENTA DE CARROS CHIHUAHUA",
                                        "rank": 1, "name_total": 2},
                                 _cfg(tmp_path), lambda *_a: None))
    stamped = [e for e in page.evals if e[0] == fl._STAMP_SHARE_ROW_AT_JS]
    assert stamped[-1][1] == {"i": 2, "name": "VENTA DE CARROS CHIHUAHUA"}
    assert page.buf == ""


def test_pick_share_group_shifted_list_falls_back_to_search(monkeypatch,
                                                            tmp_path):
    """The plan saw TWO rows with that name, the list now holds ONE: clicking
    the occurrence would risk the wrong twin, so it must fall back to the
    search (and still never guess)."""
    _listen(monkeypatch, tmp_path)
    logs = []
    page = _pick_page([_row(0, "venta de carros chihuahua")])
    asyncio.run(pick_share_group(page, {"name": "venta de carros chihuahua",
                                        "rank": 0, "name_total": 2},
                                 _cfg(tmp_path), logs.append))
    assert page.buf == "venta de carros chihuahua"     # the fallback typed it
    assert '[data-ap-share-row="1"]' in page.clicks
    assert any("row-direct shifted" in m for m in logs)


def test_pick_share_group_stamp_mismatch_never_clicks(monkeypatch, tmp_path):
    """A row that re-rendered between the read and the click must NOT be
    clicked (a wrong group is worse than a lost share)."""
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page([_row(0, "Uno")], stamp="mismatch:otro")
    with pytest.raises(FlowError, match="could not be stamped"):
        asyncio.run(pick_share_group(page, {"name": "Uno", "rank": 0,
                                            "name_total": 1},
                                     _cfg(tmp_path), lambda *_a: None))
    assert tags == ["crossshare_row_stamp"]
    assert '[data-ap-share-row="1"]' not in page.clicks


def test_pick_share_group_duplicate_name_aborts_ambiguous(monkeypatch,
                                                         tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(stamp="ambiguous:2")        # no rows -> legacy fallback
    with pytest.raises(FlowError, match="ambiguous duplicate"):
        asyncio.run(pick_share_group(page, {"name": "VENTA DE CARROS",
                                            "rank": 1, "name_total": 2},
                                     _cfg(tmp_path), lambda *_a: None))
    assert tags == ["crossshare_ambiguous_row"]
    assert '[data-ap-share-row="1"]' not in page.clicks


def test_pick_share_group_retries_with_first_20_chars(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    name = "VENTAS DE CARROS CUAUHTÉMOC CHIHUAHUA"
    page = _pick_page(stamp=["none", "ok"])       # no rows -> legacy fallback
    asyncio.run(pick_share_group(page, {"name": name, "rank": 0,
                                        "name_total": 1},
                                 _cfg(tmp_path), lambda *_a: None))
    assert page.buf == name + name[:20]      # full name, then the 20-char retry
    assert '[data-ap-share-row="1"]' in page.clicks


def test_pick_share_group_no_row_after_retry_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(stamp="none")
    with pytest.raises(FlowError, match="share_group_row_missing"):
        asyncio.run(pick_share_group(page, {"name": "NADA", "rank": 0,
                                            "name_total": 1},
                                     _cfg(tmp_path), lambda *_a: None))
    assert tags[-1] == "crossshare_no_row"


def test_pick_share_group_composer_never_opens_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(composer=False)
    with pytest.raises(FlowError, match="never opened the share composer"):
        asyncio.run(pick_share_group(page, {"name": "Uno", "rank": 0,
                                            "name_total": 1},
                                     _cfg(tmp_path), lambda *_a: None))
    assert tags == ["crossshare_no_row"]


# ---------------- match_picker_row (pure) ----------------


def test_match_picker_row_unique_name():
    rows = [_row(0, "A"), _row(1, "AUTOS CHIHUAHUA"), _row(2, "B")]
    assert match_picker_row(rows, {"name": "autos chihuahua", "rank": 0,
                                   "name_total": 1}) == {"status": "ok", "i": 1}


def test_match_picker_row_duplicate_uses_the_rank():
    rows = [_row(0, "venta de carros chihuahua"), _row(1, "x"),
            _row(2, "venta de carros chihuahua")]
    assert match_picker_row(rows, {"name": "VENTA DE CARROS CHIHUAHUA",
                                   "rank": 0, "name_total": 2})["i"] == 0
    assert match_picker_row(rows, {"name": "VENTA DE CARROS CHIHUAHUA",
                                   "rank": 1, "name_total": 2})["i"] == 2


def test_match_picker_row_missing_and_shifted():
    assert match_picker_row([_row(0, "A")],
                            {"name": "Z", "rank": 0,
                             "name_total": 1})["status"] == "missing"
    # the plan recorded two twins, the list holds one -> never click
    got = match_picker_row([_row(0, "venta de carros chihuahua")],
                           {"name": "venta de carros chihuahua", "rank": 0,
                            "name_total": 2})
    assert got == {"status": "shifted", "i": "", "seen": 1, "plan": 2}


def test_match_picker_row_rank_out_of_range_is_shifted():
    got = match_picker_row([_row(0, "venta de carros chihuahua")],
                           {"name": "venta de carros chihuahua", "rank": 1,
                            "name_total": 1})
    assert got["status"] == "shifted"


def test_picker_all_rows_rescans_until_the_count_stabilises(monkeypatch,
                                                            tmp_path):
    """The first open of a run can still be hydrating (live dry run: 40 of 60
    rows on the first scan). A short list would report a real target as
    'missing' and hand the share to the typeahead, which loses shares — so the
    scan walks again while it keeps finding more rows and keeps the largest."""
    _listen(monkeypatch, tmp_path)
    short = [_row(i, f"g{i}") for i in range(40)]
    full = [_row(i, f"g{i}") for i in range(60)]
    page = FakePage(js={fl._PICKER_ROWS_JS: [short, full],
                        fl._PICKER_TOP_JS: True,
                        fl._SCROLL_PICKER_JS: False})
    rows = asyncio.run(fl._picker_all_rows(page, _cfg(tmp_path),
                                           lambda *_a: None))
    assert len(rows) == 60 and rows[0]["name"] == "g0"


# ---------------- A4: stage_or_publish_share ----------------


DESC = "CHEVROLET TAHOE LT 2019 🚙 Excelente condición\n📍 Chihuahua, Chihuahua"


def _stage_page(dry=True, preview=True):
    return FakePage(wf={fl._PREVIEW_SETTLED_JS: preview,
                        fl._DIALOG_GONE_JS: True},
                    responses=[FakeResp("{}", op=MUT)])


def test_staged_path_never_touches_publicar(monkeypatch, tmp_path):
    tags, sleeps = _listen(monkeypatch, tmp_path)
    logs = []
    page = _stage_page(dry=True)
    out = asyncio.run(stage_or_publish_share(
        page, _cfg(tmp_path, dry_run=True), logs.append, DESC,
        {"id": "g1", "name": "CARROS BARATOS EN CHIHUAHUA"}))
    assert out == "staged"
    # the Publicar locator was NEVER built on the dry path
    assert page.get_by_role_calls == []
    assert page.buf == DESC                      # description pasted 1:1
    assert tags == ["crossshare_staged"]         # evidence prefix recorded
    assert fl._DIALOG_GONE_JS in page.wf_calls   # close was awaited
    assert sleeps == []                          # dry path never sleeps on Publicar


def test_staged_path_closes_via_cerrar_button(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    page = _stage_page(dry=True)
    page.attrs['[role="dialog"] [role="button"][aria-label*="errar" i], '
               '[role="dialog"] [role="button"][aria-label*="lose" i]'] = "Cerrar"
    asyncio.run(stage_or_publish_share(page, _cfg(tmp_path), lambda *_a: None,
                                       DESC, {"name": "G"}))
    assert any("errar" in c for c in page.clicks)


def test_live_path_clicks_publicar_once(monkeypatch, tmp_path):
    _, sleeps = _listen(monkeypatch, tmp_path)
    page = _stage_page(dry=False)
    out = asyncio.run(stage_or_publish_share(
        page, _cfg(tmp_path, dry_run=False), lambda *_a: None, DESC,
        {"name": "G"}))
    assert out == "published"
    assert ("button", fl.SHARE_PUBLISH_TEXT, True) in page.get_by_role_calls
    assert 'role:button:Publicar' in page.clicks
    assert len(sleeps) == 1                      # action sleep before Publicar


def test_link_preview_never_settles_raises_with_evidence(monkeypatch,
                                                         tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _stage_page(preview=False)
    with pytest.raises(FlowError, match="Creando vista previa del enlace"):
        asyncio.run(stage_or_publish_share(page, _cfg(tmp_path),
                                           lambda *_a: None, DESC, {"name": "G"}))
    assert tags == ["crossshare_preview_stuck"]


# ---- hydration polling in the stamp finders (dry-ladder lesson 2026-09-25) ----

async def _fake_evidence(page, dir, tag, **_kw):
    return f"{tag}.png"


def test_share_button_polls_skeleton_until_card_hydrates(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "dump_evidence", _fake_evidence)
    page = FakePage(js={fl._STAMP_SHARE_BTN_JS:
                        ["no-card", "none-in-card", "ok"]})
    btn = asyncio.run(fl._find_share_button(page, "T", _cfg(tmp_path),
                                            lambda *a: None))
    assert btn.sel == '[data-ap-share="1"]'
    assert sum(1 for e, _ in page.evals if e == fl._STAMP_SHARE_BTN_JS) == 3


def test_share_button_gives_up_with_reason_after_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "dump_evidence", _fake_evidence)
    page = FakePage(js={fl._STAMP_SHARE_BTN_JS: "no-card"})
    with pytest.raises(FlowError) as exc:
        asyncio.run(fl._find_share_button(page, "T", _cfg(tmp_path),
                                          lambda *a: None))
    assert "no-card" in str(exc.value)
    assert "hydration" in str(exc.value)


def test_share_button_ambiguous_fails_fast_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "dump_evidence", _fake_evidence)
    page = FakePage(js={fl._STAMP_SHARE_BTN_JS: "ambiguous:2"})
    with pytest.raises(FlowError):
        asyncio.run(fl._find_share_button(page, "T", _cfg(tmp_path),
                                          lambda *a: None))
    assert sum(1 for e, _ in page.evals if e == fl._STAMP_SHARE_BTN_JS) == 1


def test_hub_group_circle_polls_until_dialog_hydrates(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "dump_evidence", _fake_evidence)
    page = FakePage(js={fl._STAMP_HUB_GROUP_JS: ["no-dialog", "none", "ok"]})
    loc = asyncio.run(fl._find_hub_group_circle(page, _cfg(tmp_path),
                                                lambda *a: None))
    assert loc.sel == '[data-ap-share-group="1"]'
    assert sum(1 for e, _ in page.evals if e == fl._STAMP_HUB_GROUP_JS) == 3


# ---- discover_share_groups: DOM list is the source of truth ------------------

class _DiscPage:
    """Minimal fake for discovery: scripted scroll/scrape evaluates."""

    def __init__(self, rows, grew=False):
        self.rows = rows
        self.grew = grew
        self.timeouts = []

    def on(self, *a):
        pass

    def remove_listener(self, *a):
        pass

    async def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    async def wait_for_function(self, expr, arg=None, timeout=None):
        return None                       # dialogs "gone" instantly

    async def evaluate(self, js, arg=None):
        if js is fl._SCROLL_PICKER_JS:
            return self.grew
        if js is fl._SCRAPE_PICKER_ROWS_JS:
            return self.rows
        raise AssertionError("unexpected evaluate")


def test_discover_uses_dom_rows_and_splices_payload_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "SHARE_STEP_TIMEOUT_MS", 50)

    async def fake_open(page, listing, cfg, log=print):
        return [{"id": str(i), "name": n, "rank": 0}
                for i, n in enumerate(["Alpha", "Bravo"])]
    monkeypatch.setattr(fl, "open_share_hub", fake_open)
    rows = [{"name": "Alpha"}, {"name": "Bravo"}, {"name": "Charlie"},
            {"name": "ALPHA"}]                      # duplicate, unseen in payload
    groups = asyncio.run(fl.discover_share_groups(
        _DiscPage(rows), {"title": "T"}, _cfg(tmp_path), lambda *a: None))
    assert [g["name"] for g in groups] == ["Alpha", "Bravo", "Charlie", "ALPHA"]
    assert groups[0]["id"] == "0" and groups[1]["id"] == "1"
    assert str(groups[2]["id"]).startswith("dom-")   # no payload id -> synthetic
    assert groups[0]["rank"] == 0 and groups[3]["rank"] == 1  # ALPHA == alpha


def test_discover_falls_back_to_payload_when_dom_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(fl, "SHARE_STEP_TIMEOUT_MS", 50)

    async def fake_open(page, listing, cfg, log=print):
        return [{"id": "9", "name": "Only", "rank": 0}]
    monkeypatch.setattr(fl, "open_share_hub", fake_open)
    groups = asyncio.run(fl.discover_share_groups(
        _DiscPage([]), {"title": "T"}, _cfg(tmp_path), lambda *a: None))
    assert groups == [{"id": "9", "name": "Only", "rank": 0, "name_total": 1}]
