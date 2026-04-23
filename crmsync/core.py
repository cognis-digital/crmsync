"""Core sync engine for CRMSYNC.

The engine is deliberately tiny and dependency-free. It treats a CRM export
file (CSV or JSON array of objects) as the *incoming* state and a SQLite
table as the *canonical* state. It computes a deterministic, idempotent plan
of changes (add / update / delete / unchanged) keyed on a stable business key
(e.g. an email address or deal id).

Key ideas
---------
* **Idempotent**: applying a plan and then re-diffing yields zero changes.
* **Drift detection**: any add/update/delete is \"drift\" between the file and
  the DB. ``SyncPlan.has_drift`` powers CI exit codes.
* **Stable hashing**: each record gets a content fingerprint over its
  non-key fields so updates are detected on real value changes only.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

Record = Dict[str, Any]

_ACTIONS = ("add", "update", "delete", "unchanged")


def _normalize(value: Any) -> str:
    """Normalize a single field value to a stable string form.

    None and empty become the same, surrounding whitespace is trimmed, and
    everything is compared case-sensitively except keys are normalized by the
    caller. This keeps fingerprints stable across CSV (always str) and JSON
    (typed) sources.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # render integers-as-floats cleanly: 3.0 -> "3"
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def fingerprint(record: Record, key: str) -> str:
    """Stable content hash over all non-key fields of a record."""
    items = sorted(
        (k.strip().lower(), _normalize(v))
        for k, v in record.items()
        if k.strip().lower() != key.strip().lower()
    )
    blob = "\x1f".join(f"{k}={v}" for k, v in items)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _key_of(record: Record, key: str) -> str:
    kl = key.strip().lower()
    for k, v in record.items():
        if k.strip().lower() == kl:
            return _normalize(v)
    raise KeyError(f"key field {key!r} not found in record: {sorted(record)}")


@dataclass
class Change:
    """A single planned change for one record."""

    action: str  # one of _ACTIONS
    key: str
    before: Optional[Record] = None  # DB side
    after: Optional[Record] = None  # export side
    fields: List[str] = field(default_factory=list)  # changed field names

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "key": self.key,
            "fields": self.fields,
            "before": self.before,
            "after": self.after,
        }


@dataclass
class SyncPlan:
    """The full set of changes between DB state and an export."""

    key: str
    changes: List[Change] = field(default_factory=list)

    @property
    def adds(self) -> List[Change]:
        return [c for c in self.changes if c.action == "add"]

    @property
    def updates(self) -> List[Change]:
        return [c for c in self.changes if c.action == "update"]

    @property
    def deletes(self) -> List[Change]:
        return [c for c in self.changes if c.action == "delete"]

    @property
    def unchanged(self) -> List[Change]:
        return [c for c in self.changes if c.action == "unchanged"]

    @property
    def has_drift(self) -> bool:
        """True if the export differs from the DB in any way."""
        return bool(self.adds or self.updates or self.deletes)

    def counts(self) -> Dict[str, int]:
        return {a: sum(1 for c in self.changes if c.action == a) for a in _ACTIONS}

    def summary(self) -> str:
        c = self.counts()
        return (
            f"add={c['add']} update={c['update']} "
            f"delete={c['delete']} unchanged={c['unchanged']}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "counts": self.counts(),
            "has_drift": self.has_drift,
            "changes": [
                c.to_dict() for c in self.changes if c.action != "unchanged"
            ],
        }


def _coerce_rows(rows: Iterable[Any]) -> List[Record]:
    out: List[Record] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"row {i} is not an object/dict: {row!r}")
        out.append({str(k): row[k] for k in row})
    return out


def load_export(path: str | Path) -> List[Record]:
    """Load a CRM export file. Supports .csv and .json (array of objects)."""
    p = Path(path)
    suffix = p.suffix.lower()
    text = p.read_text(encoding="utf-8-sig")
    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            # allow {\"records\": [...]} envelope
            data = data.get("records", data.get("data", []))
        if not isinstance(data, list):
            raise ValueError("JSON export must be an array of objects")
        return _coerce_rows(data)
    if suffix in (".csv", ".tsv", ""):
        delim = "\t" if suffix == ".tsv" else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delim)
        return _coerce_rows(reader)
    raise ValueError(f"unsupported export type: {suffix!r}")


