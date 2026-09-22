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