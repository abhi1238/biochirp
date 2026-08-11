#!/usr/bin/env python3
"""Per-DB SCHEMA generator — one `dbs/<db>/schema.yaml` SSOT -> all derived artifacts.

See dbs/_schema/db_schema.py for the SSOT model and the full list of artifacts.

Modes:
    --reverse --db <db>   Scaffold a candidate dbs/<db>/schema.yaml by LIFTING the
                          current hand-authored artifacts (config.schema block,
                          schema_kg/inputs/<db>/*.json, the loader's renames/keep_native)
                          and prefilling real dtypes from the parquet. Author then
                          reviews + factors questions into params/templates.

    (--db / --all / --check / --diff: the FORWARD generator — added next stage.)

The reverse output is SCAFFOLDING for human review, not a committed artifact.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KG_INPUTS = ROOT / "evaluation" / "schema_kg" / "inputs"
DBS_DIR = ROOT / "dbs"
DB_DATA = ROOT / "database"


def migrated_dbs() -> list[str]:
    """DBs that have a hand-authored SSOT (dbs/<db>/schema.yaml)."""
    return sorted(p.parent.name for p in DBS_DIR.glob("*/schema.yaml"))


# ── flow-style dict so emitted columns stay on one line (compact, reviewable) ──
import yaml  # noqa: E402


class _Flow(dict):
    pass


def _flow_repr(dumper: "yaml.Dumper", data: _Flow):
    return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)


yaml.add_representer(_Flow, _flow_repr)


# ── polars dtype -> friendly SSOT alias ───────────────────────────────────────
def _friendly_dtype(dt: str) -> str:
    s = str(dt)
    table = {"String": "str", "Utf8": "str", "Int64": "int", "Int32": "int",
             "Float64": "float", "Float32": "float", "Boolean": "bool"}
    if s in table:
        return table[s]
    if s.startswith("List"):
        return "list[str]"
    return s.lower()


def _parquet_schema(db: str, parquet_dir: str, fname: str, renames: dict[str, str]) -> dict[str, str]:
    """{logical_col: friendly_dtype} for one parquet, AFTER applying loader renames."""
    try:
        import polars as pl
    except Exception:
        return {}
    path = DB_DATA / parquet_dir / fname
    if not path.exists():
        return {}
    try:
        sch = pl.scan_parquet(str(path)).collect_schema()
    except Exception as exc:
        print(f"  ! could not scan {path.name}: {exc}", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for name, dt in zip(sch.names(), sch.dtypes()):
        out[renames.get(name, name)] = _friendly_dtype(dt)
    return out


# ── extract loader renames + keep_native via AST (no import side-effects) ──────
def _loader_facts(db: str) -> tuple[list[str], dict[str, str]]:
    loader = ROOT / "app" / "tools" / db / "app" / "database_loader.py"
    keep_native: list[str] = []
    renames: dict[str, str] = {}
    if not loader.exists():
        return keep_native, renames
    tree = ast.parse(loader.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):       # `_X_KEEP_NATIVE: tuple = (...)`
            target = node.target
        else:
            continue
        name = getattr(target, "id", "")
        val = node.value
        if name.endswith("_KEEP_NATIVE") and isinstance(val, (ast.Tuple, ast.List)):
            keep_native = [e.value for e in val.elts if isinstance(e, ast.Constant)]
        if name == "mapping" and isinstance(val, ast.Dict):
            for k, v in zip(val.keys, val.values):
                if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    renames[k.value] = v.value
    return keep_native, renames


def _manifest_key_columns(db: str) -> set[tuple[str, str]]:
    """{(table, column)} flagged as key_columns in the manifest."""
    path = DBS_DIR / db / "manifest.yaml"
    if not path.exists():
        return set()
    doc = yaml.safe_load(path.read_text()) or {}
    tables = ((doc.get("schema") or {}).get("tables") or {})
    out: set[tuple[str, str]] = set()
    for tname, tspec in tables.items():
        for c in (tspec.get("key_columns") or []):
            if isinstance(c, dict) and c.get("name"):
                out.add((tname, c["name"]))
    return out


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def reverse(db: str) -> dict:
    """Build a candidate schema.yaml dict for <db> from the current artifacts."""
    from config.schema import database_schemas, database_decoration_tables

    inp = KG_INPUTS / db
    schema_json = _read_json(inp / "schema.json").get(db, {})
    queryable = _read_json(inp / "queryable.json")
    concept_type = _read_json(inp / "concept_type.json")
    parquet_map = {k: v for k, v in _read_json(inp / "parquet_map.json").items()
                   if not k.startswith("_")}
    rules = _read_json(inp / "schema_rules.json")
    questions = _read_json(inp / "questions.json")

    exec_schema = database_schemas.get(db, {})
    decoration = set(database_decoration_tables.get(db, set()))
    key_cols = _manifest_key_columns(db)
    keep_native, renames = _loader_facts(db)
    parquet_dir = db

    enum_columns = rules.get("enum_columns", {})
    notes = rules.get("column_notes_override", {})

    def suf(t: str) -> str:
        return t if t.endswith(f"_{db}") else f"{t}_{db}"

    # Canonical SSOT table keys = config/logical names (bare or as config has them),
    # PLUS schema_kg-only tables (present in schema.json but with no config twin).
    # schema_kg artifacts are keyed by suf(name); config by the logical name. This
    # dedups the bare↔suffixed divergence onto ONE SSOT entry per table (2B).
    # The CURRENT on-disk schema_kg artifacts may name a table bare OR suffixed
    # (mixed per DB). Look up each table's data under BOTH forms, then re-emit it
    # canonically under suf(t). covered tracks both forms so a bare schema.json key
    # for a config table isn't mistaken for a kg-only table.
    ssot_keys: list[str] = list(exec_schema.keys())
    covered = {suf(t) for t in ssot_keys} | set(ssot_keys)
    for s in list(schema_json) + list(parquet_map):
        if s not in covered:
            ssot_keys.append(s)              # genuine kg-only table
            covered.add(s); covered.add(suf(s))

    tables_out: dict[str, Any] = {}
    for t in ssot_keys:
        kg = suf(t)
        parquet = parquet_map.get(kg) or parquet_map.get(t) or f"{kg}_v2.parquet"
        pq = _parquet_schema(db, parquet_dir, parquet, renames)
        sj_cols = schema_json.get(kg) or schema_json.get(t) or {}   # bare OR suffixed
        ex_order = list(exec_schema.get(t, []))    # config/schema.py order (preserve it)
        ex_cols = set(ex_order)
        # exec order first (config byte-parity), then kg-only cols, then parquet-only.
        col_names = list(ex_order)
        for c in list(sj_cols) + list(pq):
            if c not in col_names:
                col_names.append(c)

        cols_out: list[_Flow] = []
        for cname in col_names:
            is_q = bool(queryable.get(f"{db}.{kg}.{cname}",
                                     queryable.get(f"{db}.{t}.{cname}", False)))
            ct = (concept_type.get(f"{db}.{kg}.{cname}")
                  or concept_type.get(f"{db}.{t}.{cname}") or "")
            desc = (sj_cols.get(cname) or "").strip()
            entry: dict[str, Any] = {"name": cname}
            dtype = pq.get(cname, "str")
            if dtype != "str":
                entry["dtype"] = dtype
            if cname not in ex_cols:
                entry["exec_schema"] = False
            if cname not in sj_cols:
                entry["kg_schema"] = False
            if is_q:
                entry["queryable"] = True
            if (t, cname) in key_cols or (kg, cname) in key_cols:
                entry["key_column"] = True
            if ct:
                entry["concept_type"] = ct
            if cname in enum_columns:
                entry["enum"] = enum_columns[cname]
            if cname in notes:
                entry["llm_note"] = notes[cname]
            # DRY: drop a description an FK would auto-generate anyway
            master = cname[:-3] + "_master_table" if cname.endswith("_id") else None
            auto_fk = (master in ssot_keys and t != master)
            if desc and not (auto_fk and desc == f"FK to {master}.{cname}"):
                entry["description"] = desc
            cols_out.append(_Flow(entry))

        role = "master" if t.endswith("_master_table") else ("decoration" if t in decoration else "association")
        tspec: dict[str, Any] = {"parquet": parquet, "role": role, "columns": cols_out}
        tables_out[t] = tspec

    # rules passthrough = schema_rules minus the keys we now derive elsewhere
    derived_keys = {"_comment", "db_name", "db_display_name", "db_description",
                    "enum_columns", "column_notes_override"}
    rules_passthrough = {k: v for k, v in rules.items() if k not in derived_keys}

    doc: dict[str, Any] = {
        "db": db,
        "display_name": rules.get("db_display_name") or db.upper(),
        "description": (rules.get("db_description") or "TODO: ≥50-char description").strip(),
        "parquet_dir": parquet_dir,
    }
    if decoration:
        doc["decoration_tables"] = sorted(decoration)
    doc["tables"] = tables_out
    loader_block: dict[str, Any] = {}
    if keep_native:
        loader_block["keep_native"] = keep_native
    if renames:
        loader_block["renames"] = _Flow(renames)
    if loader_block:
        doc["loader"] = loader_block
    if rules_passthrough:
        doc["rules"] = rules_passthrough
    # questions: lift verbatim into `literal`; author factors into params/templates
    if isinstance(questions, list) and questions:
        doc["questions"] = {"literal": questions}
    return doc


# ── FORWARD generation: SSOT -> derived artifacts ─────────────────────────────

def _json_text(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def build_artifacts(db: str) -> dict[str, Any]:
    """Build the logical (pre-serialization) artifacts from dbs/<db>/schema.yaml.

    Returns a dict keyed by artifact name with Python objects, so callers can do
    order-independent semantic comparison (--diff) or serialize (--db/--check).
    """
    from dbs._schema import db_schema
    obj = db_schema.load(DBS_DIR / db / "schema.yaml")

    config_block = {t: obj.exec_columns(t) for t in obj.tables
                    if any(c.exec_schema for c in obj.tables[t].columns)}

    # JSON can't carry comments, so a leading "_comment" doubles as the DO-NOT-EDIT
    # banner. Consumers already skip keys starting with "_".
    gen_note = f"GENERATED from dbs/{db}/schema.yaml by scripts/gen_schema.py — DO NOT EDIT."

    # schema_kg artifacts key tables by the canonical `_<db>`-suffixed name (2B);
    # tables with no kg columns are OMITTED entirely (no phantom empty entries).
    schema_json = {db: {
        obj.kg_name(t): {c.kg(): c.effective_description() for c in tbl.columns if c.kg_schema}
        for t, tbl in obj.tables.items()
        if any(c.kg_schema for c in tbl.columns)
    }}
    queryable = {"_comment": f"True = user can name this value in a query; False = join "
                             f"key / internal ID / raw FK column. {gen_note}"}
    queryable.update({f"{db}.{obj.kg_name(t)}.{c.kg()}": c.queryable
                      for t, tbl in obj.tables.items() for c in tbl.columns if c.kg_schema})
    concept = {"_comment": f"Maps each queryable column to a concept type from the closed "
                           f"vocabulary (only queryable columns appear). {gen_note}"}
    concept.update({f"{db}.{obj.kg_name(t)}.{c.kg()}": c.concept_type
                    for t, tbl in obj.tables.items() for c in tbl.columns
                    if c.kg_schema and c.queryable and c.concept_type})
    parquet_map = {"_comment": f"Logical schema table -> physical parquet filename. {gen_note}"}
    parquet_map.update({obj.kg_name(t): tbl.parquet for t, tbl in obj.tables.items()
                        if any(c.kg_schema for c in tbl.columns)})

    # schema_rules.json: derived (db identity + enums + notes) merged with passthrough
    enum_columns: dict[str, list] = {}
    notes: dict[str, str] = {}
    for tbl in obj.tables.values():
        for c in tbl.columns:
            if c.enum:
                enum_columns.setdefault(c.name, c.enum)
            if c.llm_note:
                notes.setdefault(c.name, c.llm_note)
    schema_rules: dict[str, Any] = {}
    pt = dict(obj.rules)
    base_comment = pt.pop("_comment", f"Schema rules for {db}. Injected into expander/filter/"
                                      f"mapper prompts at runtime.")
    # Seed with hand-maintained keys from the existing schema_rules.json so they survive
    # regeneration.  Keys produced below (derived + schema.yaml rules) take priority.
    _derived = {"_comment", "db_name", "db_display_name", "db_description",
                "enum_columns", "column_notes_override"}
    _existing = _read_json(KG_INPUTS / db / "schema_rules.json")
    schema_rules.update({k: v for k, v in _existing.items() if k not in _derived})
    schema_rules["_comment"] = f"{base_comment} {gen_note}"
    schema_rules["db_name"] = db
    schema_rules["db_display_name"] = obj.display_name
    schema_rules["db_description"] = obj.description
    for k, v in pt.items():
        schema_rules[k] = v
    if enum_columns:
        schema_rules["enum_columns"] = enum_columns
    if notes:
        schema_rules["column_notes_override"] = notes

    questions = obj.expand_questions()

    return {
        "config_block": config_block,
        "decoration": sorted(obj.decoration_tables),
        "schema.json": schema_json,
        "queryable.json": queryable,
        "concept_type.json": concept,
        "parquet_map.json": parquet_map,
        "schema_rules.json": schema_rules,
        "questions.json": questions,
    }


# JSON artifacts: artifact-name -> on-disk path
def _kg_paths(db: str) -> dict[str, Path]:
    inp = KG_INPUTS / db
    return {n: inp / n for n in (
        "schema.json", "queryable.json", "concept_type.json",
        "parquet_map.json", "schema_rules.json", "questions.json")}


def _strip_comments(o: Any) -> Any:
    """Drop _comment keys so semantic compare ignores prose-only header keys."""
    if isinstance(o, dict):
        return {k: _strip_comments(v) for k, v in o.items() if not k.startswith("_comment")}
    if isinstance(o, list):
        return [_strip_comments(x) for x in o]
    return o


def diff(db: str) -> int:
    """Semantic parity: compare generated artifacts to current on-disk files.

    Order-independent (parsed JSON / dict equality). This is the migration gate —
    an empty diff proves the SSOT reproduces the committed artifacts before any
    reformat. config/schema.py is compared as a parsed dict.
    """
    arts = build_artifacts(db)
    mismatches = 0

    from config.schema import database_schemas, database_decoration_tables
    if database_schemas.get(db, {}) != arts["config_block"]:
        mismatches += 1
        cur, gen = database_schemas.get(db, {}), arts["config_block"]
        for t in sorted(set(cur) | set(gen)):
            if cur.get(t) != gen.get(t):
                print(f"[config/schema.py] {t}:\n   current   {cur.get(t)}\n   generated {gen.get(t)}")
    if set(database_decoration_tables.get(db, set())) != set(arts["decoration"]):
        mismatches += 1
        print(f"[decoration] current {set(database_decoration_tables.get(db, set()))} "
              f"!= generated {set(arts['decoration'])}")

    for name, path in _kg_paths(db).items():
        gen = arts[name]
        if not path.exists():
            print(f"[{name}] MISSING on disk (would be created)")
            mismatches += 1
            continue
        cur = json.loads(path.read_text())
        if _strip_comments(cur) != _strip_comments(gen):
            mismatches += 1
            print(f"[{name}] semantic mismatch")
            if isinstance(cur, dict) and isinstance(gen, dict):
                ck, gk = set(_strip_comments(cur)), set(_strip_comments(gen))
                if ck - gk:
                    print(f"   only on disk:   {sorted(ck - gk)[:8]}")
                if gk - ck:
                    print(f"   only generated: {sorted(gk - ck)[:8]}")
    print(f"\n{db}: PARITY OK ✅" if mismatches == 0 else f"\n{db}: {mismatches} artifact(s) differ ☝️")
    return 1 if mismatches else 0


CONFIG_PY = ROOT / "config" / "schema.py"


def generate_config_artifacts(db: str) -> tuple[str, str]:
    """Return (schemas_block_text, decoration_text) — sentinel-wrapped Python for
    config/schema.py. Rationale for exec_schema:false columns is emitted as
    comments (carried from the SSOT's per-column `exec_note`)."""
    from dbs._schema import db_schema
    obj = db_schema.load(DBS_DIR / db / "schema.yaml")
    src = "dbs/" + db + "/schema.yaml"

    lines = [f'    # >>> GENERATED {db} BEGIN — DO NOT EDIT '
             f'(source: {src}; run: python scripts/gen_schema.py --write --db {db}) <<<',
             f'    "{db}": {{']
    for tname, tbl in obj.tables.items():
        cols = [c.name for c in tbl.columns if c.exec_schema]
        if not cols:
            continue
        if tbl.description:
            lines.append(f"        # {tbl.description}")
        rendered = "[" + ", ".join(f"'{c}'" for c in cols) + "]"
        lines.append(f'        "{tname}": {rendered},')
        for c in tbl.columns:
            if not c.exec_schema and c.exec_note:
                lines.append(f"        # {c.name} omitted (exec_schema:false): {c.exec_note}")
    lines.append("    },")
    lines.append(f"    # >>> GENERATED {db} END <<<")
    schemas_block = "\n".join(lines)

    decos = sorted(obj.decoration_tables)
    if decos:
        rendered = "{" + ", ".join(f'"{d}"' for d in decos) + "}"
        decoration = "\n".join([
            f"    # >>> GENERATED {db} DECOR BEGIN — DO NOT EDIT (source: {src}) <<<",
            f'    "{db}": {rendered},',
            f"    # >>> GENERATED {db} DECOR END <<<",
        ])
    else:
        decoration = ""
    return schemas_block, decoration


def _splice(text: str, begin_prefix: str, end_prefix: str, replacement: str) -> Optional[str]:
    """Replace the inclusive span between a BEGIN and END sentinel line. None if absent."""
    lines = text.splitlines(keepends=True)
    bi = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(begin_prefix.lstrip())), None)
    ei = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(end_prefix.lstrip())), None)
    if bi is None or ei is None or ei < bi:
        return None
    return "".join(lines[:bi]) + replacement + "\n" + "".join(lines[ei + 1:])


