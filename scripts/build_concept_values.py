#!/usr/bin/env python3
"""Build per-DB concept-value pickles from queryable.json.

For each DB that has schema_kg/inputs/<db>/queryable.json:
  1. Extract every field marked true (user-queryable).
  2. Resolve the parquet file for each schema table via:
       a. explicit override in schema_kg/inputs/<db>/parquet_map.json
       b. heuristic naming conventions (see _resolve_parquet)
  3. Load unique non-null string values for each field.
  4. Write resources/values/concept_values_<db>.pkl → {field: sorted_list}.

Each DB gets its own pickle so services only load what they need.

Usage:
  python scripts/build_concept_values.py            # all DBs with queryable.json
  python scripts/build_concept_values.py ttd hcdt   # specific DBs only
"""

import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

import polars as pl

ROOT        = Path(__file__).resolve().parent.parent
SCHEMA_ROOT = ROOT / "evaluation" / "schema_kg" / "inputs"
DB_ROOT     = ROOT / "database"
PKL_DIR     = ROOT / "resources" / "values"


def _resolve_parquet(
    table: str,
    db: str,
    db_dir: Path,
    parquet_map: dict,
) -> Optional[Path]:
    """Return the best-matching parquet Path for (table, db), or None.

    Resolution order (first existing path wins):
      1. explicit parquet_map.json override       → filename relative to db_dir
      2. <table>_v2.parquet
      3. <table>_<db>_v2.parquet
      4. strip trailing _<db>   → <base>_v2.parquet
      5. strip trailing _<db>   → <base>_<db>_v2.parquet
      6. strip trailing _<db> + _table → <stem>_v2.parquet   (CTD master tables)
      7. strip trailing _<db> + _table → <stem>_<db>_v2.parquet
      8. <table>.parquet
      9. <table>_<db>.parquet
    """
    if table in parquet_map:
        raw = parquet_map[table]
        # Strip "_loader_derived_:" sentinel — the raw parquet still exists on disk
        if raw.startswith("_loader_derived_:"):
            raw = raw[len("_loader_derived_:"):]
        p = db_dir / raw
        return p if p.exists() else None

    def first_existing(*names):
        for name in names:
            p = db_dir / name
            if p.exists():
                return p
        return None

    # base = table with trailing _<db> stripped (if present)
    base = table[: -len(f"_{db}")] if table.endswith(f"_{db}") else None
    # stem = base with trailing _table stripped (handles CTD *_master_table_ctd)
    stem = base[: -len("_table")] if (base and base.endswith("_table")) else None

    candidates = [
        f"{table}_v2.parquet",
        f"{table}_{db}_v2.parquet",
    ]
    if base:
        candidates += [
            f"{base}_v2.parquet",
            f"{base}_{db}_v2.parquet",
        ]
    if stem:
        candidates += [
            f"{stem}_v2.parquet",
            f"{stem}_{db}_v2.parquet",
        ]
    candidates += [
        f"{table}.parquet",
        f"{table}_{db}.parquet",
    ]

    return first_existing(*candidates)


def build_db_concept_values(db: str) -> dict:
    """Return {field_name: sorted_list_of_values} for *db* from its queryable.json.

    Any table whose parquet cannot be resolved is skipped with a warning so
    one bad table never blocks the whole DB.
    """
    qpath = SCHEMA_ROOT / db / "queryable.json"
    if not qpath.exists():
        raise FileNotFoundError(f"No queryable.json for db={db!r}: {qpath}")

    with open(qpath) as f:
        queryable = json.load(f)

    map_path = SCHEMA_ROOT / db / "parquet_map.json"
    parquet_map: dict = {}
    if map_path.exists():
        with open(map_path) as f:
            parquet_map = json.load(f)
        print(f"  Loaded parquet_map.json ({len(parquet_map)} overrides)")

    # Optional: logical-name → physical-column-name mapping.
    # When a loader renames parquet columns (e.g. pubchem_cid → crossmatch_pubchem_cid),
    # queryable.json uses the logical name but the raw parquet has the physical name.
    # parquet_col_map.json fixes this: {logical_field: physical_column}.
    col_map_path = SCHEMA_ROOT / db / "parquet_col_map.json"
    parquet_col_map: dict = {}
    if col_map_path.exists():
        with open(col_map_path) as f:
            parquet_col_map = json.load(f)
        print(f"  Loaded parquet_col_map.json ({len(parquet_col_map)} column renames)")

    # Structural-identifier fields are matched EXACTLY (never fuzzy/semantic), so they
    # must NOT enter the concept-value candidate pool — they only bloat it. ChEMBL ships
    # 2.85M SMILES + 2.85M InChI strings, which balloon the pickle to ~1.3 GB and choke
    # the resolver's first-load. Generic across DBs, keyed on field-name tokens.
    def _excluded_from_fuzzy(field: str) -> bool:
        f = field.lower()
        return any(tok in f for tok in ("inchi", "smiles", "molformula", "protein_sequence"))

    # group queryable=true fields by schema table name
    table_fields: dict[str, list[str]] = defaultdict(list)
    for key, val in queryable.items():
        if val is not True or key.startswith("_"):
            continue
        parts = key.split(".")
        if len(parts) != 3:
            continue
        _, table, field = parts
        table_fields[table].append(field)

    db_dir = DB_ROOT / db
    result: dict[str, set] = defaultdict(set)

    for table, fields in sorted(table_fields.items()):
        ppath = _resolve_parquet(table, db, db_dir, parquet_map)
        if ppath is None:
            print(f"  [WARN] {db}.{table}: no parquet found — skipping {fields}")
            continue

        try:
            df = pl.read_parquet(ppath)
        except Exception as e:
            print(f"  [WARN] {db}.{table}: failed to read {ppath.name}: {e}")
            continue

        for field in fields:
            if _excluded_from_fuzzy(field):
                print(f"  [SKIP] {db}.{table}.{field}: structural-identifier — excluded from fuzzy pool")
                continue
            # resolve physical column name (loader may rename on load)
            phys = parquet_col_map.get(field, field)
            if phys not in df.columns:
                print(f"  [WARN] {db}.{table}.{field}: column '{phys}' absent in {ppath.name}")
                continue
            if phys != field:
                print(f"  [COL-MAP] {db}.{table}.{field}: reading physical column '{phys}'")
            vals = set(df[phys].drop_nulls().cast(pl.Utf8).to_list())
            vals.discard("")
            result[field].update(vals)
            print(f"  {table}.{field}: {len(vals)} values  [{ppath.name}]")

    return {field: sorted(vals) for field, vals in result.items()}


def save_pickle(db: str, values: dict) -> Path:
    PKL_DIR.mkdir(parents=True, exist_ok=True)
    out = PKL_DIR / f"concept_values_{db}.pkl"
    with open(out, "wb") as f:
        pickle.dump(values, f)
    return out


def main(dbs: list | None = None) -> None:
    if dbs is None:
        dbs = sorted(
            p.name for p in SCHEMA_ROOT.iterdir()
            if p.is_dir() and (p / "queryable.json").exists()
        )
    print(f"Building concept-value pickles for: {dbs}\n")
    errors = []
    for db in dbs:
        print(f"{'='*60}\n{db}")
        try:
            values = build_db_concept_values(db)
            out = save_pickle(db, values)
            total = sum(len(v) for v in values.values())
            print(f"  → {len(values)} fields / {total} values  →  {out.name}\n")
        except Exception as e:
            print(f"  [ERROR] {db}: {e}\n")
            errors.append((db, e))
    if errors:
        print("FAILED:", [(db, str(e)) for db, e in errors])
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    main(args if args else None)
