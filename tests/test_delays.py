"""TDD RED-GREEN guard for the retuned anti-detection sleep range (2026-09-22).

Operating range is 2-4s (was 3-7s). poster/config.py:MAX_ALLOWED_DELAY=7.0
stays as a safety CEILING, not the operating range — the cap regression test
below pins that it still bites.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster.config import MAX_ALLOWED_DELAY, load_config

REPO = Path(__file__).resolve().parents[1]


def _clear_delay_env(monkeypatch):
    # a shell that exported these would otherwise win over the file under test
    monkeypatch.delenv("AP_DELAY_MIN_SECONDS", raising=False)
    monkeypatch.delenv("AP_DELAY_MAX_SECONDS", raising=False)


def test_temp_env_2_4_parses_exactly(tmp_path, monkeypatch):
    _clear_delay_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AP_DELAY_MIN_SECONDS=2\nAP_DELAY_MAX_SECONDS=4\n")
    cfg = load_config(env_file=env)
    assert cfg.delay_min == 2.0
    assert cfg.delay_max == 4.0


def test_shipped_env_example_is_2_4(monkeypatch):
    _clear_delay_env(monkeypatch)
    cfg = load_config(env_file=REPO / ".env.example")
    assert (cfg.delay_min, cfg.delay_max) == (2.0, 4.0)


def test_hard_cap_still_applies_at_7s(tmp_path, monkeypatch):
    # safety ceiling unchanged: env asking for 20 -> 7.0, min re-spread per
    # config logic (delay_min = max(0.0, delay_max - 4.0))
    _clear_delay_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AP_DELAY_MIN_SECONDS=15\nAP_DELAY_MAX_SECONDS=20\n")
    cfg = load_config(env_file=env)
    assert MAX_ALLOWED_DELAY == 7.0
    assert cfg.delay_max == 7.0
    assert cfg.delay_min == 3.0


# ---- group-to-group switch sleep (explicit user rule 2026-09-22: 10-15s) ----


def _clear_switch_env(monkeypatch):
    monkeypatch.delenv("AP_GROUP_SWITCH_MIN_SECONDS", raising=False)
    monkeypatch.delenv("AP_GROUP_SWITCH_MAX_SECONDS", raising=False)


def test_shipped_env_example_group_switch_is_10_15(monkeypatch):
    _clear_delay_env(monkeypatch)
    _clear_switch_env(monkeypatch)
    cfg = load_config(env_file=REPO / ".env.example")
    assert (cfg.group_switch_min, cfg.group_switch_max) == (10.0, 15.0)


def test_group_switch_defaults_are_10_15(tmp_path, monkeypatch):
    # absent the env keys, the range is still 10-15 (config-level rule)
    _clear_switch_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AP_DELAY_MIN_SECONDS=2\nAP_DELAY_MAX_SECONDS=4\n")
    cfg = load_config(env_file=env)
    assert (cfg.group_switch_min, cfg.group_switch_max) == (10.0, 15.0)


def test_group_switch_own_ceiling_is_20s(tmp_path, monkeypatch):
    # group switch is exempt from MAX_ALLOWED_DELAY (it is 10-15 BY DESIGN)
    # but capped by its own 20s ceiling so a .env typo can't idle for minutes
    _clear_switch_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AP_GROUP_SWITCH_MIN_SECONDS=10\n"
                   "AP_GROUP_SWITCH_MAX_SECONDS=900\n")
    cfg = load_config(env_file=env)
    assert cfg.group_switch_max == 20.0
    assert cfg.group_switch_min <= cfg.group_switch_max


def test_group_switch_invalid_range_raises(tmp_path, monkeypatch):
    _clear_switch_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text("AP_GROUP_SWITCH_MIN_SECONDS=20\n"
                   "AP_GROUP_SWITCH_MAX_SECONDS=5\n")
    import pytest
    with pytest.raises(ValueError):
        load_config(env_file=env)


def test_human_sleep_honours_lo_hi_override(monkeypatch):
    # the 10-15s gap is delivered via human_sleep's lo/hi override (same
    # symbol the integration tests stub). Capture what random.uniform was
    # asked to draw instead of patching stdlib asyncio.sleep globally.
    from poster import flows
    from poster.config import Config
    cfg = Config(delay_min=2.0, delay_max=4.0,
                 group_switch_min=10.0, group_switch_max=15.0)
    drawn = []

    async def no_await(_):
        return None

    def fake_uniform(lo, hi):
        drawn.append((lo, hi))
        return lo
    monkeypatch.setattr(flows.random, "uniform", fake_uniform)
    monkeypatch.setattr(flows.asyncio, "sleep", no_await)
    import asyncio as _a
    _a.run(flows.human_sleep(cfg, lambda m: None, "x",
                             cfg.group_switch_min, cfg.group_switch_max))
    _a.run(flows.human_sleep(cfg, lambda m: None, "y"))
    assert drawn == [(10.0, 15.0), (2.0, 4.0)]  # override wins; default stays