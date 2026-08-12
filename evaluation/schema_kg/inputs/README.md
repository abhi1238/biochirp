# `schema_kg/inputs/` — the per-DB schema-KG input contract

This tree is the **ground truth** for which databases are schema_kg-enabled and
what the shared planner/worker/chat pipeline (`app/per_db_tool/`) knows about
each one. There is no second copy of the DB list anywhere:
`config/schema_kg_dbs.discover_schema_kg_dbs()` defines "a DB is schema_kg-enabled
iff `schema_kg/inputs/<db>/schema.json` exists", and `scripts/gen_compose.py`,
`app/tools/schema_mapper`, and `app/tools/schema_planner` all read that one
function. **Dropping a new `<db>/schema.json` here auto-registers the DB
everywhere downstream** (lean image, nginx route, mapper/planner warm-lists) with
no further edits.

```
schema_kg/inputs/
├── shared_maps.json          # cross-DB maps (trade_name_map, gene_alias_map) — NOT per-DB
└── <db>/
    ├── schema.json           # REQUIRED — tables → columns → prose descriptions
    ├── queryable.json        # REQUIRED — per-column: can a user name this value?
    ├── concept_type.json     # REQUIRED — per-queryable-column → closed concept type
    ├── schema_rules.json     # REQUIRED — DB identity + grounding rules for LLM prompts
    ├── questions.json        # REQUIRED — labelled example questions (eval + few-shot)
    └── parquet_map.json       # OPTIONAL — table → parquet filename (only when names differ)
```

A DB counts as onboarded only when the five **required** files are present and
mutually consistent (every key in `queryable.json` / `concept_type.json` must
resolve to a `table.column` declared in `schema.json`). Build them with
`python schema_kg/src/build.py --inputs schema_kg/inputs/<db>` rather than by
hand where possible.

---

## File contracts

All key-bearing files use **fully-qualified dotted keys**: `<db>.<table>.<column>`.

### `schema.json` — REQUIRED
Nested map: a single top-level key (the db slug) → `{table: {column: "<prose
description>"}}`. The description string is what the schema-mapper embeds and what
the LLM column-selector reads, so it must be specific and **grounded in the actual
parquet** (real example values, real enum members — never invented; see the
project's disk-first-prose rule).

```json
{
  "string": {
    "protein_master_table_string": {
      "protein_id": "STRING protein identifier (e.g. '9606.ENSP00000275493') — join key only",
      "gene_symbol": "HGNC short gene symbol (e.g. 'EGFR', 'TP53')",
      "protein_size": "Protein length in amino acids (integer, e.g. 1210 for EGFR)"
    }
  }
}
```

### `queryable.json` — REQUIRED
Flat `{"<db>.<table>.<column>": bool}` (+ a `_comment`). `true` = a user can name
this value in a natural-language query (`gene_symbol`, `annotation`). `false` =
join key / internal ID / raw FK column the user never types (`protein_id`,
`protein_partner_id`). Every key must exist in `schema.json`.

### `concept_type.json` — REQUIRED
Flat `{"<db>.<table>.<column>": "<concept_type>"}` (+ a `_comment`). Maps each
**queryable** column to a value from the closed concept vocabulary. Two columns in
different DBs sharing a concept_type get a cross-DB `ln` (link) edge, which is how
multi-DB routing finds bridges (e.g. every `gene_symbol` column links). Use an
existing concept type wherever one fits; only mint a new one for a genuinely new
entity kind.

### `schema_rules.json` — REQUIRED
DB identity + grounding rules injected into the expander / filter / mapper /
planner prompts at runtime (consumed by `schema_kg_planner.py` and
`schema_kg/src/{value_mapper,hybrid_retrieval,query_expander,llm_filter}.py`).

Stable core keys (present for every DB):
`db_name`, `db_display_name`, `db_description`, `mandatory_entity_columns`,
`xref_id_columns`, `enum_columns`, `schema_grounding_notes`, `co_output_rules`,
`column_notes_override`.

Per-DB **ad-hoc** keys are allowed and expected (e.g. ctd's `stressor_class_map`,
hcdt's `few_shot_examples`) — this is intentional extensibility, not drift.

### `questions.json` — REQUIRED
A `list[{label, question, required}]`: `label` groups query intents, `question` is
natural-language, `required` is the list of column/concept names a correct answer
must surface. Used by the benchmark harness and as few-shot material.

### `parquet_map.json` — OPTIONAL
Flat `{table_name: "<file>.parquet"}`. Needed **only** when a table's on-disk
parquet filename differs from its schema.json table name (e.g. ctd's `_v2`
suffixes). Present today only for ctd, hcdt, ttd; omit it when names match.

---

## Table-naming convention (CANONICAL)

**Every table name MUST end with the `_<db>` suffix** (`protein_master_table_string`,
`disease_xref_orphanet`). This is the canonical convention for new databases. It
keeps schema.json, `config/schema.py`, and the `dbs/<db>/manifest.yaml`
documentation reading uniformly and lets `scripts/schema_manifest_sync.py`
reconcile them automatically (it strips a single `_<db>` suffix from either side).

Enforced by **`scripts/check_table_naming.py`** — it fails on any NEW DB/table
that omits the suffix. The runtime planner is itself suffix-AGNOSTIC (it
normalises `_<db>` when resolving — `app/per_db_tool/schema_kg_planner.py`), so
this is a consistency lint, not a correctness gate.

> The suffix is applied **inconsistently** across the existing 11 DBs (hcdt and
> ttd use bare names; chembl/clinvar/msigdb/reactome are partial). Those 49 tables
> are GRANDFATHERED in `scripts/table_naming_baseline.json` — they are NOT renamed,
> because the runtime tolerates them and a mass-rename would be risk for zero
> functional gain. New tables must still follow the convention. Do **not** rename
> an existing DB's tables without also updating `config/schema.py`, its
> `parquet_map.json`, and rebuilding the graph — the join resolution depends on
> those names lining up.

---

## Consistency gates

After editing any file here, run:

```bash
python schema_kg/src/build.py --inputs schema_kg/inputs/<db>   # rebuild graph/index
python scripts/schema_manifest_sync.py --db <db>               # schema.py ↔ manifest drift
python scripts/preflight_schema_check.py --db <db>             # schema.py ↔ parquet
```

See [`../../dbs/README.md`](../../dbs/README.md) for the full new-DB onboarding
procedure and `scripts/onboard_db.py --check <db>` for a one-shot status report.
