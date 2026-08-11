import polars as pl
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")

from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace

# Memory-sharing note (M2 audit, deferred 2026-05-21):
# ----------------------------------------------------
# All `read_parquet_polars` calls below return polars LazyFrames — no data
# is loaded into RAM until a query .collect()s downstream. The earlier
# audit claim "6 workers × 13 tables × 200 MB = 1+ GB duplicated" was a
# worst-case upper bound that only materialises during concurrent query
# bursts. In the steady-state idle case, each worker holds 13 LazyFrame
# metadata refs (~kilobytes total).
#
# A true `gunicorn --preload` + fork-copy-on-write fix requires:
#   1. Materialising the 13 tables into eager DataFrames at module-import
#      time (so the shared memory has data, not just metadata).
#   2. A bespoke Dockerfile for the TTD service (the shared
#      Dockerfile.service uses `uvicorn --workers N` — no fork-copy).
#   3. Cross-checking that polars eager-DataFrames are actually
#      fork-shareable on Linux (they use mmap under the hood; should be).
#   4. Regression testing every filter path that today expects LazyFrames.
#
# This is a 1-2 day spike with non-trivial test coverage risk and was
# deferred from the 10-user launch readiness window. Track in:
#   docs/optimization_backlog.md  (to be created if not yet present).

# N4 (2026-05-21): columns we want to leave in their native parquet type
# (typically Int64) instead of casting to Utf8. The whitelist is global —
# read_parquet_polars only applies it to tables where the column actually
# exists, so listing here is harmless for tables that lack the column.
#
# Conservative initial set: ONE column that is unambiguously numeric in
# cheminformatics and is filtered as exact-match equality (never with
# `.str.contains` or other string ops). Add more after benchmarking each:
#   - `pubchem_cid`: int PubChem CID. Exact-match filter, never substring.
# Candidates to evaluate next (NOT enabled by default — needs verification
# that no downstream filter uses .str. on them):
#   - `pubmed_id`, `activity_value`, `target_id` (TTD's are alphanumeric
#     "T<6-digit>" → must stay Utf8)
# >>> GENERATED ttd LOADER BEGIN — DO NOT EDIT (source: dbs/ttd/schema.yaml) <<<
_TTD_KEEP_NATIVE: tuple[str, ...] = ("pubchem_cid", "activity_value")

_TTD_TABLES: tuple[tuple[str, str], ...] = (
    ("biomarker_disease_association_ttd", "P1-08-Biomarker_disease_v2.parquet"),
    ("biomarker_master_table_ttd", "biomarker_master_table_ttd_v2.parquet"),
    ("disease_master_table_ttd", "disease_master_table_ttd_v2.parquet"),
    ("drug_crossmatching_association_ttd", "drug_crossmatching_association_ttd_v2.parquet"),
    ("drug_disease_association_ttd", "P1-05-Drug_disease_v2.parquet"),
    ("drug_master_table_ttd", "drug_master_table_ttd_v2.parquet"),
    ("drug_synonyms_association_ttd", "drug_synonyms_association_ttd_v2.parquet"),
    ("drug_target_association_ttd", "P1-07-Drug-TargetMapping_v2.parquet"),
    ("pathway_master_table_ttd", "pathway_master_table_ttd_v2.parquet"),
    ("target_compound_activity_association_ttd", "target_compound_activity_association_ttd_v2.parquet"),
    ("target_disease_association_ttd", "P1-06-Target_disease_v2.parquet"),
    ("target_master_table_ttd", "target_master_table_ttd_v2.parquet"),
    ("target_pathway_association_ttd", "P4-01-Target-KEGGpathway_all_v2.parquet"),
    ("target_uniprot_association_ttd", "target_uniprot_association_ttd_v2.parquet"),
)
# >>> GENERATED ttd LOADER END <<<


