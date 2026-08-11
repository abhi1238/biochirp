import logging, polars as pl
logger = logging.getLogger("uvicorn.error")
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

# NOTE: Orphanet is now a _v2 database. It is listed in _V2_DATABASES in
# utils/dataframe_loader.py, so although the calls below pass the non-_v2 names
# (e.g. disease_master_table_orphanet.parquet), read_parquet_polars transparently
# repoints each load to its `_v2` sibling on disk (e.g. ..._orphanet_v2.parquet)
# whenever that sibling exists. The bare names are kept here only as the logical
# table identifiers; the actual bytes served come from the _v2 parquets.
#
# COLUMN-NAME SSOT: preprocess_v2.ipynb (Cell 31 "SSOT 2B canonicalization") writes
# ALL column renames into the parquets before they are committed. The loader is a
# DUMB READER — it must NOT duplicate those renames. The only transforms that belong
# here are post-load concerns that the notebook cannot own:
#   • cross-table collision dedup (disease_name / association_type ambiguity across tables)
#   • FK-supplement rows injected from another table at load time

def return_preprocessed_orphanet() -> dict[str, pl.DataFrame]:
    results = {}
    results["disease_master_table_orphanet"] = read_parquet_polars("database", "orphanet", "disease_master_table_orphanet.parquet")

    # gene_master and gene_disease: columns are fully canonicalized by preprocess_v2.ipynb
    # (ensembl_accession, entrez, gene_symbol, gene_id all present on disk). No renames needed.
    results["gene_master_table_orphanet"] = read_parquet_polars("database", "orphanet", "gene_master_table_orphanet.parquet")
    results["gene_disease_association_orphanet"] = read_parquet_polars("database", "orphanet", "gene_disease_association_orphanet.parquet")

    # disease_phenotype: phenotype_id is canonicalized by preprocess_v2.ipynb. No rename needed.
    results["disease_phenotype_association_orphanet"] = read_parquet_polars("database", "orphanet", "disease_phenotype_association_orphanet.parquet")

    # === Phase 2 coverage: xrefs / epidemiology / onset / natural history / classification ===
    for parquet_name in (
        "disease_xref_orphanet.parquet",
        "disease_epidemiology_orphanet.parquet",
        "disease_onset_inheritance_orphanet.parquet",
        "disease_natural_history_orphanet.parquet",
        "disease_classification_orphanet.parquet",
    ):
        try:
            df = read_parquet_polars("database", "orphanet", parquet_name)
            results[parquet_name.replace(".parquet", "")] = df
        except Exception as e:
            logger.warning("[orphanet] Phase-2 parquet '%s' missing/unreadable: %s", parquet_name, e)

    # === FK integrity: supplement disease_xref with rows missing for new Orphanet additions ===
    # Permanently active supplement: ORPHA:708014 and ORPHA:714413 appear in disease_master
    # (via product6 gene-disease XML) but are absent from product1 xref XML — they will never
    # appear in the raw parquet regardless of re-runs.
    # Without these rows, joins from gene_disease -> disease_xref return NULL disease_name.
    # The injected disease_name column is declared in dbs/orphanet/schema.yaml
    # (added_at_load: true, exec_schema: false).
    _MISSING_XREF_IDS = ["ORPHA:708014", "ORPHA:714413"]
    disease_master = results.get("disease_master_table_orphanet")
    disease_xref = results.get("disease_xref_orphanet")
    if disease_master is not None and disease_xref is not None:
        try:
            supplement_missing = (
                disease_master
                .filter(pl.col("disease_id").is_in(_MISSING_XREF_IDS))
                .select(["disease_id", "disease_name"])
                .with_columns([
                    pl.lit("ORPHANET_SUPPLEMENT").alias("xref_source"),
                    pl.lit(None).cast(pl.Utf8).alias("xref_id"),
                    pl.lit(None).cast(pl.Utf8).alias("mapping_relation"),
                    pl.lit("Validated").alias("disease_xref_validation_status"),
                ])
            )
            # disease_master is a LazyFrame from read_parquet_polars, so
            # supplement_missing is lazy too; .height is eager-only. Collect
            # both frames so the count + concat operate on eager DataFrames
            # (pl.concat cannot mix lazy and eager frames).
            if hasattr(supplement_missing, "collect"):
                supplement_missing = supplement_missing.collect()
            if hasattr(disease_xref, "collect"):
                disease_xref = disease_xref.collect()
            if supplement_missing.height > 0:
                disease_xref = pl.concat([disease_xref, supplement_missing], how="diagonal")
                results["disease_xref_orphanet"] = disease_xref
                logger.info(
                    "[orphanet] Appended %d FK-supplement row(s) to disease_xref_orphanet for: %s",
                    supplement_missing.height,
                    _MISSING_XREF_IDS,
                )
        except Exception as e:
            logger.warning("[orphanet] FK supplement for disease_xref failed: %s", e)

    # Schema invariant: table-unique queryable names (scripts/check_column_uniqueness.py).
    # `disease_name` collides across disease_natural_history and disease_classification;
    # give each a table-context name. These renames are NOT in preprocess_v2.ipynb because
    # they are cross-table concerns (the notebook writes each table independently) — they
    # legitimately belong here.
    for _tbl, _rmap in {
        "disease_natural_history_orphanet": {"disease_name": "natural_history_disease_name"},
        "disease_classification_orphanet": {"disease_name": "classification_disease_name"},
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    return clean_table_dict("orphanet", results)
