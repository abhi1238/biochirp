import os
import polars as pl
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger("uvicorn.error")

from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace

def return_preprocessed_ttd(max_workers: int | None = None) -> dict[str, pl.DataFrame]:

    tool = "ttd"

    results = dict()

    # results[f"{tool}"] = dict()

    target_master_table_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-01-TTD_target_download.parquet")
    drug_master_table_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-02-TTD_drug_download.parquet")
    drug_disease_association_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-05-Drug_disease.parquet")
    target_disease_association_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-06-Target_disease.parquet")
    drug_target_association_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-07-Drug-TargetMapping.parquet")
    biomarker_disease_association_ttd = read_parquet_polars(path="database", database= "ttd", name="P1-08-Biomarker_disease.parquet")
    target_pathway_association_ttd = read_parquet_polars(path="database", database= "ttd", name="P4-01-Target-KEGGpathway_all.parquet")
    disease_master_table_ttd = read_parquet_polars(path="database", database= "ttd", name="disease_master_table_ttd.parquet")


    results["target_master_table_ttd"] = target_master_table_ttd
    results["drug_master_table_ttd"] = drug_master_table_ttd
    results["drug_disease_association_ttd"] = drug_disease_association_ttd
    results["target_disease_association_ttd"] = target_disease_association_ttd
    results["drug_target_association_ttd"] = drug_target_association_ttd
    results["biomarker_disease_association_ttd"] = biomarker_disease_association_ttd
    results["target_pathway_association_ttd"] = target_pathway_association_ttd
    results["disease_master_table_ttd"] = disease_master_table_ttd


        # ---- standardize column names ----
    mapping = {
        "DRUG_NAME": "drug_name", "Drug_Name": "drug_name",
        "GENE_SYMBOL": "gene_name", "Target": "gene_name",
        "Disease_Name": "disease_name", "# Disease_Name": "disease_name",
        "PathwayID": "pathway_id", "PATH_NAME": "pathway_name", "path_name": "pathway_name",
        "Pubchem_CID": "PUBCHEM_CID",
        "Biomarker_Name": "biomarker_name",
        "TTDID": "target_id", "KEGG pathway ID" : "pathway_id",
        "KEGG pathway name": "pathway_name",
        "TargetID":"target_id", "DrugID": "drug_id",
        "MOA": 'drug_mechanism_of_action_on_target'

    }

    for name, df in results.items():
        try:
            rename_dict = {c: mapping[c] for c in df.columns if c in mapping}
            if rename_dict:
                logger.info(f"{[tool]} Renaming columns in '{name}': {rename_dict}")
            df = df.rename(rename_dict)
            results[name] = df
        except Exception as e:
            logger.warning(f"{[tool]} Failed to rename columns in '{name}': {e}")

    # ---- strip whitespace + deduplicate ----
    for name, df in results.items():
        try:
            df = df.drop_nulls()
            df = strip_all_whitespace(df).unique()
            results[name] = df
        except Exception as e:
            logger.warning(f"{[tool]} Cleaning failed for '{name}': {e}")


    db = dict()
    db[tool] = results

    return db

    

# # app/database_loader.py

# import os
# import logging
# import polars as pl
# from utils.dataframe_loader import read_parquet_polars, strip_all_whitespace

# logger = logging.getLogger("uvicorn.error")

# def return_preprocessed_ttd(max_workers: int | None = None) -> dict[str, dict[str, pl.DataFrame]]:
#     tool = "ttd"
#     results: dict[str, pl.DataFrame] = {}

#     # load all the relevant tables
#     target_master_table_ttd = read_parquet_polars(path="database", database=tool, name="P1-01-TTD_target_download.parquet")
#     drug_master_table_ttd = read_parquet_polars(path="database", database=tool, name="P1-02-TTD_drug_download.parquet")
#     drug_disease_association_ttd = read_parquet_polars(path="database", database=tool, name="P1-05-Drug_disease.parquet")
#     target_disease_association_ttd = read_parquet_polars(path="database", database=tool, name="P1-06-Target_disease.parquet")
#     drug_target_association_ttd = read_parquet_polars(path="database", database=tool, name="P1-07-Drug‑TargetMapping.parquet")
#     biomarker_disease_association_ttd = read_parquet_polars(path="database", database=tool, name="P1-08-Biomarker_disease.parquet")
#     target_pathway_association_ttd = read_parquet_polars(path="database", database=tool, name="P4-01‑Target‑KEGGpathway_all.parquet")
#     disease_master_table_ttd = read_parquet_polars(path="database", database=tool, name="disease_master_table_ttd.parquet")

#     results["target_master_table_ttd"] = target_master_table_ttd
#     results["drug_master_table_ttd"] = drug_master_table_ttd
#     results["drug_disease_association_ttd"] = drug_disease_association_ttd
#     results["target_disease_association_ttd"] = target_disease_association_ttd
#     results["drug_target_association_ttd"] = drug_target_association_ttd
#     results["biomarker_disease_association_ttd"] = biomarker_disease_association_ttd
#     results["target_pathway_association_ttd"] = target_pathway_association_ttd
#     results["disease_master_table_ttd"] = disease_master_table_ttd

#     # column rename mapping
#     mapping = {
#         "DRUG_NAME": "drug_name",
#         "Drug_Name": "drug_name",
#         "GENE_SYMBOL": "gene_name",
#         "Target": "gene_name",
#         "Disease_Name": "disease_name",
#         "# Disease_Name": "disease_name",
#         "PathwayID": "pathway_id",
#         "PATH_NAME": "pathway_name",
#         "path_name": "pathway_name",
#         "Pubchem_CID": "PUBCHEM_CID",
#         "Biomarker_Name": "biomarker_name",
#         "TTDID": "target_id",
#         "TargetID": "target_id",
#         "DrugID": "drug_id",
#         "MOA": "drug_mechanism_of_action_on_target"
#     }

#     # apply renaming, cleaning, deduplication
#     for name, df in results.items():
#         try:
#             rename_dict = {c: mapping[c] for c in df.columns if c in mapping}
#             if rename_dict:
#                 logger.info(f"[{tool}] Renaming columns in '{name}': {rename_dict}")
#             df = df.rename(rename_dict)
#             results[name] = df
#         except Exception as e:
#             logger.warning(f"[{tool}] Failed to rename columns in '{name}': {e}")

#     for name, df in results.items():
#         try:
#             df = df.drop_nulls()
#             df = strip_all_whitespace(df).unique()
#             results[name] = df
#         except Exception as e:
#             logger.warning(f"[{tool}] Cleaning failed for '{name}': {e}")

#     # wrap into outer dict keyed by tool
#     db: dict[str, dict[str, pl.DataFrame]] = {tool: results}
#     return db
