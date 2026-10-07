"""Pins for poster.groups_matrix — the dashboard's per-listing "groups matrix".

The module itself is pure stdlib; the ONLY playwright-touching import is the
fold-parity check against poster.flows._norm, and it lives INSIDE that one test
so importing this file never drags playwright in.
"""
import json

import pytest

from poster.groups_matrix import (
    MatrixError,
    disabled_for,
    filter_plan,
    fold_name,
    matrix_path,
    read_matrix,
    save_matrix,
)

#: real marketplace listing ids from the recordings (all-digit strings)
L1 = "1178670714486639"
L2 = "1923574311937261"


# ---------------------------------------------------------------------------
# fail-safe direction: nothing OFF means everything enabled
# ---------------------------------------------------------------------------

def test_missing_file_means_everything_enabled(tmp_path):
    root = tmp_path / "capture"
    m = read_matrix(root)
    assert m["listings"] == {}
    assert m["path"] == str(matrix_path(root))
    assert disabled_for(root, L1) == set()
    plan = [{"id": "1", "name": "Venta de Carros Chihuahua"}]
    assert filter_plan(plan, disabled_for(root, L1)) == (plan, [])


def test_corrupt_file_never_raises_and_reports_error(tmp_path):
    root = tmp_path / "capture"
    p = matrix_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")

    m = read_matrix(root)  # MUST NOT raise
    assert m["listings"] == {}
    assert "error" in m
    assert m["path"] == str(p)
    assert disabled_for(root, L1) == set()


def test_non_object_json_is_treated_as_corrupt(tmp_path):
    root = tmp_path / "capture"
    p = matrix_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2, 3]", encoding="utf-8")
    m = read_matrix(root)
    assert m["listings"] == {}
    assert "error" in m


# ---------------------------------------------------------------------------
# save: folding, dedupe, per-listing isolation, timestamp
# ---------------------------------------------------------------------------

def test_save_folds_dedupes_and_is_per_listing(tmp_path):
    root = tmp_path / "capture"
    save_matrix(root, L1, ["Venta  de Carros", "VENTA DE CARROS"])
    save_matrix(root, L2, ["Otro Grupo"])

    m = read_matrix(root)
    # two case/space variants collapse into ONE folded key
    assert m["listings"][L1]["disabled"] == ["venta de carros"]
    # saving L1 did not clobber L2
    assert m["listings"][L2]["disabled"] == ["otro grupo"]
    assert m["updated_at"]  # stamped

    # clearing L1 empties only L1, L2 survives
    out = save_matrix(root, L1, [])
    assert out["listings"][L1]["disabled"] == []
    assert out["listings"][L2]["disabled"] == ["otro grupo"]


def test_save_writes_readable_json_layout(tmp_path):
    root = tmp_path / "capture"
    save_matrix(root, L1, ["Ávila 🚗", "otro"])
    raw = matrix_path(root).read_text(encoding="utf-8")
    data = json.loads(raw)  # valid JSON on disk
    assert data["version"] == 1
    assert data["listings"][L1]["disabled"] == ["otro", "ávila 🚗"]  # sorted, folded
    assert "\\u" not in raw  # ensure_ascii=False -> real unicode bytes


# ---------------------------------------------------------------------------
# save: validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "abc", "../..", None])
def test_save_rejects_bad_listing_ids(tmp_path, bad):
    with pytest.raises(MatrixError):
        save_matrix(tmp_path, bad, [])


@pytest.mark.parametrize("bad", ["nope", None, {"a": 1}, 5, 0.5])
def test_save_rejects_non_list_disabled(tmp_path, bad):
    with pytest.raises(MatrixError):
        save_matrix(tmp_path, L1, bad)


# ---------------------------------------------------------------------------
# filter_plan
# ---------------------------------------------------------------------------

def test_filter_plan_drops_only_disabled_and_keeps_unknown():
    plan = [
        {"id": "1", "name": "Venta de Carros Chihuahua"},
        {"id": "2", "name": "Otro Grupo Desconocido"},
        {"id": "3", "name": "Venta  DE carros CHIHUAHUA"},  # folds same as #1
    ]
    disabled = {fold_name("Venta de Carros Chihuahua")}
    kept, dropped = filter_plan(plan, disabled)
    assert [g["id"] for g in kept] == ["2"]  # unknown name KEPT
    assert [g["id"] for g in dropped] == ["1", "3"]


def test_filter_plan_empty_disabled_is_noop():
    plan = [{"id": "1", "name": "x"}]
    kept, dropped = filter_plan(plan, set())
    assert kept == plan
    assert kept is plan  # fast path: same object, no copy
    assert dropped == []


# ---------------------------------------------------------------------------
# fold parity with poster.flows._norm (the pipeline's own folding)
# ---------------------------------------------------------------------------

def test_fold_name_matches_flows_norm():
    from poster.flows import _norm  # local import: playwright only for this test

    names = [
        "Venta de Carros Chihuahua",
        "VENTA DE CARROS  CHIHUAHUA",
        "Comprá y Vende Autos 🚗",
        "  Autos\nNuevos\tLeón  ",
        "A\u0301vila",          # NFD á (decomposed) -> NFC on both sides
        "Café ☕️ CDMX",         # emoji + variation selector
        "Autos",
        "",
    ]
    for s in names:
        assert fold_name(s) == _norm(s), repr(s)
