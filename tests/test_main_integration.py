"""Integration: poster.main records every attempt to the ledger and sends
EXACTLY ONE Discord summary per finished run (incl. aborts and crashes).
Dynamic-targets era: joined groups come from groups_fetch (stubbed), the
loop selects them via select_targets + ledger rotation.

No browser, no network: playwright launch is stubbed, main.py's
collaborators (login/profile/flow/verify/sleep/evidence) are monkeypatched,
and poster.notify's urlopen is captured — the REAL build_payload +
send_summary run end-to-end so payload text is asserted as shipped.
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pytest.importorskip("playwright")  # main.py imports it at module level

from poster import groups_fetch, notify
from poster import main as m
from poster.config import Config

JOINED = [
    {"id": "42", "name": "TEST GROUP",
     "url": "https://www.facebook.com/groups/42"},
]
JOINED2 = JOINED + [
    {"id": "43", "name": "TEST GROUP B",
     "url": "https://www.facebook.com/groups/43"},
]


class FakePage:
    def __getattr__(self, name):
        async def _m(*a, **k):
            return ""
        return _m


class FakeCtx:
    def __init__(self, pages):
        self.pages = pages

    async def close(self):
        pass


def _fake_pw():
    """Stands in for playwright.async_api.async_playwright: a plain callable
    returning an async context manager yielding the driver object."""

    class _Firefox:
        async def launch_persistent_context(self, *a, **k):
            return FakeCtx([FakePage()])

    class _P:
        firefox = _Firefox()

    @asynccontextmanager
    async def _cm():
        yield _P()

    return _cm()


class Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def _cfg(tmp_path, *, dry=False):
    return Config(
        dry_run=dry, profile_dir=tmp_path / "profile",
        screenshot_dir=tmp_path / "shots", log_dir=tmp_path / "logs",
        post_text_file=tmp_path / "post.txt",
        photos_dir=tmp_path / "photos",
        ledger_file=tmp_path / "results" / "ledger.jsonl",
        discord_webhook_url="https://discord.invalid/webhook/test",
        max_posts_per_run=5, fb_posting_profile_name="TestProfile")


def _make_harness(tmp_path, monkeypatch, *, dry=False, joined=None):
    cfg = _cfg(tmp_path, dry=dry)
    (tmp_path / "post.txt").write_text("hello", encoding="utf-8")
    posts = []

    def fake_urlopen(req, timeout=None):
        posts.append(json.loads(req.data.decode("utf-8")))
        return Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "async_playwright", _fake_pw)

    async def fake_fetch(page, *, av, log=None, max_pages=25):
        return list(joined if joined is not None else JOINED)

    monkeypatch.setattr(groups_fetch, "fetch_joined_groups", fake_fetch)

    async def ok_login(ctx, page, log, login_timeout=900):
        return True

    async def ok_profile(ctx, page, *, post_as, posting_user_id,
                         main_user_id, posting_name, main_profile_name="", log=None):
        return True

    async def no_sleep(cfg_, log, why="", *args, **kwargs):
        return None

    async def no_verify(page, url, cfg_, log):
        return None

    async def no_evidence(page, dir_, tag, html="", extra=None):
        return Path("/dev/null")

    monkeypatch.setattr(m, "ensure_login", ok_login)
    monkeypatch.setattr(m, "ensure_active_profile", ok_profile)
    monkeypatch.setattr(m, "verify_pending", no_verify)
    monkeypatch.setattr(m, "human_sleep", no_sleep)
    monkeypatch.setattr(m, "dump_evidence", no_evidence)

    def install_run_group(result):
        async def fake_run_group(page, group, post, cfg_, log):
            return result
        monkeypatch.setattr(m, "run_group", fake_run_group)

    return cfg, posts, install_run_group


def _ledger_lines(cfg):
    if not cfg.ledger_file.exists():
        return []
    return [json.loads(x) for x in
            cfg.ledger_file.read_text(encoding="utf-8").splitlines() if x.strip()]


def _desc(payload):
    return payload["embeds"][0]["description"]


def _install_boom(monkeypatch):
    async def boom(page, group, post, cfg, log):
        raise RuntimeError("kaput")
    monkeypatch.setattr(m, "run_group", boom)


def test_live_run_records_published_and_notifies_once(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    install(("posted", None))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 0
    line, = _ledger_lines(cfg)
    assert line["status"] == "published" and line["group_id"] == "42"
    assert line["dry_run"] is False
    assert len(posts) == 1  # exactly ONE Discord message
    embed = posts[0]["embeds"][0]
    assert embed["color"] == notify.COLOR_OK
    assert "Published: 1/1" in _desc(posts[0])
    assert "TEST GROUP" in _desc(posts[0]) and "all-time published: 1" in _desc(posts[0])
    assert "LIVE" in embed["title"] and "TestProfile" in embed["title"]


def test_dry_run_stages_never_counts_published(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch, dry=True)
    install(("posted", None))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 0
    line, = _ledger_lines(cfg)
    assert line["status"] == "staged" and line["dry_run"] is True
    assert len(posts) == 1
    embed = posts[0]["embeds"][0]
    assert embed["color"] == notify.COLOR_DRY and "DRY RUN" in embed["title"]
    assert "NOT published" in _desc(posts[0])
    assert "all-time published" not in _desc(posts[0])


def test_two_runs_accumulate_published_counter(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    install(("posted", None))
    asyncio.run(m.main_async(cfg, None))
    asyncio.run(m.main_async(cfg, None))
    assert len(posts) == 2
    assert "all-time published: 2" in _desc(posts[-1])


def test_failed_group_records_error_rc1_and_notifies(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    install(("failed", "flow_error: x"))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 1
    line, = _ledger_lines(cfg)
    assert line["status"] == "failed" and line["error"] == "flow_error: x"
    assert len(posts) == 1
    assert posts[0]["embeds"][0]["color"] == notify.COLOR_BAD
    assert "❌ TEST GROUP" in _desc(posts[0]) and "flow_error: x" in _desc(posts[0])


def test_rotation_prefers_never_attempted_group(tmp_path, monkeypatch):
    cfg, _, _ = _make_harness(tmp_path, monkeypatch, joined=JOINED2)
    cfg.max_posts_per_run = 1
    # seed: group 42 already published earlier today; 43 never attempted
    cfg.ledger_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ledger_file.write_text(json.dumps(
        {"group_id": "42", "name": "TEST GROUP", "status": "published",
         "ts": "2026-09-21T10:00:00+00:00"}) + "\n", encoding="utf-8")
    seen = []

    async def spy_run_group(page, group, post, cfg_, log):
        seen.append(group["id"])
        return "posted", None
    monkeypatch.setattr(m, "run_group", spy_run_group)
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 0
    assert seen == ["43"]  # the NEVER-attempted group, not the fresher 42


def test_skip_records_skipped_and_notifies(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    install(("skipped", "no composer within 5s (not enabled for identity)"))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 1  # nothing posted => run reports failure code
    line, = _ledger_lines(cfg)
    assert line["status"] == "skipped" and line["group_id"] == "42"
    assert len(posts) == 1
    assert "⏭️ TEST GROUP" in _desc(posts[0])


def test_login_abort_notifies_with_reason(tmp_path, monkeypatch):
    cfg, posts, _ = _make_harness(tmp_path, monkeypatch)

    async def bad_login(ctx, page, log, login_timeout=900):
        return False
    monkeypatch.setattr(m, "ensure_login", bad_login)
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 2
    assert _ledger_lines(cfg) == []  # nothing attempted -> nothing recorded
    assert len(posts) == 1 and "not logged in" in _desc(posts[0])


def test_profile_abort_notifies_with_reason(tmp_path, monkeypatch):
    cfg, posts, _ = _make_harness(tmp_path, monkeypatch)

    async def bad_profile(ctx, page, *, post_as, posting_user_id,
                          main_user_id, posting_name, main_profile_name="",
                          log=None):
        return False
    monkeypatch.setattr(m, "ensure_active_profile", bad_profile)
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 3
    assert len(posts) == 1 and "TestProfile" in _desc(posts[0])


def test_missing_post_txt_notifies_abort(tmp_path, monkeypatch):
    cfg, posts, _ = _make_harness(tmp_path, monkeypatch)
    cfg.post_text_file.unlink()
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 1
    assert len(posts) == 1 and "missing data/post.txt" in _desc(posts[0])


def test_crash_still_notifies_once_and_reraises(tmp_path, monkeypatch):
    cfg, posts, _ = _make_harness(tmp_path, monkeypatch)
    _install_boom(monkeypatch)
    with pytest.raises(RuntimeError, match="kaput"):
        asyncio.run(m.main_async(cfg, None))
    assert len(posts) == 1
    assert "run crashed" in _desc(posts[0]) and "kaput" in _desc(posts[0])


def test_empty_webhook_sends_no_http_but_ledger_writes(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    cfg.discord_webhook_url = ""
    install(("posted", None))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 0
    assert posts == []  # no network
    assert _ledger_lines(cfg)[0]["status"] == "published"  # ledger still recorded


def test_dead_webhook_never_changes_rc(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)

    def explode(req, timeout=None):
        raise OSError("no network")
    monkeypatch.setattr(notify.urllib.request, "urlopen", explode)
    install(("posted", None))
    rc = asyncio.run(m.main_async(cfg, None))
    assert rc == 0  # monitoring failure must not affect the run
    assert posts == []


def test_finish_latch_sends_once_even_if_called_twice(tmp_path, monkeypatch):
    cfg, posts, install = _make_harness(tmp_path, monkeypatch)
    install(("posted", None))
    rec = m.RunRecorder(ledger_path=cfg.ledger_file, run_id="L", dry_run=False)
    finish = m.make_finish(cfg, rec)
    finish(None, print)
    finish("late second call", print)
    assert len(posts) == 1 and "TEST" not in _desc(posts[0])  # empty summary, not aborted


def test_finish_swallows_payload_errors(tmp_path, monkeypatch, capsys):
    cfg, posts, _ = _make_harness(tmp_path, monkeypatch)
    rec = m.RunRecorder(ledger_path=cfg.ledger_file, run_id="E", dry_run=False)

    def boom(*a, **k):
        raise ValueError("bad payload")
    monkeypatch.setattr(m, "build_payload", boom)
    finish = m.make_finish(cfg, rec)
    finish("x", print)  # must NOT raise
    out = capsys.readouterr().out
    assert "monitor: ValueError: bad payload" in out
    assert posts == []
