import os
import polars as pl
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")

from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace


def return_preprocessed_hcdt(max_workers: int | None = None) -> dict[str, pl.DataFrame]:


    tool = "hcdt"

    results = dict()


    drug_master_table = read_parquet_polars(path="database", database= tool, name="drug_master_table.parquet")
    drug_gene_association = read_parquet_polars(path="database", database= tool, name="drug_gene_association.parquet")
    drug_disease_association= read_parquet_polars(path="database", database= tool, name="drug_disease_association.parquet")
    drug_pathway_association= read_parquet_polars(path="database", database= tool, name="drug_pathway_association.parquet")
    pathway_gene_association= read_parquet_polars(path="database", database= tool, name="pathway_gene_association.parquet")
    disease_master_table= read_parquet_polars(path="database", database= tool, name="disease_master_table.parquet")
    gene_master_table= read_parquet_polars(path="database", database= tool, name="gene_master_table.parquet")
    pathway_master_table= read_parquet_polars(path="database", database= tool, name="pathway_master_table.parquet")


    results["drug_master_table_hcdt"] = drug_master_table
    results["drug_gene_association_hcdt"] = drug_gene_association
    results["drug_disease_association_hcdt"] = drug_disease_association
    results["drug_pathway_association_hcdt"] = drug_pathway_association
    results["pathway_gene_association_hcdt"] = pathway_gene_association
    results["disease_master_table_hcdt"] = disease_master_table
    results["gene_master_table_hcdt"] = gene_master_table
    results["pathway_master_table_hcdt"] = pathway_master_table


    # ---- column renaming ----
    mapping = {
        "DRUG_NAME": "drug_name", "Drug_Name": "drug_name",
        "GENE_SYMBOL": "gene_name", "Target": "gene_name",
        "# Disease_Name": "disease_name", "Disease_Name": "disease_name",
        "PathwayID": "pathway_id", "PATH_NAME": "pathway_name", "path_name": "pathway_name",
        "Pubchem_CID": "PUBCHEM_CID"
    }
    for name, df in results.items():
        try:
            df = df.rename({c: mapping[c] for c in df.columns if c in mapping})
            results[name] = df
        except Exception as e:
            logger.warning(f"[{tool}] Failed to rename columns in '{name}': {e}")

    # ---- clean text + dedup ----
    for name, df in results.items():
        try:
            df = strip_all_whitespace(df).unique()
            results[name] = df
        except Exception as e:
            logger.warning(f"[{tool}] Cleaning failed for '{name}': {e}")

    

    db = dict()
    db[tool] = results

    return db

