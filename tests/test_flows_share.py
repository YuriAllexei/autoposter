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


# ---------------- A1: fetch_listing_description ----------------


def _desc(found=True, text="", has_more=False):
    return {"found": found, "text": text, "has_more": has_more,
            "has_less": False}


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


# ---------------- A2: open_share_hub ----------------


def _hub_page(groups, responses=None, stamp="ok"):
    return FakePage(
        js={fl._STAMP_SHARE_BTN_JS: "ok:" + stamp,
            fl._STAMP_HUB_GROUP_JS: stamp},
        wf={fl._WAIT_HUB_JS: True, fl._PICKER_READY_JS: True},
        responses=responses if responses is not None
        else [FakeResp(_groups_body(groups))])


def test_open_share_hub_returns_ranked_groups(monkeypatch, tmp_path):
    _, sleeps = _listen(monkeypatch, tmp_path)
    logs = []
    page = _hub_page([("g1", "Uno"), ("g2", "Dos")])
    out = asyncio.run(open_share_hub(
        page, {"id": "L1", "title": "2019 Chevrolet Tahoe LT"},
        _cfg(tmp_path), logs.append))
    assert [(g["id"], g["name"], g["rank"]) for g in out] == \
        [("g1", "Uno", 0), ("g2", "Dos", 0)]
    # armed and clicked: card Compartir + hub Grupo circle
    assert page.clicks == ['[data-ap-share="1"]', '[data-ap-share-group="1"]']
    assert any("hub opened, 2 groups in payload" in m for m in logs)
    assert len(sleeps) == 2          # one before each sensitive click


def test_open_share_hub_no_compartir_button_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = FakePage(js={fl._STAMP_SHARE_BTN_JS: "no-card"})
    with pytest.raises(FlowError, match="no unique card 'Compartir'"):
        asyncio.run(open_share_hub(page, {"id": "L1", "title": "T"},
                                   _cfg(tmp_path), lambda *_a: None))
    assert tags == ["share_no_button"]


def test_open_share_hub_picker_never_hydrates_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _hub_page([("g1", "Uno")])
    page.wf[fl._PICKER_READY_JS] = fl.PWTimeoutError("timeout")
    with pytest.raises(FlowError, match="never listed"):
        asyncio.run(open_share_hub(page, {"id": "L1", "title": "T"},
                                   _cfg(tmp_path), lambda *_a: None))
    assert tags == ["share_picker_skeleton"]


# ---------------- A3: pick_share_group ----------------


def _pick_page(row="ok", composer=True):
    return FakePage(js={fl._STAMP_SHARE_ROW_JS: row},
                    wf={fl._COMPOSER_OPEN_JS: composer})


def test_pick_share_group_types_then_clicks_the_row(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    logs = []
    page = _pick_page()
    asyncio.run(pick_share_group(page, {"name": "CARROS BARATOS EN CHIHUAHUA"},
                                 _cfg(tmp_path), logs.append))
    assert page.buf == "CARROS BARATOS EN CHIHUAHUA"   # paste into the search
    assert '[data-ap-share-row="1"]' in page.clicks
    assert any("composer open" in m for m in logs)


def test_pick_share_group_duplicate_name_aborts_ambiguous(monkeypatch,
                                                         tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(row="ambiguous:2")
    with pytest.raises(FlowError, match="ambiguous duplicate"):
        asyncio.run(pick_share_group(page, {"name": "VENTA DE CARROS"},
                                     _cfg(tmp_path), lambda *_a: None))
    assert tags == ["crossshare_ambiguous_row"]
    assert '[data-ap-share-row="1"]' not in page.clicks


def test_pick_share_group_retries_with_first_20_chars(monkeypatch, tmp_path):
    _listen(monkeypatch, tmp_path)
    name = "VENTAS DE CARROS CUAUHTÉMOC CHIHUAHUA"
    page = _pick_page(row=["none", "ok"])
    asyncio.run(pick_share_group(page, {"name": name}, _cfg(tmp_path),
                                 lambda *_a: None))
    assert page.buf == name + name[:20]      # full name, then the 20-char retry
    assert '[data-ap-share-row="1"]' in page.clicks


def test_pick_share_group_no_row_after_retry_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(row="none")
    with pytest.raises(FlowError, match="share_group_row_missing"):
        asyncio.run(pick_share_group(page, {"name": "NADA"}, _cfg(tmp_path),
                                     lambda *_a: None))
    assert tags[-1] == "crossshare_no_row"


def test_pick_share_group_composer_never_opens_raises(monkeypatch, tmp_path):
    tags, _ = _listen(monkeypatch, tmp_path)
    page = _pick_page(composer=False)
    with pytest.raises(FlowError, match="share_group_row_missing"):
        asyncio.run(pick_share_group(page, {"name": "Uno"}, _cfg(tmp_path),
                                     lambda *_a: None))
    assert tags == ["crossshare_no_row"]


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
    assert groups == [{"id": "9", "name": "Only", "rank": 0}]
