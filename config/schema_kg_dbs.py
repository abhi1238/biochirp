"""Single source of truth for the set of schema_kg-enabled databases.

Before this module, the schema_kg DB list was hard-coded in THREE places that
had to be kept in sync by hand:

  * scripts/gen_compose.py              SCHEMA_KG_DBS  (lean Dockerfile + nginx routes)
  * app/tools/schema_mapper/app/main.py _DEFAULT_DBS   (warm bge/Qdrant at boot)
  * app/tools/schema_planner/app/main.py _DEFAULT_DBS   (pre-build FK graphs at boot)

Editing one and forgetting another silently half-registered a new DB (no nginx
chat route, or never warmed/pre-built in a shared service). All three now call
``discover_schema_kg_dbs`` instead.

GROUND TRUTH = the ``schema_kg/inputs/<db>/`` directory tree (a DB is
schema_kg-enabled iff ``schema_kg/inputs/<db>/schema.json`` exists). That tree
is exactly what the planner (``get_planner`` / ``build_schema_graph``) loads, so
it is the definition, not a proxy for it. This module is just a shared READER of
that truth — there is no second copy of the DB list anywhere.

``schema_kg/`` is mounted into the schema_mapper / schema_planner containers at
``/app/schema_kg/`` and lives at ``<repo>/schema_kg/`` on the host (where
gen_compose runs), so the same discovery works in both contexts.

There is intentionally NO fallback list. If the inputs tree cannot be found, the
configuration is broken (missing mount / bad checkout) AND the planner itself
would fail to load those same files, so a frozen list would only mask the real
fault behind a confusing downstream error. We raise loudly at the point of
discovery instead.
"""
from __future__ import annotations

import os
from pathlib import Path


def _candidate_inputs_roots() -> list[Path]:
    """Possible locations of schema_kg/inputs in container and host contexts."""
    roots = [
        Path("/app/schema_kg/inputs"),                         # inside containers
        Path(__file__).resolve().parents[1] / "schema_kg" / "inputs",  # repo host
    ]
    env_root = os.getenv("SCHEMA_KG_INPUTS_ROOT")
    if env_root:
        roots.insert(0, Path(env_root))
    return roots


def discover_schema_kg_dbs(inputs_root: str | os.PathLike | None = None) -> set[str]:
    """Return the set of schema_kg-enabled DB slugs by scanning the inputs tree.

    A DB counts iff ``<inputs_root>/<db>/schema.json`` exists.

    Raises FileNotFoundError if no inputs tree is found, or one is found but
    contains no DB with a schema.json — both are misconfigurations that must
    surface immediately rather than degrade to a stale hard-coded list.
    """
    roots = [Path(inputs_root)] if inputs_root is not None else _candidate_inputs_roots()
    for root in roots:
        if not root.is_dir():
            continue
        found = {p.name for p in root.iterdir()
                 if p.is_dir() and (p / "schema.json").is_file()}
        if found:
            return found
        raise FileNotFoundError(
            f"[schema_kg_dbs] {root} exists but contains no <db>/schema.json — "
            "the schema_kg inputs tree looks empty/corrupt."
        )
    raise FileNotFoundError(
        "[schema_kg_dbs] no schema_kg/inputs tree found in any of "
        f"{[str(r) for r in roots]} — is schema_kg/ mounted (containers) or are "
        "you running from the repo root (host)? Set SCHEMA_KG_INPUTS_ROOT to override."
    )


__all__ = ["discover_schema_kg_dbs"]
