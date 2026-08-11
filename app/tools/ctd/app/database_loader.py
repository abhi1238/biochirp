# database_loader.py
import logging
import re

import polars as pl
import duckdb

from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn.error")


# 2026-05-19: DuckDB view for evidence-weighted chem→gene ranking.
# The 50-Q bench RCA found CTD previews for queries like "BPA genes" returning
# tail genes (`A1BG`, alphabetic-first) instead of the canonical endocrine
# targets (ESR1, ESR2, AR, NR3C1) — there was no precomputed evidence-count
# column for the rank machinery in app/utils/dataframe_filtering.py:920-933.
#
# 2026-05-19 (v2): per-pair COLLAPSE.
# chemical_gene_interaction_v2.parquet has MANY rows per (drug, gene) pair
# (e.g. BPA→INS = 1,982 rows; BPA→ESR1 = 410). The first version of this view
# kept all rows and only added a pubmed_count column — but the loader's
# downstream df.unique() preserved per-interaction-text variants, so the
# preview top-25 was dominated by INS variants and never reached ESR1.
#
# This view COLLAPSES to one row per (drug_id, gene_id) pair using GROUP BY:
#   - drug_id                      — FK to chemical_master_v2 (name joined at query time)
#   - gene_id                      — FK to gene_master_v2     (symbol joined at query time)
#   - pubmed_count                 — COUNT(*) per pair (intrinsic edge evidence weight)
#   - interaction_text             — first observed text (sample for synth)
#   - chemical_gene_interaction_actions     — pipe-joined distinct action types (chem-gene)
#   - pubmed_ids                   — pipe-joined distinct PMIDs (informational)
#   - organism, organism_id, gene_forms — first observed (v2 is human-only)
#
# 2026-06-22: FK-only normalization. drug_name/gene_symbol were previously
# baked in here, but that is redundant with the master tables and is exactly
# the denormalization the schema-KG planner is built to avoid (mirrored name
# columns make the Steiner tree stitch every mirror together → join explosion).
# The planner now joins drug_name from chemical_master_table_ctd and gene_symbol
# from gene_master_table_ctd at query time, identical to every other CTD
# association table. The view is retained ONLY for the per-pair COLLAPSE +
# pubmed_count (an intrinsic edge attribute, like inference_score), which is
# load-bearing for the BPA→ESR1/ESR2/AR ranking fix (_RANK_COLUMNS, evaluated
# on the post-join final_schema so pubmed_count survives the master joins).
#
# Net effect: BPA query still returns DISTINCT genes ranked by pubmed_count
# (INS 1982, ESR1 410, ESR2 146, AR 133, CASP3 128 …); names arrive via the
# planner's master joins instead of being baked in.
_CTD_CHEM_GENE_VIEW_SQL = """
SELECT
    cgi.drug_id,
    cgi.gene_id,
    ANY_VALUE(cgi.gene_forms)                         AS gene_forms,
    ANY_VALUE(cgi.organism)                           AS chemical_gene_organism,
    ANY_VALUE(cgi.organism_id)                        AS chemical_gene_organism_id,
    ANY_VALUE(cgi.interaction_text)                   AS interaction_text,
    STRING_AGG(DISTINCT cgi.interaction_actions, '|') AS chemical_gene_interaction_actions,
    STRING_AGG(DISTINCT cgi.pubmed_ids, '|')          AS chem_gene_pubmed_ids,
    COUNT(*)                                          AS pubmed_count
FROM read_parquet('database/ctd/chemical_gene_interaction_v2.parquet') cgi
GROUP BY cgi.drug_id, cgi.gene_id
"""


