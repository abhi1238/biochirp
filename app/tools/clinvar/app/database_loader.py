import logging, polars as pl
logger = logging.getLogger("uvicorn.error")
from utils.dataframe_loader import read_parquet_polars, clean_table_dict

def return_preprocessed_clinvar() -> dict[str, pl.DataFrame]:
    results = {}
    for name in ["variant_master_table", "gene_master_table",
                  "variant_gene_association", "variant_disease_association",
                  "variant_submission", "variant_citation",
                  "variant_genomic_coords", "disease_master_table"]:
        results[f"{name}_clinvar"] = read_parquet_polars("database", "clinvar", f"{name}_clinvar_v2.parquet")

    # Fix P1: variant_citation — rsid='0' was an ETL sentinel for missing rsID.
    # Currently a no-op: already cleaned in the parquet at ETL time (0 rows affected on disk).
    # Kept as a safety net in case an older parquet snapshot is loaded.
    try:
        vc = results["variant_citation_clinvar"]
        if "rsid" in vc.columns:
            results["variant_citation_clinvar"] = vc.with_columns(
                pl.when(pl.col("rsid") == "0").then(None).otherwise(pl.col("rsid")).alias("rsid")
            )
    except Exception as e:
        logger.warning("clinvar: could not null rsid='0' sentinel: %s", e)

    # Fix P2: variant_submission — description='0' was an ETL sentinel; null it out.
    # Currently a no-op: already cleaned in the parquet at ETL time (0 rows affected on disk).
    # Kept as a safety net in case an older parquet snapshot is loaded.
    try:
        vs = results["variant_submission_clinvar"]
        if "description" in vs.columns:
            results["variant_submission_clinvar"] = vs.with_columns(
                pl.when(pl.col("description") == "0").then(None).otherwise(pl.col("description")).alias("description")
            ).rename({
                "clinical_significance": "submission_clinical_significance",
                "review_status": "submission_review_status",
            })
    except Exception as e:
        logger.warning("clinvar: could not null description='0' sentinel: %s", e)

    # Fix P2: gene_master — sentinel row gene_id='NCBIGene:-1' was present in earlier ETL
    # snapshots with a spurious compound gene_symbol ('ATXN8;ATXN8OS;LOC109461478').
    # Currently a no-op: already absent in the parquet (0 rows affected on disk).
    # Kept as a safety net in case an older parquet snapshot is loaded.
    try:
        gm = results["gene_master_table_clinvar"]
        if "gene_id" in gm.columns and "gene_symbol" in gm.columns:
            results["gene_master_table_clinvar"] = gm.with_columns(
                pl.when(pl.col("gene_id") == "NCBIGene:-1")
                .then(pl.lit("-"))
                .otherwise(pl.col("gene_symbol"))
                .alias("gene_symbol")
            )
    except Exception as e:
        logger.warning("clinvar: could not fix gene_master sentinel gene_symbol: %s", e)

    # Fix P2: variant_genomic_coords — pos is already stored as String (Utf8) on disk.
    # The cast below is a safety net in case an older parquet snapshot has pos as Int64;
    # it is currently a no-op on the v2 parquet. Callers needing integer range queries
    # should cast explicitly: pl.col('pos').cast(pl.Int64).
    try:
        gc = results["variant_genomic_coords_clinvar"]
        # read_parquet_polars returns a LazyFrame; gc["pos"].dtype is eager-only.
        # Read the dtype from the lazy schema instead.
        _gc_schema = gc.collect_schema() if hasattr(gc, "collect_schema") else gc.schema
        if "pos" in _gc_schema and _gc_schema["pos"] != pl.Utf8:
            results["variant_genomic_coords_clinvar"] = gc.with_columns(
                pl.col("pos").cast(pl.Utf8)
            )
    except Exception as e:
        logger.warning("clinvar: could not cast variant_genomic_coords pos to Utf8: %s", e)

    # Fix P3: variant_master_table — null '-' sentinel in review_status (243,604 rows)
    # and null '-', 'not provided', 'not specified' in clinical_significance.
    # Then rename to table-scoped names (aggregate_*) to avoid mapper ambiguity.
    try:
        vm = results['variant_master_table_clinvar']
        results['variant_master_table_clinvar'] = vm.with_columns([
            pl.when(pl.col('review_status') == '-').then(None).otherwise(pl.col('review_status')).alias('review_status'),
            pl.when(pl.col('clinical_significance').is_in(['-', 'not provided', 'not specified'])).then(None).otherwise(pl.col('clinical_significance')).alias('clinical_significance')
        ]).rename({
            'review_status': 'aggregate_review_status',
            'clinical_significance': 'aggregate_clinical_significance',
            'gene_symbol': 'variant_gene_symbol',
        })
    except Exception as e:
        logger.warning("clinvar: could not null sentinel values in variant_master_table: %s", e)

    # Fix P4: variant_disease_association — null 'not provided', 'not specified', '-', ''
    # in disease_name (1.4M+ rows). Rename clinical_significance to disease-scoped name.
    try:
        vda = results['variant_disease_association_clinvar']
        results['variant_disease_association_clinvar'] = vda.with_columns(
            pl.when(pl.col('disease_name').is_in(['-', 'not provided', 'not specified', ''])).then(None).otherwise(pl.col('disease_name')).alias('disease_name')
        ).rename({
            'clinical_significance': 'disease_clinical_significance',
            'disease_name': 'variant_disease_name',
            'phenotype_ids': 'variant_phenotype_ids',
        })
    except Exception as e:
        logger.warning("clinvar: could not null sentinel values in variant_disease_association: %s", e)

    # Fix P5 (2026-07-05): variant_gene_association — variants that overlap
    # MULTIPLE gene loci (the denormalized variant_master_table_clinvar.variant_gene_symbol
    # column has >1 distinct value across the rows for the same variant_id — one
    # row per overlapping gene) are ENTIRELY ABSENT from variant_gene_association
    # on disk: confirmed 8,561/8,561 (100%) of such multi-gene-locus variant_ids
    # have ZERO rows in the association table. This silently drops the variant
    # from every gene-anchored ClinVar query for ALL of its overlapping genes,
    # regardless of any disease-name/clinical-significance filter — e.g. the
    # classic HbS sickle-cell mutation CLINVAR:15333 (NM_000518.5(HBB):c.20A>T,
    # p.Glu7Val, pathogenic, overlaps HBB + 2 LOC loci) never appears in "pathogenic
    # variants in HBB" results because gene_master→variant_gene_association→
    # variant_master has no row to join through, even though the variant and its
    # disease associations both exist on disk. This is a preprocessing/ETL gap,
    # not a query-filter bug — backfill the missing (variant_id, gene_id) pairs
    # here by deriving them from the denormalized variant_gene_symbol column via
    # a gene_symbol lookup against gene_master_table_clinvar. Idempotent: becomes
    # a no-op once the upstream ETL notebook is fixed to emit these rows directly.
    try:
        _vm_for_backfill = results['variant_master_table_clinvar']
        _vg = results['variant_gene_association_clinvar']
        _gm = results['gene_master_table_clinvar']
        _existing_vids = _vg.select('variant_id').unique()
        _backfill = (
            _vm_for_backfill.select(['variant_id', 'variant_gene_symbol'])
            .filter(pl.col('variant_gene_symbol').is_not_null())
            .unique()
            .join(_existing_vids, on='variant_id', how='anti')
            .join(
                _gm.select(['gene_id', 'gene_symbol']).rename({'gene_symbol': 'variant_gene_symbol'}),
                on='variant_gene_symbol',
                how='inner',
            )
            .select(['variant_id', 'gene_id'])
            .unique()
        )
        results['variant_gene_association_clinvar'] = pl.concat(
            [_vg, _backfill], how='vertical'
        ).unique()
    except Exception as e:
        logger.warning("clinvar: could not backfill missing variant_gene_association rows: %s", e)

    # gene_master_table_clinvar.parquet stores gene_symbol as 'gene_symbol'
    # on disk (renamed from 'gene_name' at ETL time in preprocess.py).
    # No runtime rename needed.

    # Schema invariant: table-unique column names (no content column shared across
    # non-master tables). allele_id appears in two satellite tables → rename each
    # to a table-scoped name so schema_kg FK generator doesn't create a spurious
    # cross-table join edge between them.
    for _tbl, _rmap in {
        "variant_citation_clinvar":       {"allele_id": "citation_allele_id"},
        "variant_genomic_coords_clinvar": {"allele_id": "genomic_allele_id"},
    }.items():
        _df = results.get(_tbl)
        if _df is not None:
            _present = {o: n for o, n in _rmap.items() if o in _df.columns}
            if _present:
                results[_tbl] = _df.rename(_present)

    return clean_table_dict("clinvar", results)
