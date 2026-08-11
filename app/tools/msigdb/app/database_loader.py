import polars as pl
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

def return_preprocessed_msigdb() -> dict[str, pl.DataFrame]:
    results = {}
    results["geneset_master_table_msigdb"] = read_parquet_polars("database", "msigdb", "geneset_master_table_msigdb_v2.parquet")
    gene_geneset = read_parquet_polars("database", "msigdb", "gene_geneset_association_msigdb_v2.parquet")
    gene_master = read_parquet_polars("database", "msigdb", "gene_master_table_msigdb_v2.parquet")
    # Fully-normalized FK-only design (2026-06-24): gene_geneset_association carries
    # ONLY [geneset_id, gene_id] — no denormalized gene_symbol/collection/organism
    # mirrors (those caused Steiner join-explosion risk; gene_symbol joins from
    # gene_master, collection/organism from geneset_master). gene_id is a UNIQUE
    # composite PK "<gene_symbol>|<organism>": the same symbol appears under more
    # than one organism (17,458 symbols), so a bare-symbol key would fan the
    # assoc⋈gene_master join out across species. Both parquets now store the
    # composite gene_id directly, so the join is strictly 1:1. The fallback below
    # only rebuilds gene_id for an older bare-symbol gene_master parquet.
    if "gene_id" not in gene_master.columns and {"gene_symbol", "organism"}.issubset(gene_master.columns):
        gene_master = gene_master.with_columns(
            pl.concat_str([pl.col("gene_symbol"), pl.col("organism")], separator="|").alias("gene_id")
        )
    results["gene_geneset_association_msigdb"] = gene_geneset
    results["gene_master_table_msigdb"] = gene_master
    results["geneset_metadata_msigdb"] = read_parquet_polars("database", "msigdb", "geneset_metadata_msigdb_v2.parquet")

    # Table-unique column names: `organism` appears in both geneset_master_table and gene_master_table.
    # Rename to table-prefixed names so schema_kg FK generator doesn't create a spurious cross-table edge.
    for _tbl, _rmap in {
        "geneset_master_table_msigdb": {"organism": "geneset_organism"},
        "gene_master_table_msigdb":    {"organism": "gene_organism"},
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    return clean_table_dict("msigdb", results)
