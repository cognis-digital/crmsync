"""Hardening tests: error paths, edge cases, and robustness checks."""
from __future__ import annotations

import importlib
import sqlite3
import stat
from pathlib import Path


from crmsync.cli import main
from crmsync.core import diff_records

DEMO = Path(__file__).resolve().parent.parent / "demos" / "01-basic" / "contacts.csv"


def test_cli_missing_export_file_returns_2(tmp_path, capsys):
    """Missing export file should print a message to stderr and exit 2."""
    db = tmp_path / "crm.db"
    rc = main(["diff", str(tmp_path / "nonexistent.csv"), "--db", str(db), "--key", "email"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_cli_malformed_json_export_returns_2(tmp_path, capsys):
    """Malformed JSON export should exit 2 with a clear error, not a traceback."""
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json {{ ", encoding="utf-8")
    db = tmp_path / "crm.db"
    rc = main(["diff", str(bad), "--db", str(db), "--key", "id"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_cli_key_field_missing_from_csv_returns_2(tmp_path, capsys):
    """If the key column is absent from the CSV, exit 2 with a clear message."""
    src = tmp_path / "nokey.csv"
    src.write_text("name,stage\nAlice,lead\n", encoding="utf-8")
    db = tmp_path / "crm.db"
    rc = main(["diff", str(src), "--db", str(db), "--key", "email"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "email" in err


def test_cli_apply_write_failure_returns_2(tmp_path, capsys):
    """A write failure on apply should exit 2 with a clear message, not a traceback."""
    db = tmp_path / "readonly.db"
    # Create a valid DB then make it read-only so writes will fail.
    conn = sqlite3.connect(str(db))
    conn.close()
    db.chmod(stat.S_IRUSR | stat.S_IRGRP)
    rc = main(["apply", str(DEMO), "--db", str(db), "--key", "email"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error" in err.lower()


def test_empty_export_produces_no_drift():
    """An empty export produces zero drift against an empty DB state."""
    plan = diff_records({}, [], key="email")
    assert plan.has_drift is False
    assert plan.counts()["add"] == 0
    assert plan.counts()["unchanged"] == 0


def test_mcp_server_module_imports_cleanly():
    """mcp_server must be importable even though scan/to_json are not in core."""
    mod = importlib.import_module("crmsync.mcp_server")
    assert callable(mod.serve)