def return_preprocessed_ttd() -> dict[str, pl.DataFrame]:

    tool = "ttd"

    results = {}
    for key, fname in _TTD_TABLES:
        try:
            results[key] = read_parquet_polars(
                path="database", database="ttd", name=fname, keep_native=_TTD_KEEP_NATIVE,
            )
        except Exception as e:
            logger.warning("[%s] %s missing — re-run preprocess_v2.ipynb to generate: %s",
                           tool, fname, e)

        # ---- standardize column names ----
    # >>> GENERATED ttd RENAMES BEGIN — DO NOT EDIT (source: dbs/ttd/schema.yaml) <<<
    mapping = {
        "DRUG_NAME": "drug_name",
        "Drug_Name": "drug_name",
        "GENE_SYMBOL": "gene_symbol",
        "Target": "gene_symbol",
        "Disease_Name": "disease_name",
        "# Disease_Name": "disease_name",
        "PathwayID": "pathway_id",
        "PATH_NAME": "pathway_name",
        "path_name": "pathway_name",
        "Pubchem_CID": "pubchem_cid",
        "Biomarker_Name": "biomarker_name",
        "TTDID": "target_id",
        "KEGG pathway ID": "pathway_id",
        "KEGG pathway name": "pathway_name",
        "TargetID": "target_id",
        "DrugID": "drug_id",
        "MOA": "drug_mechanism_of_action_on_target",
        "moa": "drug_mechanism_of_action_on_target",
    }
    # >>> GENERATED ttd RENAMES END <<<

    for name, df in results.items():
        try:
            rename_dict = {c: mapping[c] for c in df.columns if c in mapping}
            if rename_dict:
                logger.info(f"{[tool]} Renaming columns in '{name}': {rename_dict}")
            df = df.rename(rename_dict)
            results[name] = df
        except Exception as e:
            logger.warning(f"{[tool]} Failed to rename columns in '{name}': {e}")

    # Table-unique column names (schema invariant: no content column shared across
    # non-master tables). Per-table renames applied after the global mapping above.
    _TTD_PER_TABLE_RENAMES = {
        "drug_crossmatching_association_ttd": {
            "drug_name":   "crossmatch_drug_name",
            "pubchem_cid": "crossmatch_pubchem_cid",
        },
        "drug_synonyms_association_ttd": {
            "drug_name": "synonym_drug_name",
        },
        "target_compound_activity_association_ttd": {
            "gene_symbol": "compound_activity_gene_symbol",
            "pubchem_cid": "compound_activity_pubchem_cid",
        },
        "target_uniprot_association_ttd": {
            "gene_symbol": "uniprot_gene_symbol",
            "target_name": "uniprot_target_name",
        },
    }
    for _tname, _rmap in _TTD_PER_TABLE_RENAMES.items():
        _df = results.get(_tname)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tname] = _df.rename(_present)

    # 2026-05-23: removed gene_name→gene_symbol runtime rename — the three
    # TTD parquets (target_master_table, target_compound_activity_association,
    # target_uniprot_association) were rewritten on disk to use `gene_symbol`
    # natively, matching the manifest. Pre-rewrite backups in
    # database/ttd/.backup_pre_gene_symbol_2026_05_23/.

    # 2026-05-23: drop `#N/A` disease_id sentinel from association tables.
    # 2 rows in drug_disease_association_ttd, 1 row in target_disease_association_ttd
    # carry the string "#N/A" as disease_id; it's unambiguously bad data
    # (no matching row in disease_master_table_ttd) and would join to null.
    for assoc_table in ("drug_disease_association_ttd", "target_disease_association_ttd"):
        if assoc_table in results and "disease_id" in results[assoc_table].columns:
            results[assoc_table] = results[assoc_table].filter(pl.col("disease_id") != "#N/A")

    # ---- strip whitespace + deduplicate ----
    # NOTE: unscoped df.drop_nulls() drops every row with any null in any
    # column — that wipes out rows that have nulls only in optional metadata
    # columns (e.g. cas_rn, pubchem_cid). Skip it; the join/filter pipeline
    # handles nulls correctly.
    for name, df in results.items():
        try:
            df = strip_all_whitespace(df).unique()
            results[name] = df
        except Exception as e:
            logger.warning(f"{[tool]} Cleaning failed for '{name}': {e}")


    db = dict()
    db[tool] = results

    return db

    