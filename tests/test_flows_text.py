"""RED-GREEN guard for the FASTER posting flow (2026-09-22):

  1. attach_car_photos(): NO human_sleep before photo batches — photo
     attachment is as fast as possible; upload settling stays gated by the
     condition-wait _wait_upload_settled. human_sleep is untouched everywhere
     else (group_composer_es_v1 still sleeps before PUBLISH).
  2. _ensure_text_1to1(): paste-speed insertion
     (`document.execCommand('insertText', false, line)` + Enter between lines,
     which preserves newlines 1:1) instead of char-by-char keyboard.type —
     with the READ-BACK 1:1 gate kept as the HARD gate, exactly ONE
     keyboard.type fallback when read-back mismatches, FlowError if it still
     mismatches (never post text that fails read-back).

No browser: page/box/keyboard are async mocks; the composer model mirrors what
the real event handlers would leave in the contenteditable.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster import flows
from poster.config import Config
from poster.flows import (
    FlowError,
    Post,
    _ensure_text_1to1,
    attach_car_photos,
    group_composer_es_v1,
)
from poster.photos import CarPhotos

# ---- fakes -------------------------------------------------------------------

class Composer:
    """The contenteditable's content, built by whichever path is used."""

    def __init__(self, read_overrides=()):
        self.content = ""
        self.read_overrides = list(read_overrides)
        self.reads = 0
        self.clicks = 0

    def read(self):
        self.reads += 1
        if self.read_overrides:
            return self.read_overrides.pop(0)
        return self.content


class FakeBox:
    def __init__(self, composer):
        self.composer = composer

    @property
    def first(self):
        return self

    async def click(self, **kw):
        self.composer.clicks += 1

    async def inner_text(self):
        return self.composer.read()

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def wait_for(self, **kw):
        return None


class FakeKeyboard:
    def __init__(self, composer):
        self.composer = composer
        self.types = []      # (text, delay) from the OLD char-by-char path
        self.presses = []    # key names, in order
        self.held = []

    async def type(self, text, delay=None):
        self.types.append((text, delay))
        self.composer.content = text  # typing rewrites the box

    async def press(self, key):
        self.presses.append(key)
        if key == "Enter":
            self.composer.content += "\n"

    async def down(self, key):
        self.held.append(key)

    async def up(self, key):
        self.held.append(key)


class FakeInput:
    def __init__(self, page, idx):
        self.page, self.idx = page, idx

    async def get_attribute(self, name):
        if name == "accept":
            return self.page.file_input_accepts[self.idx]
        return None

    async def set_input_files(self, paths, timeout=None):
        self.page.uploads.append((self.idx, list(paths)))


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    async def count(self):
        if 'input[type="file"]' in self.selector:
            return len(self.page.file_input_accepts)
        return 1

    async def is_visible(self):
        return True

    async def wait_for(self, **kw):
        return None

    async def inner_text(self):
        if "contenteditable" in self.selector:
            return self.page.composer.read()
        return "Publicar"

    async def click(self, **kw):
        return None

    async def scroll_into_view_if_needed(self):
        return None

    def filter(self, **kw):
        return self

    def nth(self, i):
        return FakeInput(self.page, i)


class FakePage:
    def __init__(self, composer=None, file_input_accepts=("image/*",)):
        self.composer = composer or Composer()
        self.box = FakeBox(self.composer)
        self.keyboard = FakeKeyboard(self.composer)
        self.evaluate_calls = []   # (expr, arg)
        self.timeouts = []
        self.uploads = []          # (input idx, [paths])
        self.selector_waits = []   # selectors waited on
        self.file_input_accepts = list(file_input_accepts)

    # --- text path ---
    async def evaluate(self, expr, arg=None, *a):
        self.evaluate_calls.append((expr, arg))
        if "insertText" in expr:
            self.composer.content += arg

    async def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    # --- locators / upload ---
    def locator(self, selector):
        return FakeLocator(self, selector)

    async def wait_for_selector(self, selector, timeout=None, **kw):
        self.selector_waits.append(selector)

    async def content(self):
        return "<html>fake</html>"


def _cfg():
    # env-independent: only dry_run + jitter matter to these tests
    return Config(dry_run=True, type_delay_min_ms=30, type_delay_max_ms=90)


def _insert_args(page):
    """Arguments passed to an 'insertText' evaluate call, in order."""
    return [arg for expr, arg in page.evaluate_calls if "insertText" in expr]


def _sleep_recorder(monkeypatch):
    calls = []

    async def rec(cfg, log, why=""):
        calls.append(why)

    monkeypatch.setattr(flows, "human_sleep", rec)
    return calls


