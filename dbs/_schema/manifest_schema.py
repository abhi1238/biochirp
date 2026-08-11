"""BioChirp DB-manifest schema.

A manifest is the human-authored documentation surface for one database: prose
description, key columns, sources, and the Docker/nginx `service:` spec that
`scripts/gen_compose.py` reads directly. Authors write one manifest.yaml per DB.

See `dbs/README.md` for the full new-DB onboarding procedure and
`scripts/onboard_db.py --check <slug>` for a per-touch-point wiring report.

This module defines:
    * The Manifest dataclass (Python-side validation + accessors).
    * `from_dict` / `load` — parse + validate a manifest, with error messages.

NOTE: the `derived:` block and the `skip_relations` / `force_relations` /
`explicit_capability_triggers` knobs are LEGACY — they fed the now-deleted
graph-router onboarding pipeline and are no longer read by any live code. They
remain in the dataclass for backward compatibility with existing manifests.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ─── Field-level dataclasses ────────────────────────────────────────────────


@dataclass
class InputField:
    """One input field accepted by the DB's query interface."""
    field: str                       # canonical CommonField name (gene_symbol, drug_name, ...)
    examples: list[str] = field(default_factory=list)
    description: str = ""            # optional one-liner; useful for LLM card-gen


@dataclass
class OutputField:
    """One output field the DB emits (used for outputs→inputs hop edges)."""
    field: str
    description: str = ""


@dataclass
class ColumnSpec:
    name: str
    description: str
    examples: list[str] = field(default_factory=list)


@dataclass
class TableSpec:
    description: str
    key_columns: list[ColumnSpec] = field(default_factory=list)


@dataclass
class IDPattern:
    regex: str
    description: str = ""


@dataclass
class ServiceSpec:
    """Docker / nginx service spec for a per-DB parquet service.

    Optional — only parquet-backed DBs have a `service:` block. Remote-API
    DBs (AlphaFold, gnomAD, GTEx, openFDA, …) leave this empty since they
    don't run their own container.

    Mirrors the shape gen_compose reads directly from this manifest so scripts/
    gen_compose.py can keep reading the generated registry verbatim.

    Note: the per-DB chat service fields (chat_port, chat_workers, chat_db_name,
    etc.) were removed 2026-06-24 — the per-DB chat containers were decommissioned
    and no live manifest YAML or gen_compose.py code referenced them.
    """
    # Tool service (the parquet/HTTP backend)
    tool_port: int = 0
    tool_workers: int = 2
    tool_memory_limit: str = "4g"
    tool_memory_reservation: str = "1g"
    tool_healthcheck_start_period: str = "60s"
    tool_extra_depends_on: list[str] = field(default_factory=list)
    tool_extra_volumes: list[str] = field(default_factory=list)   # additional bind-mounts
    tool_module: str = ""                                          # override default module name
    tool_env: dict[str, str] = field(default_factory=dict)


# ─── The top-level Manifest ─────────────────────────────────────────────────