def ensure_schema(conn: sqlite3.Connection, table: str) -> None:
    """Create the canonical table if missing.

    The table stores the business key, a JSON blob of the full record, and the
    content fingerprint so future diffs are O(1) per row.
    """
    _validate_ident(table)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{table}" ('
        "  key TEXT PRIMARY KEY,"
        "  data TEXT NOT NULL,"
        "  fingerprint TEXT NOT NULL"
        ")"
    )
    conn.commit()


def _validate_ident(name: str) -> None:
    if not name or not all(ch.isalnum() or ch == "_" for ch in name):
        raise ValueError(f"invalid SQL identifier: {name!r}")


def load_db_state(
    db_path: str | Path, table: str
) -> Dict[str, Record]:
    """Load canonical records from the DB keyed by business key.

    Returns an empty mapping if the table does not exist yet (fresh DB).
    """
    _validate_ident(table)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if row is None:
            return {}
        out: Dict[str, Record] = {}
        for key, data, _fp in conn.execute(
            f'SELECT key, data, fingerprint FROM "{table}"'
        ):
            out[key] = json.loads(data)
        return out
    finally:
        conn.close()


def diff_records(
    db_state: Dict[str, Record],
    export_rows: List[Record],
    key: str,
) -> SyncPlan:
    """Compute an idempotent plan from DB state -> export rows.

    * keys in export but not DB    -> add
    * keys in both, fingerprint != -> update (with changed field list)
    * keys in DB but not export    -> delete
    * keys in both, fingerprint == -> unchanged
    """
    plan = SyncPlan(key=key)
    seen = set()

    export_by_key: Dict[str, Record] = {}
    for r in export_rows:
        k = _key_of(r, key)
        if k == "":
            raise ValueError(f"export row has empty key {key!r}: {r!r}")
        if k in export_by_key:
            raise ValueError(f"duplicate key in export: {k!r}")
        export_by_key[k] = r

    for k, after in export_by_key.items():
        seen.add(k)
        before = db_state.get(k)
        if before is None:
            plan.changes.append(Change("add", k, after=after))
            continue
        fp_before = fingerprint(before, key)
        fp_after = fingerprint(after, key)
        if fp_before == fp_after:
            plan.changes.append(Change("unchanged", k, before=before, after=after))
        else:
            plan.changes.append(
                Change(
                    "update",
                    k,
                    before=before,
                    after=after,
                    fields=_changed_fields(before, after, key),
                )
            )

    for k, before in db_state.items():
        if k not in seen:
            plan.changes.append(Change("delete", k, before=before))

    plan.changes.sort(key=lambda c: (_ACTIONS.index(c.action), c.key))
    return plan


def _changed_fields(before: Record, after: Record, key: str) -> List[str]:
    kl = key.strip().lower()
    bn = {k.strip().lower(): _normalize(v) for k, v in before.items()}
    an = {k.strip().lower(): _normalize(v) for k, v in after.items()}
    fields = set(bn) | set(an)
    fields.discard(kl)
    return sorted(f for f in fields if bn.get(f, "") != an.get(f, ""))


def apply_plan(
    db_path: str | Path,
    table: str,
    plan: SyncPlan,
    *,
    allow_delete: bool = True,
) -> Dict[str, int]:
    """Apply a plan to the DB so the DB matches the export. Idempotent.

    Returns the count of rows actually written/removed. Re-running ``diff`` +
    ``apply`` immediately after returns all-zero counts.
    """
    _validate_ident(table)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn, table)
        applied = {"add": 0, "update": 0, "delete": 0}
        for c in plan.adds + plan.updates:
            data = json.dumps(c.after, sort_keys=True, ensure_ascii=False)
            fp = fingerprint(c.after, plan.key)
            conn.execute(
                f'INSERT INTO "{table}" (key, data, fingerprint) '
                "VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET data=excluded.data, "
                "fingerprint=excluded.fingerprint",
                (c.key, data, fp),
            )
            applied[c.action] += 1
        if allow_delete:
            for c in plan.deletes:
                conn.execute(f'DELETE FROM "{table}" WHERE key=?', (c.key,))
                applied["delete"] += 1
        conn.commit()
        return applied
    finally:
        conn.close()
