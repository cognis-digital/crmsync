"""Command-line interface for CRMSYNC.

Examples
--------
Detect drift between a versioned export and the local DB (CI gate)::

    crmsync diff contacts.csv --db crm.db --table contacts --key email
    # exit code 1 if the file drifted from the DB, 0 if in sync

Sync the DB to match the export (idempotent)::

    crmsync apply contacts.csv --db crm.db --table contacts --key email

Machine-readable output for pipelines::

    crmsync diff contacts.json --db crm.db --key email --format json | jq .
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import List, Optional

from . import TOOL_NAME, TOOL_VERSION
from .core import (
    SyncPlan,
    apply_plan,
    diff_records,
    load_db_state,
    load_export,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description=(
            "Idempotent sync & drift detection between a local SQLite "
            "canonical store and a versioned CRM export file (CSV/JSON)."
        ),
        epilog=(
            "examples:\n"
            "  crmsync diff contacts.csv --db crm.db --key email\n"
            "  crmsync apply contacts.csv --db crm.db --key email\n"
            "  crmsync diff deals.json --db crm.db --key deal_id --format json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}"
    )
    sub = p.add_subparsers(dest="command", required=True)

    common_parents = []

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("export", help="CRM export file (.csv, .tsv or .json)")
        sp.add_argument(
            "--db", required=True, help="path to local SQLite database"
        )
        sp.add_argument(
            "--table",
            default="contacts",
            help="canonical table name (default: contacts)",
        )
        sp.add_argument(
            "--key",
            default="email",
            help="business key field (default: email)",
        )
        sp.add_argument(
            "--format",
            choices=("table", "json"),
            default="table",
            help="output format (default: table)",
        )

    d = sub.add_parser(
        "diff",
        help="show drift between export and DB (exit 1 if drift found)",
        parents=common_parents,
    )
    add_common(d)

    a = sub.add_parser(
        "apply",
        help="sync DB to match export (idempotent)",
        parents=common_parents,
    )
    add_common(a)
    a.add_argument(
        "--no-delete",
        action="store_true",
        help="do not delete DB rows missing from the export",
    )

    return p


def _render_table(plan: SyncPlan, header: str) -> str:
    lines = [header, "  " + plan.summary()]
    drift = plan.adds + plan.updates + plan.deletes
    if not drift:
        lines.append("  no drift: export matches DB")
        return "\n".join(lines)
    width = max(len(c.key) for c in drift)
    for c in drift:
        extra = ""
        if c.action == "update" and c.fields:
            extra = "  (" + ", ".join(c.fields) + ")"
        lines.append(f"  {c.action.upper():<9} {c.key:<{width}}{extra}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        export_rows = load_export(args.export)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: failed to read export {args.export!r}: {exc}", file=sys.stderr)
        return 2

    try:
        db_state = load_db_state(args.db, args.table)
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: failed to read DB {args.db!r}: {exc}", file=sys.stderr)
        return 2

    try:
        plan = diff_records(db_state, export_rows, key=args.key)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "apply":
        try:
            applied = apply_plan(
                args.db,
                args.table,
                plan,
                allow_delete=not args.no_delete,
            )
        except (sqlite3.Error, ValueError) as exc:
            print(f"error: failed to apply changes to {args.db!r}: {exc}", file=sys.stderr)
            return 2
        if args.format == "json":
            print(json.dumps({"applied": applied, "plan": plan.to_dict()}, indent=2))
        else:
            print(_render_table(plan, f"apply -> {args.table}"))
            print(
                "  applied: "
                f"add={applied['add']} update={applied['update']} "
                f"delete={applied['delete']}"
            )
        # apply succeeds (0) once the DB matches the export.
        return 0

    # diff command
    if args.format == "json":
        print(json.dumps(plan.to_dict(), indent=2))
    else:
        print(_render_table(plan, f"diff {args.export} vs {args.table}"))

    # Exit non-zero when drift is detected so it works as a CI gate.
    return 1 if plan.has_drift else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