@dataclass
class Manifest:
    # ---- Author-provided ---------------------------------------------------
    name: str                                          # canonical slug (lowercase)
    display_name: str                                  # human-readable
    description: str                                   # 100-300 words

    schema_inputs: list[InputField] = field(default_factory=list)
    schema_outputs: list[OutputField] = field(default_factory=list)
    schema_tables: dict[str, TableSpec] = field(default_factory=dict)

    version: str = ""
    license: str = ""
    sources: list[str] = field(default_factory=list)

    # Optional author overrides (skip auto-derivation for these)
    valid_queries: Optional[list[str]] = None          # if None → LLM-generated
    invalid_queries: Optional[list[str]] = None        # if None → LLM-generated
    explicit_id_patterns: list[IDPattern] = field(default_factory=list)
    explicit_capability_triggers: list[dict] = field(default_factory=list)

    # Knobs for automation
    is_remote: bool = False                            # parquet-backed (False) vs live API (True)
    domain_hint: str = ""                              # high-level domain label
    skip_relations: list[str] = field(default_factory=list)   # never assign these
    force_relations: dict[str, float] = field(default_factory=dict)  # name → weight

    # Service block — Docker / nginx spec for parquet-backed DBs. Optional;
    # remote-API DBs leave this empty. When non-empty, scripts/onboard_db.py
    # gen_compose.py reads service blocks from all manifests directly so
    # scripts/gen_compose.py can consume them unchanged.
    service: Optional[ServiceSpec] = None

    # ---- Derived (regenerated every onboard run) ---------------------------
    # These live under `derived:` in the YAML. Author should NEVER hand-edit.
    derived: dict[str, Any] = field(default_factory=dict)
    # Expected derived keys:
    #   relation_weights:    {relation_name: float} per derived edge
    #   auto_triggers:       [str] distinctive keywords (TF-IDF)
    #   auto_id_patterns:    [{regex, description}] from examples
    #   capability_card:     {valid_queries, invalid_queries, embed_text}
    #   column_rows:         [{table, column, description}]
    #   threshold_tau:       float — per-DB calibration starting point
    #   manifest_hash:       hash of author-provided content

    # ─── Convenience ───────────────────────────────────────────────────────

    def author_hash(self) -> str:
        """Hash of author-provided content only (excludes `derived:`).

        When this hash doesn't change between runs, onboarding can short-circuit.
        """
        h = hashlib.sha256()
        payload = {
            "name": self.name,
            "description": self.description.strip(),
            "schema_inputs": [(i.field, sorted(i.examples), i.description) for i in self.schema_inputs],
            "schema_outputs": [(o.field, o.description) for o in self.schema_outputs],
            "schema_tables": {
                t: {
                    "description": spec.description,
                    "key_columns": [(c.name, c.description, sorted(c.examples)) for c in spec.key_columns],
                }
                for t, spec in sorted(self.schema_tables.items())
            },
            "valid_queries": self.valid_queries,
            "invalid_queries": self.invalid_queries,
            "explicit_id_patterns": [(p.regex, p.description) for p in self.explicit_id_patterns],
            "explicit_capability_triggers": self.explicit_capability_triggers,
            "skip_relations": sorted(self.skip_relations),
            "force_relations": dict(sorted(self.force_relations.items())),
        }
        h.update(json.dumps(payload, sort_keys=True, default=str).encode())
        return h.hexdigest()[:16]

    def to_yaml_dict(self) -> dict:
        """Round-trip-safe dict for yaml.safe_dump."""
        d: dict[str, Any] = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description.strip() + "\n",
        }
        if self.version:
            d["version"] = self.version
        if self.license:
            d["license"] = self.license
        if self.sources:
            d["sources"] = self.sources
        if self.is_remote:
            d["is_remote"] = True
        if self.domain_hint:
            d["domain_hint"] = self.domain_hint

        d["schema"] = {
            "inputs": [
                {k: v for k, v in [
                    ("field", i.field),
                    ("description", i.description),
                    ("examples", i.examples),
                ] if v}
                for i in self.schema_inputs
            ],
            "outputs": [
                {k: v for k, v in [
                    ("field", o.field),
                    ("description", o.description),
                ] if v}
                for o in self.schema_outputs
            ],
        }
        if self.schema_tables:
            d["schema"]["tables"] = {
                t: {
                    "description": spec.description,
                    "key_columns": [
                        {k: v for k, v in [
                            ("name", c.name),
                            ("description", c.description),
                            ("examples", c.examples),
                        ] if v}
                        for c in spec.key_columns
                    ],
                }
                for t, spec in self.schema_tables.items()
            }

        if self.valid_queries is not None:
            d["valid_queries"] = self.valid_queries
        if self.invalid_queries is not None:
            d["invalid_queries"] = self.invalid_queries
        if self.explicit_id_patterns:
            d["explicit_id_patterns"] = [
                {"regex": p.regex, "description": p.description}
                for p in self.explicit_id_patterns
            ]
        if self.explicit_capability_triggers:
            d["explicit_capability_triggers"] = self.explicit_capability_triggers
        if self.skip_relations:
            d["skip_relations"] = self.skip_relations
        if self.force_relations:
            d["force_relations"] = dict(self.force_relations)
        if self.service is not None and self.service.tool_port:
            svc: dict[str, Any] = {}
            t: dict[str, Any] = {}
            if self.service.tool_port:                t["port"] = self.service.tool_port
            if self.service.tool_workers != 2:        t["workers"] = self.service.tool_workers
            if self.service.tool_memory_limit != "4g":      t["memory_limit"] = self.service.tool_memory_limit
            if self.service.tool_memory_reservation != "1g": t["memory_reservation"] = self.service.tool_memory_reservation
            if self.service.tool_healthcheck_start_period != "60s":
                t["healthcheck_start_period"] = self.service.tool_healthcheck_start_period
            if self.service.tool_extra_depends_on:    t["extra_depends_on"] = self.service.tool_extra_depends_on
            if self.service.tool_extra_volumes:       t["extra_volumes"] = self.service.tool_extra_volumes
            if self.service.tool_module:              t["module"] = self.service.tool_module
            if self.service.tool_env:                 t["env"] = self.service.tool_env
            if t: svc["tool"] = t
            if svc:
                d["service"] = svc

        if self.derived:
            d["derived"] = self.derived
        return d


