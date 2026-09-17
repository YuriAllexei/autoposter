"""Unit tests for the poster package (no browser): config, ordering, dispatch."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from poster import flows
from poster.config import load_config
from poster.photos import collect_photos

REPO = Path(__file__).resolve().parents[1]


def test_config_defaults_fail_safe_dry_run(tmp_path):
    cfg = load_config(env_file=tmp_path / "absent.env")
    assert cfg.dry_run is True  # missing config => dry run, non-negotiable


def test_config_env_file_and_real_env_override(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AP_DRY_RUN=false\nAP_DELAY_MIN_SECONDS=5\nAP_DELAY_MAX_SECONDS=9\n")
    monkeypatch.delenv("AP_DRY_RUN", raising=False)
    cfg = load_config(env_file=env)
    assert cfg.dry_run is False
    assert (cfg.delay_min, cfg.delay_max) == (5.0, 7.0)  # 9 clamped to hard cap
    monkeypatch.setenv("AP_DRY_RUN", "true")  # real env wins over file
    assert load_config(env_file=env).dry_run is True


def test_delay_bounds_validated(tmp_path):
    env = tmp_path / ".env"
    env.write_text("AP_DELAY_MIN_SECONDS=100\nAP_DELAY_MAX_SECONDS=50\n")
    with pytest.raises(ValueError, match="delay"):
        load_config(env_file=env)


def test_delay_hard_capped_at_7s(tmp_path):
    # user rule: no wait may exceed 7s no matter what .env says; spread kept
    env = tmp_path / ".env"
    env.write_text("AP_DELAY_MIN_SECONDS=45\nAP_DELAY_MAX_SECONDS=180\n")
    cfg = load_config(env_file=env)
    assert cfg.delay_max == 7.0
    assert 0 <= cfg.delay_min < cfg.delay_max


def test_random_sleep_within_cap():
    import asyncio
    import random as rnd

    from poster.config import MAX_ALLOWED_DELAY
    from poster.flows import human_sleep

    cfg = load_config(env_file=REPO / "nonexistent.env")  # defaults -> clamped
    vals = []
    real_uniform = rnd.uniform

    def fake_uniform(a, b):
        v = real_uniform(a, b)
        vals.append(v)
        return v

    orig = asyncio.sleep
    asyncio.sleep = lambda s: orig(0)
    try:
        rnd.uniform = fake_uniform
        for _ in range(200):
            asyncio.run(human_sleep(cfg, lambda *_: None))
    finally:
        rnd.uniform = real_uniform
        asyncio.sleep = orig
    assert vals and max(vals) <= MAX_ALLOWED_DELAY
    assert len(set(vals)) > 1  # random, not a constant


def test_collect_photos_sorted(tmp_path):
    root = tmp_path / "car_photos"
    for car in ("02_suv", "01_truck"):
        (root / car).mkdir(parents=True)
    (root / "01_truck").joinpath("b.jpg").write_bytes(b"x")
    (root / "01_truck").joinpath("a.png").write_bytes(b"x")
    (root / "02_suv").joinpath("c.webp").write_bytes(b"x")
    (root / "02_suv").joinpath("notes.txt").write_text("skip me")
    (root / "unnumbered_stray.jpg").write_bytes(b"x")  # file at root, not a car folder

    cars = collect_photos(root, [".jpg", ".png", ".webp"], max_photos=10)
    assert [c.name for c in cars] == ["01_truck", "02_suv"]
    assert [p.name for p in cars[0].files] == ["a.png", "b.jpg"]
    assert cars[1].files == [root / "02_suv" / "c.webp"]


def test_collect_photos_global_cap_keeps_order(tmp_path):
    root = tmp_path / "p"
    for i in range(3):
        d = root / f"0{i}_a"
        d.mkdir(parents=True)
        for j in range(3):
            (d / f"{j}.jpg").write_bytes(b"x")
    cars = collect_photos(root, [".jpg"], max_photos=5)
    assert [len(c.files) for c in cars] == [3, 2]  # tail trimmed, order intact
    assert [c.name for c in cars] == ["00_a", "01_a"]  # over-cap car dropped


def test_nested_folders_are_not_merged(tmp_path):
    root = tmp_path / "p"
    car = root / "01_x"
    (car / "sub").mkdir(parents=True)
    (car / "1.jpg").write_bytes(b"x")
    (car / "sub" / "2.jpg").write_bytes(b"x")
    cars = collect_photos(root, [".jpg"], max_photos=10)
    assert [p.name for c in cars for p in c.files] == ["1.jpg"]


def test_collect_photos_empty_dir_is_ok(tmp_path):
    assert collect_photos(tmp_path / "nothing", [".jpg"], max_photos=10) == []


def test_dispatcher_unknown_code_hard_errors():
    with pytest.raises(KeyError):
        flows.get_flow("definitely_not_a_flow")


def test_dispatcher_resolves_real_flow():
    fn = flows.get_flow("group_composer_es_v1")
    assert callable(fn) and fn.__name__ == "group_composer_es_v1"


def test_flow_registry_covers_groups_file():
    groups = json.loads((REPO / "data/groups.json").read_text(encoding="utf-8"))
    for g in groups["groups"]:
        assert g["posting_code"] in flows.REGISTRY, g["posting_code"]
