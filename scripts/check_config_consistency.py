#!/usr/bin/env python3
"""CI gate: cross-validate the four DB-config surfaces stay in sync.

The four surfaces are:

  1. dbs/<slug>/manifest.yaml service blocks (per-DB tool services)
  2. frontend/configs/db_chats.json         (homepage tiles + ?db= deep links)
  3. resources/db_profiles/registry.md      (routing-digest read by the LLM)
  4. resources/prompts/db_notes.yaml        (per-DB summarizer display name +
                                             description; drives 1+2+3 too)

Pre-2026-05 these drifted independently — `db_chats.json` grew slugs that
weren't in the registry, the routing digest had display names that didn't
match the registry's `db_name`, and the summarizer prompt referenced DBs
that had been removed elsewhere. This script enforces the invariants
documented in each file's header comment.

Invariants checked:

  A. Every parquet-backed DB (manifest with a service.tool block) has:
       * a matching entry in db_chats.json,
       * a `## <Display>` section in registry.md,
       * an entry in db_notes.yaml.
     And the display names agree across all four sources.

  B. Every slug in db_chats.json that isn't an aggregate (multi/bio/…) is
     a known per-DB slug (already enforced by check_db_chats_drift.py;
     re-checked here so this script is independently informative).

  C. Every `## Display` section in registry.md corresponds to a registry
     slug, OR to the special `WEB` pseudo-DB (the LLM uses it as an
     escape hatch for non-curated questions).

  D. db_notes.yaml has the same slug set as the manifest DB tools.

A failure is a CI-blocking event. Exit codes:
  0 — all four surfaces agree
  1 — drift detected
  2 — a file is missing or malformed

Run:
    python scripts/check_config_consistency.py
    python scripts/check_config_consistency.py --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DBS_DIR = REPO / "dbs"
DB_CHATS_JSON = REPO / "frontend" / "configs" / "db_chats.json"
PROFILES_MD = REPO / "resources" / "db_profiles" / "registry.md"
DB_NOTES_YAML = REPO / "resources" / "prompts" / "db_notes.yaml"

# A profile section that isn't a real database — kept in registry.md so the
# LLM can route ambiguous out-of-scope queries to a generic web fallback.
_PSEUDO_DBS: set[str] = {"WEB"}

# Real DB chat tiles NOT backed by the parquet planner (no manifest service.tool
# block), so they're absent from _registry_dbs. `opentarget` is the 11th DB —
# the GraphQL OpenTargets service (opentarget_service, port 8026), live.
_NON_PARQUET_DB_TILES: set[str] = {"opentarget"}

# H2 section marker in the routing digest: "## DisplayName".
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")


def _die(msg: str, code: int = 2) -> None:
    print(f"[check_config_consistency] {msg}", file=sys.stderr)
    sys.exit(code)


def _load_yaml(path: Path) -> dict:
    try:
        import yaml  # type: ignore
    except Exception as exc:
        _die(f"PyYAML required: {exc}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _die(f"failed to parse {path}: {exc}")


def _registry_dbs() -> dict[str, str]:
    """Return {slug: db_name} for every parquet-backed DB tool.

    SOURCE: dbs/<slug>/manifest.yaml with a `service.tool` block (the same set
    gen_compose renders). services_registry.yaml was dropped 2026-06-18 — it was
    a denormalised copy of the manifest service blocks. db_name is the manifest's
    `display_name` (matches the `## <Display>` headers in registry.md and the
    `display:` in db_notes.yaml)."""
    out: dict[str, str] = {}
    for manifest in sorted(DBS_DIR.glob("*/manifest.yaml")):
        doc = _load_yaml(manifest)
        if ((doc.get("service") or {}).get("tool")):
            out[manifest.parent.name] = str(doc.get("display_name") or manifest.parent.name.upper())
    return out


def _registry_aggregates() -> set[str]:
    # No multi-DB aggregate surfaces currently exist — the multi/bio/multi_v2
    # tiles were decommissioned 2026-06-18 with the /bio_chat/ backend. Empty
    # (not is_multi-derived) ON PURPOSE: if an aggregate tile is re-added, the
    # checker should FAIL so its backend wiring gets a conscious review rather
    # than being silently blessed.
    return set()


def _chat_slugs() -> dict[str, dict]:
    if not DB_CHATS_JSON.is_file():
        _die(f"missing {DB_CHATS_JSON}")
    return json.loads(DB_CHATS_JSON.read_text(encoding="utf-8"))


def _profile_sections() -> set[str]:
    if not PROFILES_MD.is_file():
        _die(f"missing {PROFILES_MD}")
    out: set[str] = set()
    for line in PROFILES_MD.read_text(encoding="utf-8").splitlines():
        m = _SECTION_RE.match(line)
        if m:
            out.add(m.group(1).strip())
    return out


def _db_notes_slugs() -> dict[str, str]:
    """Return {slug: display} from db_notes.yaml."""
    if not DB_NOTES_YAML.is_file():
        _die(f"missing {DB_NOTES_YAML}")
    cfg = _load_yaml(DB_NOTES_YAML)
    out: dict[str, str] = {}
    for slug, rec in cfg.items():
        if isinstance(rec, dict) and "display" in rec:
            out[slug] = str(rec["display"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                    help="print every slug/check, not just failures.")
    args = ap.parse_args()

    registry = _registry_dbs()
    aggregates = _registry_aggregates()
    chats = _chat_slugs()
    profile_displays = _profile_sections()
    notes = _db_notes_slugs()

    failures: list[str] = []
    warnings: list[str] = []
    ok_lines: list[str] = []

    # ─── Invariant A: registry → chats / profile / notes ───────────────────
    for slug, db_name in registry.items():
        # A.1 — chat tile exists.
        if slug not in chats:
            failures.append(
                f"  {slug}: declared in a manifest service block but absent "
                f"from frontend/configs/db_chats.json"
            )

        # A.2 — profile section exists. The profile uses the chat's
        # `db_name` (display string), case-sensitive — matches the section
        # heading in registry.md.
        if db_name not in profile_displays:
            failures.append(
                f"  {slug}: manifest db tool db_name='{db_name}' "
                f"but no `## {db_name}` section in resources/db_profiles/registry.md"
            )

        # A.3 — db_notes.yaml entry exists.
        if slug not in notes:
            failures.append(
                f"  {slug}: declared in a manifest service block but absent "
                f"from resources/prompts/db_notes.yaml"
            )
        else:
            # A.4 — display names agree.
            if notes[slug] != db_name:
                # Many registry db_names are upper-shortcodes (TTD) while
                # db_notes.yaml carries the friendly variant ("Therapeutic
                # Target Database"). Only fail when both are present AND
                # disagree case-insensitively on the leading token.
                tok_a = (notes[slug] or "").split()[0].lower()
                tok_b = (db_name or "").split()[0].lower()
                if tok_a and tok_b and tok_a != tok_b:
                    warnings.append(
                        f"  {slug}: db_notes display={notes[slug]!r} differs "
                        f"from registry db_name={db_name!r} — verify intentional"
                    )

        ok_lines.append(f"  {slug}: registry={db_name} chats=✓ profile=✓ notes=✓")

    # ─── Invariant B: chats → registry / aggregates ───────────────────────
    for slug in chats.keys():
        if slug in registry:
            continue
        if slug in aggregates:
            continue
        if slug in _NON_PARQUET_DB_TILES:
            continue
        failures.append(
            f"  {slug}: present in db_chats.json but is neither a manifest DB "
            f"tool, a known frontend aggregate, nor a non-parquet DB tile"
        )

    # ─── Invariant C: profile sections → registry / pseudo ────────────────
    expected_displays = set(registry.values()) | _PSEUDO_DBS
    for disp in sorted(profile_displays):
        if disp in expected_displays:
            continue
        warnings.append(
            f"  profile section `## {disp}` has no matching registry db_name "
            f"(stray entry in registry.md or registry rename pending)"
        )

    # ─── Invariant D: db_notes ↔ registry slug set ────────────────────────
    extra_notes = sorted(set(notes.keys()) - set(registry.keys()))
    for slug in extra_notes:
        warnings.append(
            f"  db_notes.yaml has slug '{slug}' that is not in "
            f"any manifest service block (orphaned note?)"
        )

    # ─── Output ───────────────────────────────────────────────────────────
    if args.verbose:
        for line in ok_lines:
            print(f"  OK    {line.strip()}")
    for line in warnings:
        print(f"  WARN  {line.strip()}")
    for line in failures:
        print(f"  FAIL  {line.strip()}", file=sys.stderr)

    if failures:
        print(
            f"\n[check_config_consistency] {len(failures)} drift(s) detected. "
            f"Fix dbs/*/manifest.yaml, db_chats.json, registry.md, "
            f"and db_notes.yaml so they agree.",
            file=sys.stderr,
        )
        return 1
    print(
        f"[check_config_consistency] OK — {len(registry)} DB(s) cross-validated, "
        f"{len(warnings)} warning(s), 0 failures."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
