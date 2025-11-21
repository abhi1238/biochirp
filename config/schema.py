from pydantic import BaseModel, Field, constr
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from pydantic import BaseModel, Extra, Field, constr, model_validator


database_schemas = {

    "ttd": {
        "target_pathway_association": ['target_id', 'pathway_id', 'pathway_name'],
        "biomarker_disease_association": ['biomarker_id', 'biomarker_name', 'disease_id', 'icd_11_code_for_disease', 'icd_10_code_for_disease', 'icd_9_code_for_disease'],
        "target_master_table": ['target_id', 'target_name', 'target_class', 'ec_number', 'target_former_identifier', 'target_biological_function',
                                'gene_name', 'pdb_structure', 'aa_sequence', 'target_evidence_type', 'target_uniprot_id'],
        "target_disease_association": ["target_id", "disease_id"],
        "drug_disease_association": ['drug_id', 'disease_id', 'approval_status'],
        "drug_master_table": ['drug_id', 'drug_name', 'drug_composition_class', 'drug_components', 'drug_inchi', 'drug_inchi_key',
                              'drug_smiles', 'drug_type', 'drug_therapeutic_class', 'drug_trade_name'],
        "drug_target_association": ['target_id', 'drug_id', 'drug_mechanism_of_action_on_target'],
        "disease_master_table": ['disease_id', 'disease_name']
    }
    ,
    "ctd": {
        "chemical_gene_association": ['drug_id', 'cas_rn', 'gene_id', 'organism', 'organism_id',
                                        'interaction', 'interaction_actions', 'pubmed_ids'],
        "gene_pathway_association": ['gene_id', 'pathway_id'],
        "chemical_disease_association" : ['drug_id', 'disease_id'],
        "disease_pathway_association": ['disease_id', 'pathway_id'],
        "chemical_master_table" : ['drug_name', 'drug_id', 'cas_rn'],
        "pathway_master_table" : ['pathway_name', 'pathway_id'],
        # "CTD_diseases" : ['disease_name', 'disease_id'],
        "gene_master_table" : ['gene_name', 'gene_id', 'pharm_gkb_ids'],
        "disease_master_table" :  ['disease_name', 'disease_id']
    },

    "hcdt":
    {
        'drug_master_table': ['pubchem_cid', 'drug_name', 'compound_synonym', 'molecular_weight', 'molecular_formula',  'inchi', 'iso_smiles', 'canonical_smiles', 'inchi_key', 'iupac_name'],
        'drug_gene_association': ['pubchem_cid', 'hgnc_id'],
        'drug_disease_association': ['pubchem_cid', 'disease_id'],
        'drug_pathway_association': ['pubchem_cid', 'kegg_id'],
        'pathway_gene_association': ['hgnc_id', 'kegg_id'],
        "disease_master_table" : ['disease_id', 'disease_name'],
        "gene_master_table": ['hgnc_id', 'gene_name', 'entrez_id', 'ensembl_id', 'uniprot_id'],
        "pathway_master_table" : ['pathway_name', 'reactome_id', 'kegg_hsaid', 'smpdb_id', 'kegg_id']
    }

}




primary_keys_by_db = {
    "ttd": {
        "drug_master_table": ["drug_id"],
        "target_master_table": ["target_id"],
        "target_pathway_association": ['target_id', 'pathway_id'],
        "biomarker_disease_association": ['biomarker_id', 'disease_id'],
        "target_disease_association": ["target_id", "disease_name"],
        "drug_disease_association": ['drug_id', 'disease_name'],
        "drug_target_association": ['target_id', 'drug_id'],
        "disease_master_table": ['disease_id'],
    }
    ,
    "ctd": {
        'chemical_gene_association': ['drug_id', 'gene_id'],
        'chemical_master_table': ['drug_id'],
        'chemical_disease_association': ['drug_id', 'disease_id'],
        'disease_master_table': ['disease_id'],
        'disease_pathway_association': ['disease_id', 'pathway_id'],
        'gene_master_table': ['gene_id'],
        'gene_pathway_association': ['gene_id', 'pathway_id'],
        'pathway_master_table': ['pathway_id']
        },
    "hcdt":
            {
        'drug_master_table': ['pubchem_cid'],
        'drug_gene_association': ['pubchem_cid', 'hgnc_id'],
        'drug_disease_association': ['pubchem_cid', 'disease_id'],
        'drug_pathway_association': ['pubchem_cid', 'kegg_id'],
        "disease_master_table": ['disease_id'],
        "gene_master_table": ['hgnc_id'],
        "pathway_master_table" : ['kegg_id'],
        'pathway_gene_association': ['hgnc_id', 'kegg_id'],
        }
}