# ─── Loader + Validator ────────────────────────────────────────────────────


def _slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]+", s or ""))


def from_dict(data: dict) -> Manifest:
    """Construct a Manifest from a YAML-parsed dict. Raises ValueError on issue."""
    if not isinstance(data, dict):
        raise ValueError("manifest must be a YAML mapping")
    name = data.get("name")
    if not name or not _slug_ok(name):
        raise ValueError(f"name {name!r} must match [a-z][a-z0-9_]+ (slug)")
    display_name = data.get("display_name") or name
    description = (data.get("description") or "").strip()
    if len(description) < 50:
        raise ValueError(f"description for {name!r} must be ≥ 50 chars (got {len(description)})")

    schema = data.get("schema") or {}
    if not isinstance(schema, dict):
        raise ValueError("`schema` must be a mapping")
    inputs_raw = schema.get("inputs") or []
    outputs_raw = schema.get("outputs") or []
    tables_raw = schema.get("tables") or {}

    schema_inputs = [
        InputField(
            field=i["field"],
            examples=list(i.get("examples") or []),
            description=(i.get("description") or "").strip(),
        )
        for i in inputs_raw if isinstance(i, dict) and i.get("field")
    ]
    schema_outputs = [
        OutputField(
            field=o["field"],
            description=(o.get("description") or "").strip(),
        )
        for o in outputs_raw if isinstance(o, dict) and o.get("field")
    ]
    if not schema_inputs:
        raise ValueError(f"manifest {name!r} must declare ≥ 1 input field")

    schema_tables: dict[str, TableSpec] = {}
    for tname, tspec in tables_raw.items():
        if not isinstance(tspec, dict):
            continue
        cols = [
            ColumnSpec(
                name=c["name"],
                description=(c.get("description") or "").strip(),
                examples=list(c.get("examples") or []),
            )
            for c in (tspec.get("key_columns") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        schema_tables[tname] = TableSpec(
            description=(tspec.get("description") or "").strip(),
            key_columns=cols,
        )

    id_patterns = [
        IDPattern(
            regex=p["regex"],
            description=(p.get("description") or "").strip(),
        )
        for p in (data.get("explicit_id_patterns") or [])
        if isinstance(p, dict) and p.get("regex")
    ]
    # validate regexes compile
    for p in id_patterns:
        try:
            re.compile(p.regex)
        except re.error as exc:
            raise ValueError(f"manifest {name!r} explicit_id_patterns: bad regex {p.regex!r}: {exc}")

    svc_raw = data.get("service")
    service: Optional[ServiceSpec] = None
    if isinstance(svc_raw, dict):
        t = (svc_raw.get("tool") or {})
        service = ServiceSpec(
            tool_port=int(t.get("port") or 0),
            tool_workers=int(t.get("workers") or 2),
            tool_memory_limit=str(t.get("memory_limit") or "4g"),
            tool_memory_reservation=str(t.get("memory_reservation") or "1g"),
            tool_healthcheck_start_period=str(t.get("healthcheck_start_period") or "60s"),
            tool_extra_depends_on=list(t.get("extra_depends_on") or []),
            tool_extra_volumes=list(t.get("extra_volumes") or []),
            tool_module=str(t.get("module") or ""),
            tool_env=dict(t.get("env") or {}),
        )

    return Manifest(
        name=name,
        display_name=display_name,
        description=description,
        schema_inputs=schema_inputs,
        schema_outputs=schema_outputs,
        schema_tables=schema_tables,
        version=data.get("version") or "",
        license=data.get("license") or "",
        sources=list(data.get("sources") or []),
        valid_queries=data.get("valid_queries"),
        invalid_queries=data.get("invalid_queries"),
        explicit_id_patterns=id_patterns,
        explicit_capability_triggers=list(data.get("explicit_capability_triggers") or []),
        is_remote=bool(data.get("is_remote", False)),
        domain_hint=data.get("domain_hint") or "",
        skip_relations=list(data.get("skip_relations") or []),
        force_relations=dict(data.get("force_relations") or {}),
        service=service,
        derived=dict(data.get("derived") or {}),
    )


def load(path: Path) -> Manifest:
    """Load + validate a manifest from a YAML file."""
    import yaml
    if not path.exists():
        raise FileNotFoundError(f"no manifest at {path}")
    data = yaml.safe_load(path.read_text())
    return from_dict(data)


def save(manifest: Manifest, path: Path) -> None:
    """Write a manifest back to YAML (round-trip-safe)."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(
        manifest.to_yaml_dict(),
        sort_keys=False,
        width=88,
        default_flow_style=False,
        allow_unicode=True,
    ))