def write_config(db: str) -> bool:
    """Splice the generated config/schema.py blocks between sentinels.

    Returns False (and prints the block) when sentinels are not yet present — the
    one-time wrap is a manual, reviewed edit for the first flip of each DB.
    """
    schemas_block, decoration = generate_config_artifacts(db)
    text = CONFIG_PY.read_text()
    new = _splice(text, f"# >>> GENERATED {db} BEGIN", f"# >>> GENERATED {db} END", schemas_block)
    if new is None:
        print(f"  config/schema.py: no sentinels for {db!r} yet — wrap the block once, e.g.:\n")
        print(schemas_block)
        if decoration:
            print("\n  and in database_decoration_tables:\n")
            print(decoration)
        return False
    if decoration:
        new2 = _splice(new, f"# >>> GENERATED {db} DECOR BEGIN", f"# >>> GENERATED {db} DECOR END", decoration)
        if new2 is not None:
            new = new2
    CONFIG_PY.write_text(new)
    print(f"spliced config/schema.py ({db})")
    return True


LOADER_PY = lambda db: ROOT / "app" / "tools" / db / "app" / "database_loader.py"


def generate_loader_artifacts(db: str) -> tuple[str, str]:
    """Return (module_block, renames_block) — sentinel-wrapped Python for the loader.

    module_block : _<DB>_KEEP_NATIVE + _<DB>_TABLES (logical_key_<db>, parquet) tuple.
    renames_block: the `mapping = {raw: logical}` dict (indented, inside the fn).
    """
    from dbs._schema import db_schema
    obj = db_schema.load(DBS_DIR / db / "schema.yaml")
    U = db.upper()

    kn = "(" + ", ".join(f'"{c}"' for c in obj.loader_keep_native) + ("," if len(obj.loader_keep_native) == 1 else "") + ")"
    rows = "\n".join(f'    ("{t}_{db}", "{tbl.parquet}"),' for t, tbl in obj.tables.items())
    module_block = "\n".join([
        f"# >>> GENERATED {db} LOADER BEGIN — DO NOT EDIT (source: dbs/{db}/schema.yaml) <<<",
        f"_{U}_KEEP_NATIVE: tuple[str, ...] = {kn}",
        "",
        f"_{U}_TABLES: tuple[tuple[str, str], ...] = (",
        rows,
        ")",
        f"# >>> GENERATED {db} LOADER END <<<",
    ])

    if obj.loader_renames:
        body = "\n".join(f'        "{raw}": "{logical}",' for raw, logical in obj.loader_renames.items())
        renames_block = "\n".join([
            f"    # >>> GENERATED {db} RENAMES BEGIN — DO NOT EDIT (source: dbs/{db}/schema.yaml) <<<",
            "    mapping = {",
            body,
            "    }",
            f"    # >>> GENERATED {db} RENAMES END <<<",
        ])
    else:
        renames_block = ""
    return module_block, renames_block


