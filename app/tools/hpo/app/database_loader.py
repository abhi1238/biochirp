import logging, polars as pl
logger = logging.getLogger("uvicorn.error")
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

def return_preprocessed_hpo() -> dict[str, pl.DataFrame]:
    results = {}
    results[f"gene_master_table_hpo"] = read_parquet_polars("database", "hpo", f"gene_master_table_hpo_v2.parquet")
    # 2026-06-24: phenotype_master_table_hpo now carries the FULL ontology snapshot
    # (19,389 terms + name + definition/is_a_parents/alt_ids/xrefs). The old lossy
    # 11,609-term subset and the separate phenotype_ontology_master_hpo were merged
    # into this one parquet on disk; the ontology parquets were retired to *.retired.
    results[f"phenotype_master_table_hpo"] = read_parquet_polars("database", "hpo", f"phenotype_master_table_hpo_v2.parquet")
    results[f"disease_master_table_hpo"] = read_parquet_polars("database", "hpo", f"disease_master_table_hpo_v2.parquet")
    results[f"gene_phenotype_association_hpo"] = read_parquet_polars("database", "hpo", f"gene_phenotype_association_hpo_v2.parquet")
    results[f"disease_phenotype_association_hpo"] = read_parquet_polars("database", "hpo", f"disease_phenotype_association_hpo_v2.parquet")
    results[f"phenotype_hierarchy_hpo"] = read_parquet_polars("database", "hpo", f"phenotype_hierarchy_hpo_v2.parquet")
    results[f"phenotype_synonym_hpo"] = read_parquet_polars("database", "hpo", f"phenotype_synonym_hpo_v2.parquet")
    results[f"phenotype_gene_association_hpo"] = read_parquet_polars("database", "hpo", f"phenotype_gene_association_hpo_v2.parquet")
    results[f"gene_disease_association_hpo"] = read_parquet_polars("database", "hpo", f"gene_disease_association_hpo_v2.parquet")
    try:
        results["disease_phenotype_annotation_hpo"] = read_parquet_polars(
            "database", "hpo", "disease_phenotype_annotation_hpo_v2.parquet")
    except Exception as e:
        logger.warning("[hpo] disease_phenotype_annotation_hpo not loaded: %s", e)
    # Precautionary guard: the on-disk parquet is already clean (no header-row
    # leak detected as of the current build), so this filter is a no-op in
    # practice.  It is kept as a defensive safeguard against future re-ingestion
    # that might re-introduce a raw TSV header as a data row.
    if "gene_master_table_hpo" in results:
        # Drop literal "gene_symbol" header-sentinel rows while preserving null-symbol rows
        # (6 uncharacterized loci produce 280 null-gene_symbol rows in gene_phenotype_association_hpo
        # (each locus appears in many phenotype associations) and must be kept).
        # Note: polars evaluates null.is_in([...]) as null, and ~null is null (falsy in
        # .filter()), so the naive ~is_in() filter silently drops null rows. The explicit
        # .is_null() branch restores them.
        results["gene_master_table_hpo"] = (
            results["gene_master_table_hpo"].filter(
                (~pl.col("gene_symbol").is_in(["gene_symbol"])) | pl.col("gene_symbol").is_null()
            )
        )
    # NOTE: The following sentinel/dtype fixes were applied directly to the on-disk
    # parquets (2026-06-16).  The runtime guards below are intentionally removed:
    #   - gene_master_table_hpo.gene_name renamed to gene_symbol on disk
    #   - gene_master_table_hpo / gene_disease_association_hpo / gene_phenotype_association_hpo
    #     gene_symbol '-' -> null on disk
    #   - gene_phenotype_association_hpo frequency '-' -> null on disk
    #   - phenotype_master_table_hpo phenotype_name '' -> null on disk; null phenotype_names
    #     populated from phenotype_ontology_master_hpo (all 1134 gaps filled on disk)
    #   - phenotype_gene_association_hpo ncbi_gene_id cast to Utf8 on disk
    #   - disease_phenotype_annotation_hpo 3 ghost rows (OMIM:111620, OMIM:612271, OMIM:111400)
    #     with null phenotype_id removed from parquet 2026-06-27
    #   - disease_phenotype_annotation_hpo sex column: 2 lowercase sex values remain on disk
    #     for OMIM:139500 ('male', 'female'); these were NOT normalized to uppercase in the
    #     current parquet build. Use case-insensitive match when filtering by sex.
    # [P1] fk_integrity warnings: log orphan disease_ids not present in disease_master_table_hpo.
    # As of 2026-06-24 all orphan disease_ids were cleaned; gene_disease_association_hpo now has
    # 0 orphan disease_ids (inner joins on disease_id are lossless). The logger below is kept
    # as a defensive safeguard against future re-ingestion that might re-introduce orphans.
    # Orphan-disease-id diagnostics (best-effort logging only). read_parquet_polars
    # returns LazyFrames, which are not subscriptable / len()-able — collect the
    # single needed column lazily, and never let this break DB load.
    if "disease_master_table_hpo" in results:
        try:
            def _eager(df):
                return df.collect() if hasattr(df, "collect") else df
            master_ids = set(_eager(results["disease_master_table_hpo"])["disease_id"].to_list())
            for _assoc in ("gene_disease_association_hpo", "gene_phenotype_association_hpo"):
                if _assoc not in results:
                    continue
                _orphans = _eager(results[_assoc]).filter(~pl.col("disease_id").is_in(master_ids))
                if _orphans.height > 0:
                    logger.warning(
                        "[hpo] %s has %d orphan disease_ids (%d unique) not present in "
                        "disease_master_table_hpo — joins on disease_name will drop these rows.",
                        _assoc, _orphans.height, _orphans["disease_id"].n_unique(),
                    )
        except Exception as _e:
            logger.warning("[hpo] orphan-id diagnostic skipped: %s", _e)
    # NOTE: phenotype_ontology_master_hpo sentinel cleanup (definition/alt_ids/xrefs '' -> null)
    # was applied directly to the on-disk parquet (2026-06-16).
    # HP:0000001 root has is_a_parents='' intentionally (no parents); that column is NOT nulled.

    # frequency column already named correctly in v2 parquet (pre-canonicalized):
    # disease_phenotype_annotation_hpo_v2 → disease_phenotype_frequency
    # gene_phenotype_association_hpo_v2   → gene_phenotype_frequency
    # The rename block that previously mapped "frequency" → table-context names has
    # been removed; it was a permanent no-op after the parquets were canonicalized.

    return clean_table_dict("hpo", results)
