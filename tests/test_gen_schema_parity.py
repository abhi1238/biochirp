"""SSOT parity + drift guard for the per-DB schema generator.

For every migrated DB (one with a hand-authored dbs/<db>/schema.yaml):
  * the SSOT must validate (db_schema.validate has no fatal errors), and
  * every generated surface — the 6 schema_kg JSONs, the config/schema.py block,
    and the loader table/renames blocks — must be byte-in-sync with what
    gen_schema would emit (i.e. nobody hand-edited a generated file).

This is the test counterpart of the CI `gen_schema.py --check --all` gate; it
runs without the parquet files, so it is safe in any CI runner. The exact
parquet/dtype validation (which needs the data volume) lives in
app/per_db_tool/_schema_guard.assert_db_schema_exact and runs in preflight.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dbs._schema import db_schema          # noqa: E402
from scripts import gen_schema             # noqa: E402

MIGRATED = gen_schema.migrated_dbs()


@pytest.mark.skipif(not MIGRATED, reason="no DB has a schema.yaml yet")
@pytest.mark.parametrize("db", MIGRATED)
def test_ssot_validates(db):
    obj = db_schema.load(ROOT / "dbs" / db / "schema.yaml")
    fatal = [e for e in db_schema.validate(obj) if not e.startswith("WARN")]
    assert not fatal, f"{db}/schema.yaml invalid:\n" + "\n".join(fatal)


@pytest.mark.skipif(not MIGRATED, reason="no DB has a schema.yaml yet")
@pytest.mark.parametrize("db", MIGRATED)
def test_generated_surfaces_in_sync(db):
    assert gen_schema.check(db) == 0, (
        f"{db}: a generated surface is stale or hand-edited — "
        f"run `python scripts/gen_schema.py --write --db {db}`")
