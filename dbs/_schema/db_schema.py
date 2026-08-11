"""BioChirp per-DB SCHEMA single-source-of-truth (SSOT).

One `dbs/<db>/schema.yaml` per database is the ONLY hand-authored description of a
DB's physical schema. Everything else is generated from it by
`scripts/gen_schema.py`:

    config/schema.py  database_schemas[db]        (the Polars execution/join schema)
    schema_kg/inputs/<db>/schema.json             (mapper/graph column descriptions)
    schema_kg/inputs/<db>/queryable.json          (user-nameable flags)
    schema_kg/inputs/<db>/concept_type.json       (cross-DB concept links)
    schema_kg/inputs/<db>/parquet_map.json        (logical table -> parquet file)
    schema_kg/inputs/<db>/schema_rules.json       (enums + notes merged w/ `rules:`)
    schema_kg/inputs/<db>/questions.json          (expanded from `questions:`)
    dbs/<db>/manifest.yaml  key_columns block     (columns flagged key_column: true)
    app/tools/<db>/app/database_loader.py         (table tuple + keep_native + renames)

This module is the validated in-memory model (parse + validate + round-trip),
mirroring dbs/_schema/manifest_schema.py.

── Column routing (the subtlety this model exists to make explicit) ─────────────
Every PHYSICAL parquet column is declared (so the exact validator has the full
truth). Two independent booleans then route each column to the artifacts it
belongs in, because the execution schema and the mapper surface genuinely
diverge per-column (e.g. a denormalised name kept for matching but suppressed
from the join schema to avoid a Polars `_right` collision):

    exec_schema (default True)  -> column appears in config/schema.py
    kg_schema   (default True)  -> column appears in schema.json/queryable/concept_type
    queryable   (default False) -> user can name it (requires kg_schema)
    key_column  (default False) -> featured in manifest.yaml key_columns docs

A denormalised, mapper-visible-but-not-joined column is `exec_schema: false,
kg_schema: true`. A purely physical column ignored everywhere is
`exec_schema: false, kg_schema: false` (still dtype-validated).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ── dtype canonicalisation ───────────────────────────────────────────────────
# The SSOT declares friendly aliases; we compare against polars' str(dtype).
# polars renamed Utf8 -> String; treat them as equal.
_DTYPE_CANON = {
    "str": "str", "string": "str", "utf8": "str", "categorical": "str",
    "int": "int", "int64": "int", "int32": "int", "i64": "int", "i32": "int",
    "float": "float", "float64": "float", "float32": "float", "f64": "float", "f32": "float",
    "bool": "bool", "boolean": "bool",
    "list[str]": "list[str]", "list(str)": "list[str]",
    "list[string]": "list[str]", "list(string)": "list[str]",
}


def canon_dtype(dt: str) -> str:
    """Normalise a dtype string (SSOT alias OR polars repr) to a canonical token."""
    s = str(dt).strip().lower()
    if s in _DTYPE_CANON:
        return _DTYPE_CANON[s]
    # polars List(String) / List(Int64) etc.
    m = re.fullmatch(r"list\[?\(?(\w+)\)?\]?", s)
    if m:
        inner = _DTYPE_CANON.get(m.group(1), m.group(1))
        return f"list[{inner}]"
    return s


# ── concept-type closed vocabulary ───────────────────────────────────────────
# Seeded from current usage across schema_kg/inputs/*/concept_type.json. Unknown
# values are a WARNING (typos silently break cross-DB bridge edges), not a hard
# error, so a new legitimate concept doesn't block onboarding before this set is
# extended.
CONCEPT_TYPES: frozenset[str] = frozenset({
    "drug_name", "gene_symbol", "disease_name", "pathway_name", "protein_name",
    "protein_id", "approval_status", "interaction_type", "xref_id",
    "compound_xref_id", "mol_formula", "bioactivity_value", "enum_value",
    "phenotype_name", "geneset_name", "organism", "anatomy", "cell_type",
    "variant_id", "go_term", "tissue",
})


# ── Column / Table / Db dataclasses ──────────────────────────────────────────


@dataclass
class Column:
    name: str                      # the LOGICAL name (used in config/schema.py)
    dtype: str = "str"
    role: str = ""                 # pk | fk | attr ; "" => infer
    fk_target: str = ""            # "<table>.<col>" ; "" => infer for fk
    exec_schema: bool = True       # -> config/schema.py
    kg_schema: bool = True         # -> schema.json / queryable / concept_type
    queryable: bool = False        # user can name it (requires kg_schema)
    key_column: bool = False       # -> manifest key_columns docs
    concept_type: str = ""         # cross-DB concept (requires queryable)
    enum: list[str] = field(default_factory=list)
    description: str = ""
    llm_note: str = ""             # -> schema_rules.column_notes_override
    exec_note: str = ""            # WHY exec_schema:false — emitted as a config/schema.py comment
    # ── per-column name divergence (for DBs whose loader renames per-table) ──
    kg_name: str = ""              # name in schema_kg artifacts if ≠ name (default: name)
    parquet_name: str = ""         # RAW column name in the parquet if ≠ name (for the validator)
    added_at_load: bool = False    # injected by the loader (not in any parquet) → validator skips

    def kg(self) -> str:
        return self.kg_name or self.name

    def pq(self) -> str:
        return self.parquet_name or self.name

    def effective_description(self) -> str:
        """FK descriptions auto-generate when not hand-written (DRY)."""
        if self.description:
            return self.description
        if self.role == "fk" and self.fk_target:
            return f"FK to {self.fk_target}"
        return ""


@dataclass
class Table:
    name: str
    parquet: str
    role: str = "association"      # master | association | decoration
    description: str = ""
    columns: list[Column] = field(default_factory=list)
    kg_name: str = ""              # schema_kg table name if NOT just <name>_<db> (e.g. uniprot xwalk)

    def col(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def is_master(self) -> bool:
        return self.role == "master" or self.name.endswith("_master_table")


@dataclass
class QuestionTemplate:
    template: str
    required: list[str] = field(default_factory=list)
    label: str = ""
    grid: dict[str, str] = field(default_factory=dict)   # {param_name: param_list_name}


@dataclass
class DbSchema:
    db: str
    display_name: str
    description: str
    parquet_dir: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, Table] = field(default_factory=dict)
    decoration_tables: list[str] = field(default_factory=list)
    loader_keep_native: list[str] = field(default_factory=list)
    loader_renames: dict[str, str] = field(default_factory=dict)
    loader_generated: bool = True      # False ⇒ bespoke hand-written loader (msigdb, hcdt)
    rules: dict[str, Any] = field(default_factory=dict)            # schema_rules passthrough
    q_params: dict[str, list[str]] = field(default_factory=dict)
    q_templates: list[QuestionTemplate] = field(default_factory=list)
    q_literal: list[dict] = field(default_factory=list)

    # ── derived artifact projections ────────────────────────────────────────
    def exec_columns(self, table: str) -> list[str]:
        """config/schema.py column list for one table."""
        return [c.name for c in self.tables[table].columns if c.exec_schema]

    def kg_name(self, table: str) -> str:
        """schema_kg table name = canonical `_<db>` suffix (2B canonicalization).

        config/schema.py keeps the bare/logical name (the executor doesn't need
        cross-DB namespacing); every schema_kg artifact (schema.json, queryable,
        concept_type, parquet_map) uses the suffixed name. Already-suffixed names
        (e.g. `exposure_studies_ctd`) are left as-is. An explicit Table.kg_name
        wins (for non-suffix renames like uniprot gene_protein_association ->
        gene_protein_xwalk_uniprot)."""
        explicit = self.tables[table].kg_name if table in self.tables else ""
        if explicit:
            return explicit
        return table if table.endswith(f"_{self.db}") else f"{table}_{self.db}"

    def expand_questions(self) -> list[dict]:
        """Flatten templates x params + literal into the questions.json array.

        Deterministic: param iteration follows insertion order; grid is the
        cartesian product in template-declared key order.
        """
        out: list[dict] = []
        for tpl in self.q_templates:
            keys = list(tpl.grid.keys())
            value_lists = [self.q_params.get(tpl.grid[k], []) for k in keys]
            for combo in _cartesian(value_lists):
                subst = dict(zip(keys, combo))
                out.append({
                    "label": tpl.label,
                    "question": tpl.template.format(**subst),
                    "required": list(tpl.required),
                })
        out.extend(self.q_literal)
        return out


def _cartesian(lists: list[list[str]]) -> list[tuple]:
    if not lists:
        return [()]
    acc: list[tuple] = [()]
    for lst in lists:
        acc = [prev + (item,) for prev in acc for item in lst]
    return acc


# ── parse + validate ─────────────────────────────────────────────────────────


def _slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]+", s or ""))


def _master_for_id(col_name: str) -> Optional[str]:
    """drug_id -> drug_master_table ; returns None for non-<x>_id names."""
    if not col_name.endswith("_id"):
        return None
    return col_name[:-3] + "_master_table"


def from_dict(data: dict) -> DbSchema:
    if not isinstance(data, dict):
        raise ValueError("schema.yaml must be a YAML mapping")
    db = data.get("db")
    if not db or not _slug_ok(db):
        raise ValueError(f"db {db!r} must match [a-z][a-z0-9_]+")
    display_name = data.get("display_name") or db.upper()
    description = (data.get("description") or "").strip()

    defaults = dict(data.get("defaults") or {})
    default_dtype = defaults.get("dtype", "str")

    tables_raw = data.get("tables") or {}
    if not isinstance(tables_raw, dict):
        raise ValueError("`tables` must be a mapping of table_name -> spec")

    tables: dict[str, Table] = {}
    for tname, tspec in tables_raw.items():
        if not isinstance(tspec, dict):
            raise ValueError(f"table {tname!r} spec must be a mapping")
        cols: list[Column] = []
        for craw in (tspec.get("columns") or []):
            if not isinstance(craw, dict) or not craw.get("name"):
                raise ValueError(f"{tname}: each column needs a `name` (got {craw!r})")
            cols.append(Column(
                name=craw["name"],
                dtype=craw.get("dtype", default_dtype),
                role=craw.get("role", ""),
                fk_target=craw.get("fk_target", ""),
                exec_schema=bool(craw.get("exec_schema", True)),
                kg_schema=bool(craw.get("kg_schema", True)),
                queryable=bool(craw.get("queryable", False)),
                key_column=bool(craw.get("key_column", False)),
                concept_type=craw.get("concept_type", ""),
                enum=list(craw.get("enum") or []),
                description=(craw.get("description") or "").strip(),
                llm_note=(craw.get("llm_note") or "").strip(),
                exec_note=(craw.get("exec_note") or "").strip(),
                kg_name=craw.get("kg_name", ""),
                parquet_name=craw.get("parquet_name", ""),
                added_at_load=bool(craw.get("added_at_load", False)),
            ))
        tables[tname] = Table(
            name=tname,
            parquet=tspec.get("parquet", ""),
            role=tspec.get("role", "association"),
            description=(tspec.get("description") or "").strip(),
            columns=cols,
            kg_name=tspec.get("kg_name", ""),
        )

    loader = data.get("loader") or {}
    q = data.get("questions") or {}
    q_templates = [
        QuestionTemplate(
            template=t["template"],
            required=list(t.get("required") or []),
            label=t.get("label", ""),
            grid=dict(t.get("grid") or {}),
        )
        for t in (q.get("templates") or []) if isinstance(t, dict) and t.get("template")
    ]

    obj = DbSchema(
        db=db,
        display_name=display_name,
        description=description,
        parquet_dir=data.get("parquet_dir") or db,
        defaults=defaults,
        tables=tables,
        decoration_tables=list(data.get("decoration_tables") or []),
        loader_keep_native=list(loader.get("keep_native") or []),
        loader_renames=dict(loader.get("renames") or {}),
        loader_generated=bool(loader.get("generated", True)),
        rules=dict(data.get("rules") or {}),
        q_params=dict(q.get("params") or {}),
        q_templates=q_templates,
        q_literal=list(q.get("literal") or []),
    )
    _infer_roles(obj)
    return obj


def _infer_roles(obj: DbSchema) -> None:
    """Fill role/fk_target where the author left them blank (DRY inference)."""
    for table in obj.tables.values():
        id_cols = [c for c in table.columns if c.name.endswith("_id")]
        for c in table.columns:
            if c.role:
                continue
            master = _master_for_id(c.name)
            if master and table.is_master and master == table.name and len(id_cols) == 1:
                c.role = "pk"
            elif master and master in obj.tables:
                c.role = "fk"
                if not c.fk_target:
                    c.fk_target = f"{master}.{c.name}"
            else:
                c.role = "attr"


def validate(obj: DbSchema) -> list[str]:
    """Return a list of human-readable errors ([] == valid)."""
    errs: list[str] = []
    if len(obj.description) < 50:
        errs.append(f"description must be >= 50 chars (got {len(obj.description)})")

    for tname, table in obj.tables.items():
        if not table.parquet:
            errs.append(f"{tname}: missing `parquet`")
        pks = [c for c in table.columns if c.role == "pk"]
        if table.is_master and len(pks) != 1:
            errs.append(f"{tname}: master table needs exactly 1 pk column (got {len(pks)})")
        for c in table.columns:
            if canon_dtype(c.dtype) == "":
                errs.append(f"{tname}.{c.name}: empty dtype")
            if c.queryable and not c.kg_schema:
                errs.append(f"{tname}.{c.name}: queryable=true requires kg_schema=true")
            if c.concept_type and not c.queryable:
                errs.append(f"{tname}.{c.name}: concept_type set but column not queryable")
            if c.concept_type and c.concept_type not in CONCEPT_TYPES:
                errs.append(f"WARN {tname}.{c.name}: concept_type {c.concept_type!r} "
                            f"not in CONCEPT_TYPES (typo? extend the vocab if intentional)")
            if c.role == "fk" and c.fk_target:
                mt, _, mc = c.fk_target.partition(".")
                if mt not in obj.tables:
                    errs.append(f"{tname}.{c.name}: fk_target {c.fk_target!r} -> unknown table")
                elif not obj.tables[mt].col(mc):
                    errs.append(f"{tname}.{c.name}: fk_target {c.fk_target!r} -> unknown column")

    for tpl in obj.q_templates:
        for pkey, plist in tpl.grid.items():
            if plist not in obj.q_params:
                errs.append(f"question template {tpl.label!r}: grid param {plist!r} "
                            f"not defined in questions.params")
        cols = {c.name for t in obj.tables.values() for c in t.columns}
        for req in tpl.required:
            # `required` may reference logical concept names (drug_name) that are
            # column names; tolerate concept aliases, only flag obvious typos.
            if req not in cols and req not in CONCEPT_TYPES:
                errs.append(f"WARN question template {tpl.label!r}: required {req!r} "
                            f"is neither a column nor a known concept")
    return errs


def load(path: Path) -> DbSchema:
    import yaml
    if not path.exists():
        raise FileNotFoundError(f"no schema.yaml at {path}")
    obj = from_dict(yaml.safe_load(path.read_text()))
    fatal = [e for e in validate(obj) if not e.startswith("WARN")]
    if fatal:
        raise ValueError(f"{path}: invalid schema.yaml:\n  - " + "\n  - ".join(fatal))
    return obj


__all__ = [
    "Column", "Table", "DbSchema", "QuestionTemplate",
    "from_dict", "validate", "load", "canon_dtype", "CONCEPT_TYPES",
]