def return_preprocessed_ctd() -> dict:
    """
    Load and preprocess all CTD parquet tables into memory.

    Returns
    -------
    dict
        {
          "ctd": {
             "chemical_gene_association_ctd": pl.DataFrame,
             "chemical_master_table_ctd": pl.DataFrame,
             ...
          }
        }
    """
    tool = "ctd"
    results: dict[str, pl.DataFrame] = {}

    # CTD data v2 (2026-04 refresh from ctdbase.org/reports/) is the only
    # supported version. The legacy v1 parquets (pre-_v2 names) were
    # decommissioned 2026-06-18 — every v1 table is superseded by a v2 file:
    #   chem_gene_association        -> chemical_gene_interaction_v2 (DuckDB view)
    #   chemical_master_table        -> chemical_master_v2
    #   chemical_disease_association -> chemical_disease_association_v2
    #   disease_master_table         -> disease_master_v2
    #   disease_pathway_association  -> disease_pathway_association_v2
    #   gene_master_table            -> gene_master_v2
    #   gene_pathway_association     -> genes_pathways_v2 (projected to gene_id, pathway_id)
    #   pathway_master_table         -> pathway_master_v2
    #   gene_disease_association     -> gene_disease_association_v2

    # ---- load parquet tables ----
    # chemical_gene_association: route through the DuckDB view so the result
    # carries a `pubmed_count` column (window-aggregated per drug-gene pair).
    # The downstream rank machinery auto-picks it up.
    logger.info("[%s] loading chemical_gene_association via DuckDB view (pubmed_count)", tool)
    _con = duckdb.connect(":memory:")
    try:
        chemical_gene_association = _con.sql(_CTD_CHEM_GENE_VIEW_SQL).pl()
    finally:
        _con.close()
    chemical_master_table = read_parquet_polars(
        path="database", database="ctd", name="chemical_master_v2.parquet")
    chemical_disease_association = read_parquet_polars(
        path="database", database="ctd", name="chemical_disease_association_v2.parquet")
    disease_master_table = read_parquet_polars(
        path="database", database="ctd", name="disease_master_v2.parquet")
    disease_pathway_association = read_parquet_polars(
        path="database", database="ctd", name="disease_pathway_association_v2.parquet")
    gene_master_table = read_parquet_polars(
        path="database", database="ctd", name="gene_master_v2.parquet")
    # gene_pathway_association_ctd (declared 2-col join table) is derived from
    # the richer genes_pathways_v2 — a strict superset (same 135,792 rows, adds
    # gene_symbol + pathway_name) — rather than the decommissioned v1 parquet.
    gene_pathway_association = read_parquet_polars(
        path="database", database="ctd", name="genes_pathways_v2.parquet"
    )
    pathway_master_table = read_parquet_polars(
        path="database", database="ctd", name="pathway_master_v2.parquet")
    gene_disease_association = read_parquet_polars(
        path="database", database="ctd", name="gene_disease_association_v2.parquet")
    # ---- assemble into result dict ----
    results["chemical_gene_association_ctd"] = chemical_gene_association
    results["chemical_master_table_ctd"] = chemical_master_table
    results["chemical_disease_association_ctd"] = chemical_disease_association
    results["disease_master_table_ctd"] = disease_master_table
    results["disease_pathway_association_ctd"] = disease_pathway_association
    results["gene_master_table_ctd"] = gene_master_table
    results["gene_pathway_association_ctd"] = gene_pathway_association
    results["pathway_master_table_ctd"] = pathway_master_table
    results["gene_disease_association_ctd"] = gene_disease_association

    # v2 NEW tables: direct chem→pathway, chem→phenotype, exposure.
    # DISABLED (low biological value, not saved by preprocess_v2.ipynb — 2026-06-21):
    #   chemical_go_enriched_v2, anatomy_master_v2,
    #   phenotype_disease_bp/cc/mf_v2, disease_go_bp/cc/mf_v2, chem_gene_ixn_types_v2
    for tbl in ("chemical_pathway_enriched_v2",
                "chemical_phenotype_ixn_v2",
                "exposure_studies_v2",
                "exposure_events_v2",
                "exposure_study_disease_association_v2",
                "exposure_study_stressor_association_v2"):
        try:
            key = tbl.replace("_v2","") + "_ctd"
            results[key] = read_parquet_polars(
                path="database", database="ctd", name=f"{tbl}.parquet")
        except Exception as e:
            logger.warning("[%s] optional v2 table %s missing: %s", tool, tbl, e)

    # Table-unique column names (schema invariant: no content column shared across
    # non-master tables — see scripts/check_column_uniqueness.py).
    # chemical_gene_association gets organism/organism_id renamed via the DuckDB
    # view above (AS chemical_gene_organism / AS chemical_gene_organism_id).
    # chemical_phenotype_ixn carries the same raw parquet column names, so rename
    # at load time to give each table a unique, table-prefixed name.
    _cpixn = results.get("chemical_phenotype_ixn_ctd")
    if _cpixn is not None:
        _cpixn_map = {
            "organism":         "chemical_phenotype_organism",
            "organism_id":      "chemical_phenotype_organism_id",
            "interaction_text": "chemical_phenotype_interaction_text",
            "phenotype_name":   "chemical_phenotype_name",
            "pubmed_ids":       "chem_phenotype_pubmed_ids",
        }
        _present = {o: n for o, n in _cpixn_map.items() if o in _cpixn.columns}
        if _present:
            results["chemical_phenotype_ixn_ctd"] = _cpixn.rename(_present)

    # Table-unique column names — additional per-table renames for columns that
    # collide across non-master tables and are queryable (check_column_uniqueness).
    for _tbl, _rmap in {
        "gene_disease_association_ctd": {
            "direct_evidence": "gene_disease_direct_evidence",
            "pubmed_ids":      "gene_disease_pubmed_ids",
        },
        "chemical_disease_association_ctd": {
            "direct_evidence": "chem_disease_direct_evidence",
            "pubmed_ids":      "chem_disease_pubmed_ids",
        },
        "disease_pathway_association_ctd": {
            # raw `inference_gene` collides with chemical_disease_association's
            # queryable `inference_gene`; rename to the table-unique logical name
            # declared in dbs/ctd/schema.yaml.
            "inference_gene": "disease_pathway_inference_gene",
        },
        "chemical_master_table_ctd": {
            "definition": "chemical_definition",
            "synonyms":   "chemical_synonyms",
        },
        "disease_master_table_ctd": {
            "definition": "disease_definition",
            "synonyms":   "disease_synonyms",
        },
        "gene_master_table_ctd": {
            "synonyms": "gene_synonyms",
        },
        "exposure_studies_ctd": {
            "receptors":       "study_receptors",
            "study_countries": "exposure_study_countries",
            "reference":       "study_reference",
        },
        "exposure_events_ctd": {
            "phenotype_name":  "event_phenotype_name",
            "receptors":       "event_receptors",
            "study_countries": "event_study_countries",
            "reference":       "event_reference",
        },
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    # Add evidence_rank (int) to evidence tables so the SQL can ORDER BY it.
    # Ordering logic stays here (CTD-specific); _finalize.py stays DB-agnostic.
    _EVIDENCE_ORDER = {
        "therapeutic": 0,
        "marker/mechanism": 2,
    }
    for _tbl, _ev_col in {
        "chemical_disease_association_ctd": "chem_disease_direct_evidence",
        "gene_disease_association_ctd":     "gene_disease_direct_evidence",
    }.items():
        _df = results.get(_tbl)
        if _df is not None and _ev_col in _df.columns:
            _n = max(_EVIDENCE_ORDER.values()) + 1
            results[_tbl] = _df.with_columns(
                pl.col(_ev_col)
                .map_elements(
                    lambda v: _EVIDENCE_ORDER.get(str(v).lower(), _n),
                    return_dtype=pl.Int32,
                )
                .fill_null(_n)
                .alias("evidence_rank")
            )

    # Add pubmed_count (study depth) to the evidence tables, mirroring
    # evidence_rank: count the pipe-joined PMIDs. Used as a within-tier sort key
    # (well-studied associations rank above incidental ones). CTD-specific data;
    # the ORDERING decision lives in the per-DB sort_order config, not here.
    for _tbl, _pm_col in {
        "chemical_disease_association_ctd": "chem_disease_pubmed_ids",
        "gene_disease_association_ctd":     "gene_disease_pubmed_ids",
    }.items():
        _df = results.get(_tbl)
        if _df is not None and _pm_col in _df.columns:
            results[_tbl] = _df.with_columns(
                pl.when(pl.col(_pm_col).cast(pl.Utf8).str.len_chars() == 0)
                .then(0)
                .otherwise(pl.col(_pm_col).cast(pl.Utf8).str.count_matches(r"\|") + 1)
                .fill_null(0).cast(pl.Int32).alias("pubmed_count")
            )

    # Safety net: cast any Int64 gene_id to Utf8 so it joins cleanly with
    # tables that store gene_id as String (Issues #1, #12). v2 parquets already
    # store gene_id as String, so this is normally a no-op.
    for tname in list(results.keys()):
        df = results[tname]
        try:
            schema = df.collect_schema() if hasattr(df, "collect_schema") else df.schema
            if "gene_id" in schema.names() and schema["gene_id"] == pl.Int64:
                results[tname] = df.with_columns(pl.col("gene_id").cast(pl.Utf8))
        except Exception as e:
            logger.warning("[%s] gene_id cast failed for '%s': %s", tool, tname, e)

    # ---- strip whitespace + deduplicate ----
    # NOTE: do NOT call df.drop_nulls() without a subset — chemical_master_v2
    # has ~94% nulls in optional columns (pubchem_cid, definition, synonyms),
    # so an unscoped drop_nulls removes Cisplatin and ~178k other rows. The
    # join/filter pipeline downstream handles nulls correctly on its own.
    for name, df in list(results.items()):
        try:
            df = strip_all_whitespace(df).unique()
            results[name] = df
        except Exception as e:
            logger.warning("[%s] Cleaning failed for '%s': %s", tool, name, e)

    # ---- normalise CTD MeSH-prefixed ID columns ----
    # CTD stores ID columns inconsistently: chemical_master.drug_id is
    # "MESH:D002945" while chemical_gene_association.drug_id is "D002945".
    # Same for disease_id: chemical_disease.disease_id is "MESH:D009135"
    # but disease_master.disease_id is "D009135". Without these strips,
    # the planner's joins on those IDs silently lose all rows.
    _PREFIXES = ("MESH:", "REACT:", "KEGG:", "OMIM:")
    _ID_COLUMNS = ("drug_id", "disease_id", "pathway_id", "anatomy_id")
    for tname in list(results.keys()):
        df = results[tname]
        try:
            cols = df.collect_schema().names() if hasattr(df, "collect_schema") else df.columns
        except Exception:
            cols = []
        for col in _ID_COLUMNS:
            if col not in cols:
                continue
            try:
                for prefix in _PREFIXES:
                    escaped = re.escape(prefix)
                    df = df.with_columns(
                        pl.col(col).str.replace_all(f"^{escaped}", "")
                    )
            except Exception as e:
                logger.warning("[%s] %s strip failed for '%s': %s", tool, col, tname, e)
        results[tname] = df

    # ---- cast numeric score/p-value columns stored as String in v2 parquets ----
    _FLOAT_COLS = ("p_value", "corrected_p_value", "inference_score")
    for tname in list(results.keys()):
        df = results[tname]
        try:
            cols = df.collect_schema().names() if hasattr(df, "collect_schema") else df.columns
            for col in _FLOAT_COLS:
                if col in cols:
                    df = df.with_columns(
                        pl.col(col).cast(pl.Float64, strict=False).alias(col)
                    )
            results[tname] = df
        except Exception as e:
            logger.warning("[%s] Float cast failed for '%s': %s", tool, tname, e)

    # ---- load synonym association parquets (written by preprocess_v2.ipynb) ----
    # One row per entity × synonym; enables exact-match synonym lookup without
    # pipe-substring hacks. Missing = preprocess not yet re-run; warn and skip.
    # Physical parquet column is "synonym"; schema.yaml declares table-specific
    # logical names (chemical_synonym / gene_synonym / disease_synonym) to avoid
    # column-uniqueness violations in the Steiner planner. Rename at load time.
    for _assoc_key, _fname, _logical_col in [
        ("chemical_synonyms_association_ctd", "chemical_synonyms_association_ctd_v2.parquet", "chemical_synonym"),
        ("gene_synonyms_association_ctd",     "gene_synonyms_association_ctd_v2.parquet",     "gene_synonym"),
        ("disease_synonyms_association_ctd",  "disease_synonyms_association_ctd_v2.parquet",  "disease_synonym"),
    ]:
        try:
            _df = read_parquet_polars(path="database", database=tool, name=_fname)
            # Rename physical "synonym" → table-specific logical name if present.
            if hasattr(_df, "collect_schema"):
                _cols = _df.collect_schema().names()
            else:
                _cols = list(_df.schema.keys())
            if "synonym" in _cols and _logical_col not in _cols:
                _df = _df.rename({"synonym": _logical_col})
            results[_assoc_key] = _df
            logger.info("[%s] loaded %s", tool, _assoc_key)
        except Exception as e:
            logger.warning("[%s] %s missing — re-run preprocess_v2.ipynb to generate: %s",
                           tool, _fname, e)

    # ---- precompute drug-resolution helpers for the narrow hook ----
    # _drug_name_lower_to_id: {drug_name.lower() → drug_id} from chemical_master.
    # _active_drug_ids: drug_ids with ≥1 row in any CTD association table.
    # Built once at load time so the per-query narrow hook is O(k) not O(N).
    _chem_m = results.get("chemical_master_table_ctd")
    if _chem_m is not None and "drug_name" in _chem_m.columns and "drug_id" in _chem_m.columns:
        _eager_cm = _chem_m.collect() if isinstance(_chem_m, pl.LazyFrame) else _chem_m
        _rows = _eager_cm.select(["drug_name", "drug_id"]).to_dicts()
        results["_drug_name_lower_to_id"] = {
            r["drug_name"].lower(): r["drug_id"]
            for r in _rows if r["drug_name"]
        }
    else:
        results["_drug_name_lower_to_id"] = {}

    _active_ids: set = set()
    for _atbl in (
        "chemical_disease_association_ctd",
        "chemical_gene_association_ctd",
        "chemical_phenotype_ixn_ctd",
        "chemical_pathway_enriched_ctd",
    ):
        _adf = results.get(_atbl)
        if _adf is not None and "drug_id" in _adf.columns:
            _eager_adf = _adf.collect() if isinstance(_adf, pl.LazyFrame) else _adf
            _active_ids.update(_eager_adf["drug_id"].drop_nulls().unique().to_list())
    results["_active_drug_ids"] = _active_ids
    logger.info("[%s] precomputed drug lookup: %d names → %d active drug_ids",
                tool, len(results["_drug_name_lower_to_id"]), len(_active_ids))

    db: dict[str, dict[str, pl.DataFrame]] = {}
    db[tool] = results

    logger.info("[%s] CTD database loaded with %d tables", tool, len(results))
    return db

    