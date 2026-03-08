from pydantic import BaseModel, Field, constr
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Extra, Field, constr, model_validator
from typing import Dict, List, Tuple
from pprint import pprint

def validate_schema(database_schemas: dict):
    """Validate schema consistency at startup. Fail fast on errors."""
    for db, tables in database_schemas.items():
        for table, cols in tables.items():
            if not cols:
                raise ValueError(f"{db}.{table} has no columns")
            if len(cols) != len(set(cols)):
                raise ValueError(f"{db}.{table} has duplicate columns: {cols}")
            # Master tables must have exactly one _id column
            if table.endswith("_master_table"):
                id_cols = [c for c in cols if c.endswith("_id")]
                if len(id_cols) != 1:
                    raise ValueError(
                        f"{db}.{table} must have exactly one primary key ID, found {id_cols}"
                    )


def _build_id_to_master_table_map(tables: dict) -> dict:
    """Maps id column name -> master table name for a given DB."""
    id_to_master = {}
    for table_name, columns in tables.items():
        if table_name.endswith("_master_table"):
            for col in columns:
                if col.endswith("_id"):
                    id_to_master[col] = table_name
    return id_to_master


def generate_primary_keys(database_schemas: dict) -> dict:
    primary_keys_by_db = {}
    for db_name, tables in database_schemas.items():
        primary_keys_by_db[db_name] = {}
        for table_name, columns in tables.items():
            if table_name.endswith("_master_table"):
                # Use the _id column explicitly, not positional (handles ordering inconsistencies)
                pk = [col for col in columns if col.endswith("_id")]
                primary_keys_by_db[db_name][table_name] = pk
            elif "_association" in table_name:
                pk = [col for col in columns if col.endswith("_id")]
                primary_keys_by_db[db_name][table_name] = pk
    return primary_keys_by_db


def generate_foreign_keys(database_schemas: dict) -> dict:
    foreign_keys_by_db = {}
    for db_name, tables in database_schemas.items():
        fk_list = []
        id_to_master = _build_id_to_master_table_map(tables)

        for table_name, columns in tables.items():
            if "_association" in table_name:
                for col in columns:
                    if col.endswith("_id") and col in id_to_master:
                        master_table = id_to_master[col]
                        fk_list.append((table_name, col, master_table, col))

        foreign_keys_by_db[db_name] = fk_list
    return foreign_keys_by_db




from typing import Dict, List, Tuple


database_schemas = {

    "ttd": {
        "target_pathway_association": ['target_id', 'pathway_id'],
        "biomarker_disease_association": ['biomarker_id', 'disease_id'],
        "target_master_table": ['target_id', 'target_name', 'gene_name'],
        "target_disease_association": ["target_id", "disease_id"],
        "drug_disease_association": ['drug_id', 'disease_id', 'approval_status'],
        "drug_master_table": ['drug_id', 'drug_name'],
        "drug_target_association": ['target_id', 'drug_id', 'drug_mechanism_of_action_on_target'],
        "disease_master_table": ['disease_id', 'disease_name'],
        "pathway_master_table": ['pathway_id', 'pathway_name'],
        "biomarker_master_table":  ['biomarker_id', 'biomarker_name'],

    }
    ,
    "ctd": {
        "chemical_gene_association": ['drug_id', 'gene_id'],
        "gene_pathway_association": ['gene_id', 'pathway_id'],
        "gene_disease_association": ['gene_id', 'disease_id'],
        "chemical_disease_association" : ['drug_id', 'disease_id'],
        "disease_pathway_association": ['disease_id', 'pathway_id'],
        "chemical_master_table" : ['drug_id', 'drug_name'],
        "pathway_master_table" : ['pathway_id', 'pathway_name' ],
        # "CTD_diseases" : ['disease_name', 'disease_id'],
        "gene_master_table" : ['gene_id', 'gene_name'],
        "disease_master_table" :  ['disease_id', 'disease_name' ]
    },

    "hcdt":
    {
        'drug_master_table': ['drug_id', 'drug_name'],
        'drug_gene_association': ['drug_id', 'gene_id'],
        'drug_disease_association': ['drug_id', 'disease_id'],
        'drug_pathway_association': ['drug_id', 'pathway_id'],
        'pathway_gene_association': ['gene_id', 'pathway_id'],
        "disease_master_table" : ['disease_id', 'disease_name'],
        "gene_master_table": ['gene_id', 'gene_name'],
        "pathway_master_table" : ['pathway_name', 'pathway_id']
    }

}


validate_schema(database_schemas)

primary_keys_by_db = generate_primary_keys(database_schemas)
foreign_keys_by_db = generate_foreign_keys(database_schemas)