def write_loader(db: str) -> bool:
    """Splice the generated loader blocks between sentinels (one-time wrap is manual)."""
    from dbs._schema import db_schema
    if not db_schema.load(DBS_DIR / db / "schema.yaml").loader_generated:
        print(f"  loader: {db!r} has loader.generated=false (bespoke) — skipped")
        return False
    module_block, renames_block = generate_loader_artifacts(db)
    path = LOADER_PY(db)
    if not path.exists():
        print(f"  loader {path} not found — skipped")
        return False
    text = path.read_text()
    new = _splice(text, f"# >>> GENERATED {db} LOADER BEGIN", f"# >>> GENERATED {db} LOADER END", module_block)
    if new is None:
        print(f"  loader: no sentinels for {db!r} yet — wrap _{db.upper()}_TABLES/KEEP_NATIVE once:\n")
        print(module_block)
        if renames_block:
            print("\n  and wrap the `mapping = {{...}}` dict:\n")
            print(renames_block)
        return False
    if renames_block:
        n2 = _splice(new, f"# >>> GENERATED {db} RENAMES BEGIN", f"# >>> GENERATED {db} RENAMES END", renames_block)
        if n2 is not None:
            new = n2
    path.write_text(new)
    print(f"spliced {path}")
    return True


def write_kg(db: str) -> None:
    """Write the 6 schema_kg JSON artifacts from the SSOT (the flip)."""
    arts = build_artifacts(db)
    for name, path in _kg_paths(db).items():
        path.write_text(_json_text(arts[name]))
        print(f"wrote {path}")


