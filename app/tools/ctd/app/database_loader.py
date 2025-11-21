# import os
# import polars as pl
# import logging

# logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# logger = logging.getLogger("uvicorn.error")

# from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace



# def return_preprocessed_ctd(max_workers: int | None = None) -> dict[str, pl.DataFrame]:

#     tool = "ctd"

#     results = dict()

#     # results[f"{tool}"] = dict()

#     chemical_gene_association = read_parquet_polars(path="database", database= "ctd", name="chem_gene_association.parquet")
#     chemical_master_table = read_parquet_polars(path="database", database= "ctd", name="chemical_master_table.parquet")
#     chemical_disease_association = read_parquet_polars(path="database", database= "ctd", name="chemical_disease_association.parquet")
#     disease_master_table = read_parquet_polars(path="database", database= "ctd", name="disease_master_table.parquet")
#     disease_pathway_association = read_parquet_polars(path="database", database= "ctd", name="disease_pathway_association.parquet")
#     gene_master_table = read_parquet_polars(path="database", database= "ctd", name="gene_master_table.parquet")
#     gene_pathway_association = read_parquet_polars(path="database", database= "ctd", name="gene_pathway_association.parquet")
#     pathway_master_table = read_parquet_polars(path="database", database= "ctd", name="pathway_master_table.parquet")


#     results["chemical_gene_association_ctd"] = chemical_gene_association
#     results["chemical_master_table_ctd"] = chemical_master_table
#     results["chemical_disease_association_ctd"] = chemical_disease_association
#     results["disease_master_table_ctd"] = disease_master_table
#     results["disease_pathway_association_ctd"] = disease_pathway_association
#     results["gene_master_table_ctd"] = gene_master_table
#     results["gene_pathway_association_ctd"] = gene_pathway_association
#     results["pathway_master_table_ctd"] = pathway_master_table


#         # ---- normalize column names ----
#     mapping = {
#         "# ChemicalName": "drug_name", "ChemicalName": "drug_name",
#         "ChemicalID": "drug_id",
#         "GeneSymbol": "gene_name", "# GeneSymbol": "gene_name",
#         "GeneID": "gene_id", "GeneForms": "gene_forms",
#         "DiseaseName": "disease_name", "# DiseaseName": "disease_name",
#         "DiseaseID": "disease_id",
#         "PathwayName": "pathway_name", "# PathwayName": "pathway_name",
#         "PathwayID": "pathway_id",
#         "InferenceGeneSymbol" : "gene_name"
#     }



#     for name, df in results.items():
#         try:
#             rename_dict = {c: mapping[c] for c in df.columns if c in mapping}
#             if rename_dict:
#                 logger.info(f"{[tool]} Renaming columns in '{name}': {rename_dict}")
#             df = df.rename(rename_dict)
#             results[name] = df
#         except Exception as e:
#             logger.warning(f"{[tool]} Failed to rename columns in '{name}': {e}")

#     # ---- strip whitespace + deduplicate ----
#     for name, df in results.items():
#         try:
#             df = df.drop_nulls()
#             df = strip_all_whitespace(df).unique()
#             results[name] = df
#         except Exception as e:
#             logger.warning(f"{[tool]} Cleaning failed for '{name}': {e}")


#     db = dict()
#     db[tool] = results

#     return db



# database_loader.py
import logging
import os

import polars as pl

from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("uvicorn.error")


def return_preprocessed_ctd(max_workers: int | None = None) -> dict:
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

    # ---- load parquet tables ----
    chemical_gene_association = read_parquet_polars(
        path="database", database="ctd", name="chem_gene_association.parquet"
    )
    chemical_master_table = read_parquet_polars(
        path="database", database="ctd", name="chemical_master_table.parquet"
    )
    chemical_disease_association = read_parquet_polars(
        path="database", database="ctd", name="chemical_disease_association.parquet"
    )
    disease_master_table = read_parquet_polars(
        path="database", database="ctd", name="disease_master_table.parquet"
    )
    disease_pathway_association = read_parquet_polars(
        path="database", database="ctd", name="disease_pathway_association.parquet"
    )
    gene_master_table = read_parquet_polars(
        path="database", database="ctd", name="gene_master_table.parquet"
    )
    gene_pathway_association = read_parquet_polars(
        path="database", database="ctd", name="gene_pathway_association.parquet"
    )
    pathway_master_table = read_parquet_polars(
        path="database", database="ctd", name="pathway_master_table.parquet"
    )

    # ---- assemble into result dict ----
    results["chemical_gene_association_ctd"] = chemical_gene_association
    results["chemical_master_table_ctd"] = chemical_master_table
    results["chemical_disease_association_ctd"] = chemical_disease_association
    results["disease_master_table_ctd"] = disease_master_table
    results["disease_pathway_association_ctd"] = disease_pathway_association
    results["gene_master_table_ctd"] = gene_master_table
    results["gene_pathway_association_ctd"] = gene_pathway_association
    results["pathway_master_table_ctd"] = pathway_master_table

    # ---- normalize column names ----
    mapping = {
        "# ChemicalName": "drug_name",
        "ChemicalName": "drug_name",
        "ChemicalID": "drug_id",
        "GeneSymbol": "gene_name",
        "# GeneSymbol": "gene_name",
        "GeneID": "gene_id",
        "GeneForms": "gene_forms",
        "DiseaseName": "disease_name",
        "# DiseaseName": "disease_name",
        "DiseaseID": "disease_id",
        "PathwayName": "pathway_name",
        "# PathwayName": "pathway_name",
        "PathwayID": "pathway_id",
        "InferenceGeneSymbol": "gene_name",
    }

    for name, df in list(results.items()):
        try:
            rename_dict = {c: mapping[c] for c in df.columns if c in mapping}
            if rename_dict:
                logger.info("[%s] Renaming columns in '%s': %s", tool, name, rename_dict)
            df = df.rename(rename_dict)
            results[name] = df
        except Exception as e:
            logger.warning("[%s] Failed to rename columns in '%s': %s", tool, name, e)

    # ---- strip whitespace + deduplicate ----
    for name, df in list(results.items()):
        try:
            df = df.drop_nulls()
            df = strip_all_whitespace(df).unique()
            results[name] = df
        except Exception as e:
            logger.warning("[%s] Cleaning failed for '%s': %s", tool, name, e)

    db: dict[str, dict[str, pl.DataFrame]] = {}
    db[tool] = results

    logger.info("[%s] CTD database loaded with %d tables", tool, len(results))
    return db

    