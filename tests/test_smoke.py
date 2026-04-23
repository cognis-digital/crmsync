"""Smoke tests for CRMSYNC. No network. Operates on the bundled demo file."""
import json
import sqlite3
from pathlib import Path

import pytest

from crmsync import (
    TOOL_NAME,
    TOOL_VERSION,
    apply_plan,
    diff_records,
    load_db_state,
    load_export,
)
from crmsync.cli import main

DEMO = Path(__file__).resolve().parent.parent / "demos" / "01-basic" / "contacts.csv"


def test_metadata():
    assert TOOL_NAME == "crmsync"
    assert TOOL_VERSION.count(".") == 2


def test_load_export_reads_demo():
    rows = load_export(DEMO)
    assert len(rows) == 4
    assert {r["email"] for r in rows} >= {"ada@analyticeng.example"}
    assert rows[0]["stage"] in {"customer", "lead", "opportunity"}


def test_fresh_db_is_all_adds_and_drifts():
    rows = load_export(DEMO)
    plan = diff_records({}, rows, key="email")
    assert plan.counts()["add"] == 4
    assert plan.has_drift is True


def test_apply_then_diff_is_idempotent(tmp_path):
    db = tmp_path / "crm.db"
    rows = load_export(DEMO)

    plan = diff_records(load_db_state(db, "contacts"), rows, key="email")
    applied = apply_plan(db, "contacts", plan, allow_delete=True)
    assert applied["add"] == 4

    # Re-diff against same file: nothing should change.
    plan2 = diff_records(load_db_state(db, "contacts"), rows, key="email")
    assert plan2.has_drift is False
    assert plan2.counts()["unchanged"] == 4

    # Re-applying is a no-op.
    applied2 = apply_plan(db, "contacts", plan2, allow_delete=True)
    assert applied2 == {"add": 0, "update": 0, "delete": 0}


def test_update_and_delete_detection(tmp_path):
    db = tmp_path / "crm.db"
    rows = load_export(DEMO)
    apply_plan(db, "contacts", diff_records({}, rows, key="email"))

    # Mutate one record (stage change) and drop one record entirely.
    mutated = [dict(r) for r in rows if r["email"] != "grace@navy.example"]
    for r in mutated:
        if r["email"] == "alan@bletchley.example":
            r["stage"] = "customer"

    plan = diff_records(load_db_state(db, "contacts"), mutated, key="email")
    assert plan.counts()["update"] == 1
    assert plan.counts()["delete"] == 1
    upd = plan.updates[0]
    assert upd.key == "alan@bletchley.example"
    assert "stage" in upd.fields
    assert plan.deletes[0].key == "grace@navy.example"


def test_no_delete_flag_keeps_rows(tmp_path):
    db = tmp_path / "crm.db"
    rows = load_export(DEMO)
    apply_plan(db, "contacts", diff_records({}, rows, key="email"))

    smaller = [r for r in rows if r["email"] != "grace@navy.example"]
    plan = diff_records(load_db_state(db, "contacts"), smaller, key="email")
    applied = apply_plan(db, "contacts", plan, allow_delete=False)
    assert applied["delete"] == 0
    # Row still present.
    assert "grace@navy.example" in load_db_state(db, "contacts")


def test_json_export_supported(tmp_path):
    src = tmp_path / "deals.json"
    src.write_text(
        json.dumps(
            [
                {"deal_id": "D1", "amount": 1000, "stage": "open"},
                {"deal_id": "D2", "amount": 2500, "stage": "won"},
            ]
        ),
        encoding="utf-8",
    )
    rows = load_export(src)
    plan = diff_records({}, rows, key="deal_id")
    assert plan.counts()["add"] == 2


def test_duplicate_key_in_export_raises(tmp_path):
    rows = [
        {"email": "x@y.example", "name": "A"},
        {"email": "x@y.example", "name": "B"},
    ]
    with pytest.raises(ValueError):
        diff_records({}, rows, key="email")


def test_cli_diff_exit_codes(tmp_path, capsys):
    db = tmp_path / "crm.db"
    # Fresh DB -> drift -> exit 1
    rc = main(["diff", str(DEMO), "--db", str(db), "--key", "email"])
    assert rc == 1

    # Apply -> exit 0
    rc = main(["apply", str(DEMO), "--db", str(db), "--key", "email"])
    assert rc == 0

    # Re-diff -> no drift -> exit 0
    rc = main(["diff", str(DEMO), "--db", str(db), "--key", "email"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no drift" in out


def test_cli_json_format(tmp_path, capsys):
    db = tmp_path / "crm.db"
    rc = main(
        ["diff", str(DEMO), "--db", str(db), "--key", "email", "--format", "json"]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["has_drift"] is True
    assert payload["counts"]["add"] == 4
