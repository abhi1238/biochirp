import logging, polars as pl
logger = logging.getLogger("uvicorn.error")
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

def return_preprocessed_uniprot() -> dict[str, pl.DataFrame]:
    results = {}

    protein = read_parquet_polars("database", "uniprot", "protein_master_table_uniprot_v2.parquet")
    # Canonical parquet already uses protein_id/ensembl_accession/entrez; conditional rename
    # no-ops there (kept for backward-compat with pre-canonicalization parquets).
    protein = protein.rename({k: v for k, v in {"accession": "protein_id", "ensembl_id": "ensembl_accession", "entrez_id": "entrez", "hgnc_id": "hgnc"}.items() if k in protein.columns})
    # Strip version suffix and isoform bracket from ensembl_accession (e.g. 'ENSG00000146648.22. [P00533-1]' -> 'ENSG00000146648')
    protein = protein.with_columns(
        pl.col("ensembl_accession").str.extract(r"(ENSG\d{11})", 0).alias("ensembl_accession")
    )
    results["protein_master_table_uniprot"] = protein

    xwalk = read_parquet_polars("database", "uniprot", "gene_protein_xwalk_uniprot_v2.parquet")
    xwalk = xwalk.rename({k: v for k, v in {"accession": "protein_id", "ensembl_id": "ensembl_accession", "entrez_id": "entrez"}.items() if k in xwalk.columns})
    # Strip version suffix and isoform bracket from ensembl_accession (same format as protein_master_table)
    xwalk = xwalk.with_columns(
        pl.col("ensembl_accession").str.extract(r"(ENSG\d{11})", 0).alias("ensembl_accession")
    )
    # Drop the redundant HGNC xref here: it is canonically served by protein_master_table.hgnc
    # and id_mapping_uniprot (db='HGNC'). Keeping it would re-create the phantom hgnc join edge
    # (non-queryable shared column) and a queryable-name collision with protein_master.hgnc.
    if "hgnc_id" in xwalk.collect_schema().names():
        xwalk = xwalk.drop("hgnc_id")
    results["gene_protein_association_uniprot"] = xwalk

    # ── Phase-2 complementary tables (optional; load if present) ──────────
    _phase2 = {
        "variant_disease_uniprot":     "variant_disease_uniprot_v2.parquet",
        "keyword_master_uniprot":      "keyword_master_uniprot_v2.parquet",
        "subcell_location_uniprot":    "subcell_location_uniprot_v2.parquet",
        "species_master_uniprot":      "species_master_uniprot_v2.parquet",
        "gene_ontology_uniprot":       "gene_ontology_uniprot_v2.parquet",
        "id_mapping_uniprot":          "id_mapping_uniprot_v2.parquet",
        "protein_keyword_uniprot":     "protein_keyword_uniprot_v2.parquet",
        "protein_subcell_uniprot":     "protein_subcell_uniprot_v2.parquet",
        "protein_function_uniprot":    "protein_function_uniprot_v2.parquet",
        "ptm_sites_uniprot":           "ptm_sites_uniprot_v2.parquet",
        "protein_interaction_uniprot": "protein_interaction_uniprot_v2.parquet",
    }
    for key, fname in _phase2.items():
        try:
            df = read_parquet_polars("database", "uniprot", fname)
            # accession -> protein_id for join-consistency with protein master
            if "accession" in df.columns:
                df = df.rename({"accession": "protein_id"})
            # subcell_location: parquet column `keyword` holds KW-NNNN keyword IDs;
            # rename to keyword_id so it joins by-name to keyword_master_uniprot.keyword_id (FK).
            if key == "subcell_location_uniprot" and "keyword" in df.columns:
                df = df.rename({"keyword": "keyword_id"})
            # Safety net: cast Null-typed disease_id to Utf8 (all-null column, dtype non-conformant)
            # NOTE: read_parquet_polars returns a LazyFrame, which is not subscriptable; use the
            # lazy schema instead of df["disease_id"] to read the dtype.
            if key == "variant_disease_uniprot" and "disease_id" in df.columns:
                _vd_schema = df.collect_schema() if hasattr(df, "collect_schema") else df.schema
                if _vd_schema["disease_id"] == pl.Null:
                    df = df.with_columns(pl.col("disease_id").cast(pl.Utf8))
            # Warn only if orphan protein_ids are actually present (parquet may already be clean)
            if key == "gene_ontology_uniprot":
                # Patch stale HGNC aliases in gene_symbol: 267 rows across 13 accessions carry
                # outdated symbols (e.g. MIMS1->FAM210A, GPHRA->GPR89A, H2BC10->H2BC4).
                # Join on protein_id to protein_master and replace with canonical gene_symbol.
                if "protein_id" in df.columns and "gene_symbol" in df.columns:
                    canonical = protein.select(["protein_id", "gene_symbol"])
                    df = df.join(canonical, on="protein_id", how="left", suffix="_canonical")
                    df = df.with_columns(
                        pl.coalesce(["gene_symbol_canonical", "gene_symbol"]).alias("gene_symbol")
                    ).drop("gene_symbol_canonical")
                # Check for orphan protein_ids (TrEMBL accessions absent from protein_master_table)
                if "protein_id" in df.columns:
                    master_ids = set(protein.select("protein_id").collect()["protein_id"].to_list())
                    _go_ids = df.select("protein_id").collect()["protein_id"].to_list()
                    _orphan_count = sum(1 for pid in _go_ids if pid not in master_ids)
                    if _orphan_count > 0:
                        logger.warning(
                            "[uniprot] gene_ontology: %d rows with accessions absent from "
                            "protein_master_table will be dropped on join", _orphan_count
                        )
            results[key] = df
        except Exception as e:
            logger.warning("[uniprot] phase-2 table %s missing: %s", fname, e)

    # Schema invariant: table-unique column names (no content column shared across
    # non-master tables). gene_symbol is the queryable column in protein_master_table;
    # the two association tables carry denormalised copies (exec_schema: false) that
    # must be renamed so they don't collide in the in-memory DataFrame namespace.
    # gene_ontology rename must come AFTER the canonical alias patch above (lines ~54-62).
    for _tbl, _rmap in {
        "gene_protein_association_uniprot": {"gene_symbol": "gene_protein_gene_symbol"},
        "gene_ontology_uniprot":            {"gene_symbol": "ontology_gene_symbol"},
        "keyword_master_uniprot": {
            "description": "keyword_description",
            "synonyms":    "keyword_synonyms",
            "entry_type":  "keyword_entry_type",
            "hierarchy":   "keyword_hierarchy",
        },
        "subcell_location_uniprot": {
            "description": "subcell_description",
            "synonyms":    "subcell_synonyms",
            "hierarchy":   "subcell_hierarchy",
            "entry_type":  "subcell_entry_type",
        },
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    return clean_table_dict("uniprot", results)