def _between(text: str, begin_prefix: str, end_prefix: str) -> Optional[str]:
    """Return the inclusive text between BEGIN/END sentinel lines, or None."""
    lines = text.splitlines(keepends=True)
    bi = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(begin_prefix.lstrip())), None)
    ei = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith(end_prefix.lstrip())), None)
    if bi is None or ei is None or ei < bi:
        return None
    return "".join(lines[bi:ei + 1]).rstrip("\n")


def check(db: str) -> int:
    """Byte-compare every generated surface (6 KG JSONs + config block + loader block)
    to freshly-generated output. The full CI drift gate — any hand-edit to a
    generated surface fails here. Needs only pyyaml (no config.schema import)."""
    from dbs._schema import db_schema
    obj = db_schema.load(DBS_DIR / db / "schema.yaml")
    arts = build_artifacts(db)
    stale = 0
    for name, path in _kg_paths(db).items():
        want = _json_text(arts[name])
        have = path.read_text() if path.exists() else ""
        if want != have:
            stale += 1
            print(f"[stale] {path} — regenerate with: python scripts/gen_schema.py --write --db {db}")

    schemas_block, decoration = generate_config_artifacts(db)
    cfg = CONFIG_PY.read_text()
    for label, gen, bp, ep in (
        ("config/schema.py block", schemas_block, f"# >>> GENERATED {db} BEGIN", f"# >>> GENERATED {db} END"),
        ("config/schema.py decoration", decoration, f"# >>> GENERATED {db} DECOR BEGIN", f"# >>> GENERATED {db} DECOR END"),
    ):
        if not gen:
            continue
        cur = _between(cfg, bp, ep)
        if cur is None or cur.strip() != gen.strip():
            stale += 1
            print(f"[stale] {label} — regenerate with: python scripts/gen_schema.py --write --db {db}")

    module_block, renames_block = generate_loader_artifacts(db)
    lp = LOADER_PY(db)
    if obj.loader_generated and lp.exists():
        ltext = lp.read_text()
        for label, gen, bp, ep in (
            ("loader tables", module_block, f"# >>> GENERATED {db} LOADER BEGIN", f"# >>> GENERATED {db} LOADER END"),
            ("loader renames", renames_block, f"# >>> GENERATED {db} RENAMES BEGIN", f"# >>> GENERATED {db} RENAMES END"),
        ):
            if not gen:
                continue
            cur = _between(ltext, bp, ep)
            if cur is None or cur.strip() != gen.strip():
                stale += 1
                print(f"[stale] {label} — regenerate with: python scripts/gen_schema.py --write --db {db}")

    # Manifest key_columns SELECTION must match schema.yaml key_column flags.
    # (The manifest's data-profiled descriptions stay hand-authored per the
    # disk-first-prose policy — only the *set of featured columns* is SSOT-governed.)
    man_kc_raw = _manifest_key_columns(db)
    if man_kc_raw:
        # The manifest documents the LOGICAL schema, but its table names are mixed
        # bare/suffixed per DB. Map each manifest table name back to the SSOT logical
        # name (the manifest uses either <logical> or <logical>_<db>).
        ssot_tables = set(obj.tables)
        def _logical(mt):
            if mt in ssot_tables:
                return mt
            stripped = mt[: -len(db) - 1] if mt.endswith(f"_{db}") else mt
            return stripped if stripped in ssot_tables else mt
        man_kc = {(_logical(t), c) for (t, c) in man_kc_raw}
        ssot_kc = {(t, c.name) for t, tbl in obj.tables.items()
                   for c in tbl.columns if c.key_column}
        only_ssot, only_man = ssot_kc - man_kc, man_kc - ssot_kc
        if only_ssot or only_man:
            stale += 1
            print(f"[stale] manifest key_columns selection ≠ schema.yaml key_column flags for {db}:")
            if only_ssot:
                print(f"   key_column:true in schema.yaml but missing from manifest: {sorted(only_ssot)}")
            if only_man:
                print(f"   in manifest key_columns but not key_column:true in schema.yaml: {sorted(only_man)}")

    print(f"{db}: up to date ✅" if stale == 0 else f"{db}: {stale} stale surface(s) ☝️")
    return 1 if stale else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reverse", action="store_true", help="scaffold schema.yaml from current artifacts")
    ap.add_argument("--diff", action="store_true", help="semantic parity vs current artifacts (migration gate)")
    ap.add_argument("--check", action="store_true", help="byte-compare on-disk artifacts to generated (CI)")
    ap.add_argument("--write", action="store_true", help="write generated schema_kg JSONs (the flip)")
    ap.add_argument("--db", help="database slug")
    ap.add_argument("--all", action="store_true", help="apply to every migrated DB (with --check/--diff)")
    ap.add_argument("--out", help="output path (default dbs/<db>/schema.yaml)")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    if args.all and (args.check or args.diff):
        fn = check if args.check else diff
        rc = 0
        for db in migrated_dbs():
            rc |= fn(db)
        return rc

    if args.reverse:
        if not args.db:
            ap.error("--reverse requires --db")
        doc = reverse(args.db)
        text = yaml.dump(doc, sort_keys=False, width=100, allow_unicode=True, default_flow_style=False)
        banner = (f"# CANDIDATE schema.yaml for {args.db} — scaffolded by "
                  f"`gen_schema.py --reverse`.\n# REVIEW before use: verify dtypes, "
                  f"factor `questions.literal` into params/templates,\n# and confirm "
                  f"exec_schema/kg_schema flags on denormalised columns.\n")
        text = banner + text
        # sanity: round-trip through the model
        from dbs._schema import db_schema
        obj = db_schema.from_dict(yaml.safe_load(text))
        for e in db_schema.validate(obj):
            print(f"  [validate] {e}", file=sys.stderr)
        if args.stdout:
            print(text)
        else:
            out = Path(args.out) if args.out else (DBS_DIR / args.db / "schema.yaml")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
            print(f"wrote {out} ({len(doc['tables'])} tables)")
        return 0

    if args.diff:
        if not args.db:
            ap.error("--diff requires --db")
        return diff(args.db)
    if args.check:
        if not args.db:
            ap.error("--check requires --db")
        return check(args.db)
    if args.write:
        if not args.db:
            ap.error("--write requires --db")
        write_kg(args.db)
        write_config(args.db)
        write_loader(args.db)
        return 0

    ap.error("nothing to do; pass --reverse / --diff / --write / --check with --db")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
