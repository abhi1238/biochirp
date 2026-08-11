import polars as pl
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

def return_preprocessed_reactome() -> dict[str, pl.DataFrame]:
    results = {}
    results[f"pathway_master_table_reactome"] = read_parquet_polars("database", "reactome", f"pathway_master_table_reactome_v2.parquet")
    results[f"gene_master_table_reactome"] = read_parquet_polars("database", "reactome", f"gene_master_table_reactome_v2.parquet")
    results[f"gene_pathway_association_reactome"] = read_parquet_polars("database", "reactome", f"gene_pathway_association_reactome_v2.parquet")
    results["pathway_hierarchy_reactome"] = read_parquet_polars("database", "reactome", "pathway_hierarchy_reactome_v2.parquet")
    results["uniprot_pathway_reactome"] = read_parquet_polars("database", "reactome", "uniprot_pathway_reactome_v2.parquet")
    results["chebi_pathway_reactome"] = read_parquet_polars("database", "reactome", "chebi_pathway_reactome_v2.parquet")
    results["ensembl_pathway_reactome"] = read_parquet_polars("database", "reactome", "ensembl_pathway_reactome_v2.parquet")
    _ncbi = read_parquet_polars("database", "reactome", "ncbi_pathway_reactome_v2.parquet")
    # No-op safety backstop: the `^\d+$` filter that removes non-Entrez accessions
    # (RefSeq/GenBank/SARS) is applied at PREPROCESS time (notebook cell 14). The served
    # parquet is already all-numeric (156,146 rows, 0 non-numeric verified), so this drops 0 rows.
    _ncbi = _ncbi.filter(pl.col("ncbi_gene_id").str.contains(r"^\d+$"))
    results["ncbi_pathway_reactome"] = _ncbi

    # Schema invariant: table-unique queryable names (scripts/check_column_uniqueness.py).
    # The v2 parquets are now written with disambiguated evidence column names
    # (e.g. chebi_pathway_evidence) directly, so this rename is a no-op backstop
    # that only rescues a legacy parquet still carrying the bare `evidence` column.
    for _tbl, _rmap in {
        "chebi_pathway_reactome":          {"evidence": "chebi_pathway_evidence"},
        "ensembl_pathway_reactome":        {"evidence": "ensembl_pathway_evidence"},
        "gene_pathway_association_reactome": {"evidence": "gene_pathway_evidence"},
        "ncbi_pathway_reactome":           {"evidence": "ncbi_pathway_evidence"},
        "uniprot_pathway_reactome":        {"evidence": "uniprot_pathway_evidence"},
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    # Hierarchy denormalization backstop. pathway_hierarchy carries parent_id /
    # child_id, both self-referential FKs to pathway_master_table.pathway_id.
    # The planner cannot self-join a table to one master twice (name mismatch +
    # Polars pathway_name collision), so human-readable parent_pathway_name / child_pathway_name
    # are denormalized into the parquet at preprocess time. This guarded block is
    # a no-op when the parquet already carries them; it only rescues a regenerated
    # parquet still holding bare parent_id/child_id (mirrors the evidence-rename
    # backstop above).
    _hier = results.get("pathway_hierarchy_reactome")
    _master = results.get("pathway_master_table_reactome")
    if _hier is not None and _master is not None:
        _hcols = _hier.collect_schema().names() if hasattr(_hier, "collect_schema") else _hier.columns
        if "parent_pathway_name" not in _hcols or "child_pathway_name" not in _hcols:
            _m = _master.select(["pathway_id", "pathway_name"])
            _hier = (
                _hier
                .join(_m.rename({"pathway_id": "parent_id", "pathway_name": "parent_pathway_name"}),
                      on="parent_id", how="left")
                .join(_m.rename({"pathway_id": "child_id", "pathway_name": "child_pathway_name"}),
                      on="child_id", how="left")
            )
            results["pathway_hierarchy_reactome"] = _hier

    return clean_table_dict("reactome", results)
