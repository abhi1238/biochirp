DRUG_KNOWN_DISEASES_QUERY = """
query ($chemblId: String!, $cursor: String, $size: Int) {
  drug(chemblId: $chemblId) {
    knownDrugs(cursor: $cursor, size: $size) {
      cursor
      rows {
        phase
        status
        disease { id name }
        target { id approvedSymbol approvedName }
      }
    }
  }
}
"""

DRUG_MOA_QUERY = """
query ($chemblId: String!) {
  drug(chemblId: $chemblId) {
    mechanismsOfAction {
      rows {
        mechanismOfAction
        targetName
        targets { id approvedSymbol approvedName }
        references { source }
      }
    }
  }
}
"""

TARGET_DRUGS_QUERY = """
query ($id: String!, $cursor: String, $size: Int) {
  target(ensemblId: $id) {
    knownDrugs(cursor: $cursor, size: $size) {
      cursor
      rows {
        phase
        status
        disease { id name }
        drug { id name mechanismsOfAction { rows { mechanismOfAction targets { id }}}}
      }
    }
  }
}
"""


TARGET_ASSOC_QUERY = """
query ($id: String!) {
  target(ensemblId: $id) {
    associatedDiseases(page: {size: 2500, index: 0}) {
      rows { score disease { id name } }
    }
  }
}
"""

TARGET_ASSOC_PAGED_QUERY = """
query ($id: String!, $index: Int!, $size: Int!) {
  target(ensemblId: $id) {
    associatedDiseases(page: {size: $size, index: $index}) {
      count
      rows { score disease { id name } }
    }
  }
}
"""



TARGET_PATHWAYS_QUERY = """
query ($id: String!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    pathways { pathwayId pathway topLevelTerm }
  }
}
"""


DISEASE_KNOWN_DRUGS_QUERY = """
query ($efoId: String!, $freeTextQuery: String, $cursor: String, $size: Int) {
  disease(efoId: $efoId) {
    id
    name
    knownDrugs(
      cursor: $cursor,
      freeTextQuery: $freeTextQuery,
      size: $size
    ) {
      cursor
      rows {
        phase
        status
        drugType
        mechanismOfAction
        disease {
          id
          name
        }
        drug {
          id
          name
        }
        target {
          id
          approvedSymbol
          approvedName
        }
      }
    }
  }
}
"""


# DISEASE_TARGET_ASSOC_QUERY = """
# query ($id: String!) {
#   disease(efoId: $id) {
#     id
#     name
#     associatedTargets(page: {size: 2500, index: 0}) {
#       rows {
#         score
#         target { id approvedSymbol approvedName }
#       }
#     }
#   }
# }

# """



DISEASE_TARGET_ASSOC_QUERY = """
query ($id: String!) {
  disease(efoId: $id) {
    id
    name
    associatedTargets(page: {size: 2500, index: 0}) {
      rows {
        score
        target { id approvedSymbol approvedName }
      }
    }
  }
}
"""

DISEASE_TARGETS_PAGED_QUERY = """
query DiseaseTargets($id: String!, $index: Int!, $size: Int!) {
  disease(efoId: $id) {
    associatedTargets(page: { index: $index, size: $size }) {
      count
      rows {
        score
        target { id approvedSymbol approvedName }
      }
    }
  }
}
"""