# ---- (a) paste is the primary text path --------------------------------------

def test_paste_used_and_keyboard_type_not_called():
    page = FakePage()
    text = "Linea uno\nLinea dos\nLinea tres"
    logs = []
    asyncio.run(_ensure_text_1to1(page, page.box, text, _cfg(), logs.append))

    # one insertText per non-empty line, in order
    assert _insert_args(page) == ["Linea uno", "Linea dos", "Linea tres"]
    # ...and it really is the insertText/execCommand paste, not a keystroke sim
    for expr, _ in page.evaluate_calls:
        assert "execCommand" in expr and "insertText" in expr
    # char-by-char typing is NOT the path anymore
    assert page.keyboard.types == []
    assert page.composer.clicks >= 1          # box was focused
    # success log says paste + read-back verified
    assert any("pasted 1:1" in m and "read-back verified" in m for m in logs)


# ---- (b) newline fidelity -----------------------------------------------------

def test_multiline_text_presses_enter_once_per_line_break():
    page = FakePage()
    text = "uno\n\ndos\ntres\ncuatro"          # includes an empty line
    asyncio.run(_ensure_text_1to1(page, page.box, text, _cfg(), lambda *_: None))

    lines = text.split("\n")
    assert page.keyboard.presses == ["Enter"] * (len(lines) - 1)
    assert page.composer.content == text       # newlines preserved 1:1
    assert _insert_args(page) == ["uno", "dos", "tres", "cuatro"]  # empty skipped


def test_single_line_text_needs_no_enter():
    page = FakePage()
    asyncio.run(_ensure_text_1to1(page, page.box, "solo una linea", _cfg(), lambda *_: None))
    assert page.keyboard.presses == []
    assert page.composer.content == "solo una linea"


# ---- (c) one fallback on read-back mismatch -----------------------------------

def test_readback_mismatch_falls_back_to_typing_once_then_succeeds():
    page = FakePage(Composer(read_overrides=["texto basura"]))
    text = "uno\ndos"
    logs = []
    asyncio.run(_ensure_text_1to1(page, page.box, text, _cfg(), logs.append))

    assert len(page.keyboard.types) == 1                 # exactly ONE fallback
    typed, delay = page.keyboard.types[0]
    assert typed == text                                 # whole post retyped
    assert isinstance(delay, int) and 30 <= delay <= 90  # cfg jitter kept
    assert page.composer.reads == 2                      # re-read after fallback
    assert not any("mismatch" in m for m in logs if "refusing" in m)


# ---- (d) double mismatch is a hard stop ---------------------------------------

def test_double_mismatch_raises_flow_error():
    page = FakePage(Composer(read_overrides=["basura 1", "basura 2"]))
    with pytest.raises(FlowError, match="refusing to continue"):
        asyncio.run(_ensure_text_1to1(page, page.box, "uno\ndos", _cfg(), lambda *_: None))
    assert len(page.keyboard.types) == 1   # fallback attempted once, then abort
    assert page.composer.reads == 2


# ---- (e) sleep policy: photo batches fast, publish still delayed --------------

def test_attach_car_photos_never_sleeps(monkeypatch, tmp_path):
    calls = _sleep_recorder(monkeypatch)
    page = FakePage()
    f1, f2 = tmp_path / "a.jpg", tmp_path / "b.jpg"
    f1.write_bytes(b"x")
    f2.write_bytes(b"x")
    post = Post(
        text="hola",
        cars=[CarPhotos(name="01_car", dir=tmp_path, files=[f1]),
              CarPhotos(name="02_car", dir=tmp_path, files=[f2])],
    )
    asyncio.run(attach_car_photos(page, post, _cfg(), lambda *_: None))

    assert calls == []                       # NO sleep before photo batches
    assert [p for _, p in page.uploads] == [[str(f1)], [str(f2)]]  # both attached
    assert page.selector_waits                  # settling gate still used


def test_dry_run_still_sleeps_before_publish(monkeypatch, tmp_path):
    calls = _sleep_recorder(monkeypatch)
    async def _dump(*a, **k):
        return tmp_path / "shot.png"

    monkeypatch.setattr(flows, "dump_evidence", _dump)
    page = FakePage()
    post = Post(text="uno\ndos", cars=[])    # no photos: attach is a no-op
    out = asyncio.run(group_composer_es_v1(page, post, _cfg(), lambda *_: None))

    assert out == {"evidence": str(tmp_path / "shot.png")}
    assert len(calls) == 1                   # exactly the pre-PUBLISH sleep
    assert "PUBLISH" in calls[0] or "DRY" in calls[0]
    assert _insert_args(page) == ["uno", "dos"]   # paste drove the real flow too