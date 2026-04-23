# Demo 01 - Basic drift detection & idempotent sync

This demo shows CRMSYNC detecting that a versioned CRM export file has
**drifted** from the canonical local SQLite store, then syncing the DB so the
two match.

## Files

- `contacts.csv` - a CRM contacts export, keyed on `email`.

## What happens

1. Start with a fresh DB (no `contacts` table yet). Every row in the export is
   an **add**, so `diff` reports drift and exits non-zero.
2. Run `apply` to write those rows into the DB.
3. Run `diff` again against the **same** file: now everything is `unchanged`,
   drift is gone, and the exit code is `0`. This proves the sync is
   idempotent.
4. If a value in the file later changes (e.g. a contact's `stage` moves from
   `lead` to `customer`), `diff` flags exactly that record as an `UPDATE`
   with the changed field name, and a removed row shows up as a `DELETE`.

## Run it

```sh
# 1. Detect drift (fresh DB) -> exit 1
python -m crmsync diff demos/01-basic/contacts.csv --db /tmp/crm.db --key email

# 2. Sync the DB to match the export
python -m crmsync apply demos/01-basic/contacts.csv --db /tmp/crm.db --key email

# 3. Re-diff -> "no drift", exit 0 (idempotent)
python -m crmsync diff demos/01-basic/contacts.csv --db /tmp/crm.db --key email

# JSON output for CI / jq
python -m crmsync diff demos/01-basic/contacts.csv --db /tmp/crm.db --key email --format json
```

## Expected result

- First `diff`: `add=4 update=0 delete=0 unchanged=0`, exit code **1**.
- After `apply`: DB has 4 contacts.
- Second `diff`: `add=0 update=0 delete=0 unchanged=4`, `no drift`, exit code **0**.