foreign_keys_by_db = {
    "ttd": [
        ("target_pathway_association", "target_id", "target_master_table", "target_id"),
        ("target_disease_association", "target_id", "target_master_table", "target_id"),
        ("target_disease_association", "disease_id", "disease_master_table", "disease_id"),
        ("drug_disease_association", "drug_id", "drug_master_table", "drug_id"),
        ("drug_disease_association", "disease_id", "disease_master_table", "disease_id"),
        ("drug_target_association", "target_id", "target_master_table", "target_id"),
        ("drug_target_association", "drug_id", "drug_master_table", "drug_id"),
        ("biomarker_disease_association", "disease_id", "disease_master_table", "disease_id"),
    ],


    "ctd": [('chemical_gene_association','drug_id','chemical_master_table','drug_id'),
            ('chemical_gene_association','drug_id','chemical_disease_association','drug_id'),
             ('chemical_gene_association', 'gene_id', 'gene_master_table', 'gene_id'),
            ('chemical_gene_association','gene_id', 'gene_pathway_association', 'gene_id'),
            ('chemical_master_table', 'drug_id','chemical_disease_association', 'drug_id'),
            ('chemical_disease_association', 'disease_id', 'disease_master_table', 'disease_id'),
            ('chemical_disease_association', 'disease_id', 'disease_pathway_association', 'disease_id'),
            ('disease_master_table', 'disease_id', 'disease_pathway_association', 'disease_id'),
            ('disease_pathway_association','pathway_id','gene_pathway_association', 'pathway_id'),
            ('disease_pathway_association', 'pathway_id', 'pathway_master_table', 'pathway_id'),
            # ('disease_pathway_association', 'gene_name', 'gene_master_table', 'gene_name'),
            ('gene_master_table', 'gene_id', 'gene_pathway_association', 'gene_id'),
            ('gene_pathway_association', 'pathway_id', 'pathway_master_table', 'pathway_id')],

    "hcdt" : [('drug_master_table', 'pubchem_cid', 'drug_gene_association', 'pubchem_cid'),
            ('drug_master_table',  'pubchem_cid',  'drug_disease_association',  'pubchem_cid'),
            ('drug_master_table',  'pubchem_cid',  'drug_pathway_association',  'pubchem_cid'),
            ('drug_gene_association',  'pubchem_cid',  'drug_disease_association',  'pubchem_cid'),
            ('drug_gene_association',  'pubchem_cid', 'drug_pathway_association',  'pubchem_cid'),
            ('drug_gene_association',  'hgnc_id',  'pathway_gene_association',  'hgnc_id'),
            ('drug_pathway_association',  'pubchem_cid',  'drug_disease_association',  'pubchem_cid'),
             ('disease_master_table',  'disease_id',  'drug_disease_association',  'disease_id'),
            ('gene_master_table',  'hgnc_id',  'pathway_gene_association',  'hgnc_id'),
             ('gene_master_table',  'hgnc_id',  'drug_gene_association',  'hgnc_id'),
             ('pathway_master_table',  'kegg_id',  'drug_pathway_association',  'kegg_id'),
             ('pathway_master_table',  'kegg_id',  'pathway_gene_association',  'kegg_id'),
            ('pathway_gene_association',  'kegg_id',  'drug_pathway_association',  'kegg_id')]
            #  ('rna_targetgene_association',  'gene_name',  'drug_gene_association',  'gene_name'),
            #  ('rna_targetgene_association',  'gene_name',  'negative_dti_association',  'gene_name'),
            #  ('rna_targetgene_association',  'gene_name',  'pathway_gene_association',  'gene_name'),
            #  ('drug_rna_association', 'PUBCHEM_CID', 'drug_master_table', 'PUBCHEM_CID'),
            #  ('drug_rna_association',  'PUBCHEM_CID',  'drug_gene_association',  'PUBCHEM_CID'),
            #  ('drug_rna_association',  'PUBCHEM_CID',  'drug_disease_association',  'PUBCHEM_CID'),
            #  ('drug_rna_association',  'PUBCHEM_CID',  'drug_pathway_association',  'PUBCHEM_CID'),
            #  ('drug_rna_association',  'PUBCHEM_CID',  'negative_dti_association',  'PUBCHEM_CID'),
            #  ('drug_rna_association',  'PUBCHEM_CID',  'pathway_gene_association',  'PUBCHEM_CID')]

}
