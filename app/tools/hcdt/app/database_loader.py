import os
import polars as pl
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")

from utils.dataframe_loader import read_parquet_polars, clean_table_dict


def return_preprocessed_hcdt() -> dict[str, pl.DataFrame]:


    tool = "hcdt"

    results = dict()

    # HCDT data v2 (Apr 2026 refresh from http://hainmu-biobigdata.com/hcdt/) is
    # available as <table>_v2.parquet alongside the legacy tables. Toggle with
    # HCDT_DATA_VERSION=v2 (default) or =v1 to revert.
    v = os.getenv("HCDT_DATA_VERSION", "v2").lower()
    sfx = "_v2.parquet" if v == "v2" else ".parquet"

    drug_master_table       = read_parquet_polars(path="database", database=tool, name=f"drug_master_table{sfx}")
    drug_gene_association   = read_parquet_polars(path="database", database=tool, name=f"drug_gene_association{sfx}")
    # Derive source-quality columns from the raw datasource string so SQL can
    # ORDER BY them.  source_count and ttd_confirmed are factual attributes of
    # the data (not ranking magic); the SQL generator is instructed via
    # db_llm_rules.yaml col_selection to include ORDER BY ttd_confirmed DESC,
    # source_count DESC on drug_gene_association queries.
    if "datasource" in drug_gene_association.collect_schema().names():
        drug_gene_association = drug_gene_association.with_columns([
            pl.col("datasource").str.split(", ").list.len().alias("source_count"),
            pl.col("datasource").str.contains("TTD").cast(pl.Int32).alias("ttd_confirmed"),
        ])
    drug_disease_association= read_parquet_polars(path="database", database=tool, name=f"drug_disease_association{sfx}")
    drug_pathway_association= read_parquet_polars(path="database", database=tool, name=f"drug_pathway_association{sfx}")
    disease_master_table    = read_parquet_polars(path="database", database=tool, name=f"disease_master_table{sfx}")
    # Rename disk column omim_id → logical name omim_xref (matches dbs/hcdt/schema.yaml parquet_name alias).
    _omim_rename = {o: n for o, n in {"omim_id": "omim_xref"}.items()
                    if o in disease_master_table.collect_schema().names()}
    if _omim_rename:
        disease_master_table = disease_master_table.rename(_omim_rename)
    # Drop mesh_id from disease_master — exec_schema:false in schema.yaml so absent from
    # config.schema, but the parquet column survives the load.  If kept, the Steiner planner
    # detects DDA.mesh_id ↔ disease_master.mesh_id as a shared column and generates a JOIN
    # on mesh_id — which then crashes at execute time because mesh_id is not in the exec
    # schema (config.schema disease_master_table = [disease_id, disease_name, icd11, omim_xref]).
    _dm_exec_cols = {"disease_id", "disease_name", "icd11", "omim_xref"}
    _dm_extra = [c for c in disease_master_table.collect_schema().names() if c not in _dm_exec_cols]
    if _dm_extra:
        disease_master_table = disease_master_table.drop(_dm_extra)
        logger.info("[%s] disease_master_table: dropped non-exec cols %s", tool, _dm_extra)
    gene_master_table       = read_parquet_polars(path="database", database=tool, name=f"gene_master_table{sfx}")

    # DDA uses source-native drug IDs (TTD sequential IDs, PubChem CIDs from CTD/KEGG)
    # that differ from drug_master's PubChem CID space — 42% of DDA drug_ids are orphans.
    # Fix: extend drug_master with placeholder entries for orphan DDA drugs so the
    # FK join drug_master → DDA → disease_master works for all DDA drugs.
    _dm_schema = drug_master_table.collect_schema().names()
    _dda_schema = drug_disease_association.collect_schema().names()
    if "drug_id" in _dm_schema and "drug_id" in _dda_schema and "drug_name" in _dda_schema:
        _dm_ids_df = drug_master_table.select("drug_id").collect()
        _dm_ids = set(_dm_ids_df["drug_id"].to_list())
        _dda_orphan = (
            drug_disease_association
            .select(["drug_id", "drug_name"])
            .unique("drug_id")
            .filter(~pl.col("drug_id").is_in(_dm_ids))
            .collect()
        )
        if _dda_orphan.shape[0] > 0:
            drug_master_table = pl.concat(
                [drug_master_table.collect(), _dda_orphan], how="diagonal"
            ).lazy()
            logger.info(
                "[%s] extended drug_master with %d orphan DDA drug entries (FK fix)",
                tool, _dda_orphan.shape[0],
            )
    # Expose the 4 xref ids as non-_id logical outputs (uniprot/hgnc/ensembl/entrez) so they stay
    # exec-visible without tripping the single-pk-per-master rule (C1). Matches dbs/hcdt/schema.yaml
    # parquet_name aliases. Conditional so a re-loaded already-renamed frame is a no-op.
    _xref_rename = {o: n for o, n in {"uniprot_id": "uniprot", "hgnc_id": "hgnc",
                                      "ensembl_id": "ensembl", "entrez_id": "entrez"}.items()
                    if o in gene_master_table.collect_schema().names()}
    if _xref_rename:
        gene_master_table = gene_master_table.rename(_xref_rename)
    pathway_master_table    = read_parquet_polars(path="database", database=tool, name=f"pathway_master_table{sfx}")

    # Strip exec_schema:false denormalized columns from association tables.
    # These mirror master-table columns; keeping them lets the Steiner planner
    # detect false FK edges (e.g. DDA.mesh_id → disease_master.mesh_id or
    # DPA.pathway_name → pathway_master.pathway_name) that generate wrong/crashing
    # joins.  Exec schemas are [drug_id, disease_id] and [drug_id, pathway_id].
    _dda_keep = {"drug_id", "disease_id"}
    _dda_extra = [c for c in drug_disease_association.collect_schema().names() if c not in _dda_keep]
    if _dda_extra:
        drug_disease_association = drug_disease_association.drop(_dda_extra)
        logger.info("[%s] drug_disease_association: dropped non-exec cols %s", tool, _dda_extra)

    _dpa_keep = {"drug_id", "pathway_id"}
    _dpa_extra = [c for c in drug_pathway_association.collect_schema().names() if c not in _dpa_keep]
    if _dpa_extra:
        drug_pathway_association = drug_pathway_association.drop(_dpa_extra)
        logger.info("[%s] drug_pathway_association: dropped non-exec cols %s", tool, _dpa_extra)

    results["drug_master_table_hcdt"]        = drug_master_table
    results["drug_gene_association_hcdt"]    = drug_gene_association
    results["drug_disease_association_hcdt"] = drug_disease_association
    results["drug_pathway_association_hcdt"] = drug_pathway_association
    results["disease_master_table_hcdt"]     = disease_master_table
    results["gene_master_table_hcdt"]        = gene_master_table
    results["pathway_master_table_hcdt"]     = pathway_master_table

    # Round-4 (2026-05-14): pathway_master_table.{reactome_id, kegg_hsa_id,
    # smpdb_id, chebi_id, kegg_id} cross-refs are dropped from the master table
    # (single-`_id`-per-master invariant). Recover them as a long-form
    # pathway_xref_hcdt table so the planner can resolve external pathway IDs.
    # v2 pathway_master_table uses _xref suffix; reactome ID became pathway_id itself.
    _xref_sources = {
        "kegg_hsa_xref": "kegg_hsa",
        "smpdb_xref":    "smpdb",
        "chebi_xref":    "chebi",
        "kegg_xref":     "kegg",
    }
    _present = [c for c in _xref_sources if c in pathway_master_table.columns]
    if _present and "pathway_id" in pathway_master_table.columns:
        try:
            xref_df = (
                pathway_master_table
                .select(["pathway_id", *_present])
                .unpivot(index="pathway_id", on=_present,
                         variable_name="xref_source", value_name="xref_id")
                .filter(pl.col("xref_id").is_not_null() & (pl.col("xref_id") != "") & (pl.col("xref_id") != "NA"))
                .with_columns(pl.col("xref_source").replace(_xref_sources))
                .unique()
            )
            results["pathway_xref_hcdt"] = xref_df
        except Exception as e:
            logger.warning(f"[{tool}] pathway_xref build failed: {e}")

    # v1 had pathway_gene_association; for v2 we restore it from the GENEIDS
    # column of Pathway.xlsx (preprocess_v2.build_pathway_gene). Fall back to
    # the v1 file when only the legacy version is present on disk.
    try:
        results["pathway_gene_association_hcdt"] = read_parquet_polars(
            path="database", database=tool,
            name=f"pathway_gene_association{sfx}")
    except Exception as e:
        logger.warning(
            f"[{tool}] pathway_gene_association{sfx} missing ({e}); "
            f"falling back to v1 pathway_gene_association.parquet")
        try:
            results["pathway_gene_association_hcdt"] = read_parquet_polars(
                path="database", database=tool,
                name="pathway_gene_association.parquet")
        except Exception as e2:
            logger.warning(f"[{tool}] no pathway_gene_association available: {e2}")

    # v2 NEW tables (drug↔RNA, negative DTIs with Ki/IC50/Kd)
    if v == "v2":
        for new_name in ("rna_master_table_v2", "drug_rna_association_v2",
                         "drug_target_negative_v2"):
            try:
                results[new_name.replace("_v2","") + "_hcdt"] = read_parquet_polars(
                    path="database", database=tool, name=f"{new_name}.parquet")
            except Exception as e:
                logger.warning(f"[{tool}] optional v2 table {new_name} missing: {e}")
        # drug_rna_association: dedup duplicate (drug_id, rna_id) pairs and
        # enforce FK integrity against drug_master_table so orphan drug_ids
        # (absent from the master) do not corrupt join results.
        if "drug_rna_association_hcdt" in results:
            _dra = results["drug_rna_association_hcdt"]
            _dra_schema = _dra.collect_schema().names()
            if "drug_id" in _dra_schema and "rna_id" in _dra_schema:
                _dra = _dra.unique(subset=["drug_id", "rna_id"], keep="first")
            if "drug_id" in _dra_schema and "drug_id" in drug_master_table.collect_schema().names():
                _dm_ids_set = set(drug_master_table.select("drug_id").collect()["drug_id"].to_list())
                _before = _dra.select(pl.len()).collect().item()
                _dra = _dra.filter(pl.col("drug_id").is_in(_dm_ids_set))
                _after = _dra.select(pl.len()).collect().item()
                if _before != _after:
                    logger.info(
                        "[%s] drug_rna_association FK filter: %d → %d rows (%d orphan drug_ids dropped)",
                        tool, _before, _after, _before - _after,
                    )
            results["drug_rna_association_hcdt"] = _dra
        # Rename disk uppercase-M affinity columns → logical lowercase names
        # (ki_nM/ic50_nM/kd_nM on disk → ki_nm/ic50_nm/kd_nm in schema.yaml).
        if "drug_target_negative_hcdt" in results:
            _dtn = results["drug_target_negative_hcdt"]
            _dtn_rename = {o: n for o, n in {
                "ki_nM": "ki_nm", "ic50_nM": "ic50_nm", "kd_nM": "kd_nm"
            }.items() if o in _dtn.collect_schema().names()}
            if _dtn_rename:
                results["drug_target_negative_hcdt"] = _dtn.rename(_dtn_rename)


    # ---- load drug synonym association parquet (written by preprocess_v2.ipynb) ----
    # One row per drug_id × synonym; enables exact brand-name → drug lookup.
    # Missing = preprocess not yet re-run; warn and skip gracefully.
    try:
        results["drug_synonyms_association_hcdt"] = read_parquet_polars(
            path="database", database=tool,
            name=f"drug_synonyms_association_hcdt{sfx}")
        logger.info("[%s] loaded drug_synonyms_association_hcdt", tool)
    except Exception as e:
        logger.warning(
            f"[{tool}] drug_synonyms_association_hcdt{sfx} missing — "
            f"re-run preprocess_v2.ipynb to generate: {e}")

    # ---- clean text + dedup + wrap as {tool: results} ----
    return clean_table_dict("hcdt", results)

