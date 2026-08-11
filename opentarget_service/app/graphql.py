# ─────────────────────────────────────────────────────────────────────────────
# Associations with per-datatype evidence scores
# ─────────────────────────────────────────────────────────────────────────────
TARGET_ASSOC_PAGED_QUERY_V2 = """
query ($id: String!, $index: Int!, $size: Int!) {
  target(ensemblId: $id) {
    associatedDiseases(page: {size: $size, index: $index}) {
      count
      rows {
        score
        datatypeScores { id score }
        disease { id name }
      }
    }
  }
}
"""

DISEASE_TARGETS_PAGED_QUERY_V2 = """
query DiseaseTargets($id: String!, $index: Int!, $size: Int!) {
  disease(efoId: $id) {
    associatedTargets(enableIndirect: true, page: { index: $index, size: $size }) {
      count
      rows {
        score
        datatypeScores { id score }
        target { id approvedSymbol approvedName biotype }
      }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Target biological annotation — comprehensive
# ─────────────────────────────────────────────────────────────────────────────
TARGET_INFO_QUERY = """
query ($id: String!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    approvedName
    biotype
    isEssential
    functionDescriptions
    symbolSynonyms { label source }
    nameSynonyms { label source }
    synonyms { label source }
    dbXrefs { source id }
    proteinIds { source id }

    genomicLocation { chromosome start end strand }
    canonicalTranscript { id chromosome start end strand }

    targetClass { id label level }

    subcellularLocations { source location termSL labelSL }

    tractability { label modality value }

    geneticConstraint {
      constraintType score exp obs oe oeLower oeUpper
    }

    geneOntology {
      aspect
      source
      geneProduct
      evidence
      term { id label }
    }

    safetyLiabilities {
      event
      eventId
      datasource
      url
      effects { direction dosing }
      biosamples { tissueId tissueLabel cellId cellLabel cellFormat }
      studies { name type description }
    }

    expressions {
      tissue { id label anatomicalSystems organs }
      protein { level reliability }
      rna { value unit zscore }
    }

    mousePhenotypes {
      modelPhenotypeId
      modelPhenotypeLabel
      targetInModel
      targetInModelEnsemblId
      targetInModelMgiId
    }

    pharmacogenomics {
      pgxCategory
      evidenceLevel
      variantId
      variantRsId
      genotype
      genotypeAnnotationText
      phenotypeText
      isDirectTarget
      datasourceId
      drugs { drugId drugFromSource }
    }

    prioritisation {
      items { key value }
    }

    hallmarks {
      cancerHallmarks { label impact pmid description }
      attributes { name description pmid }
    }

    homologues {
      targetGeneId
      targetGeneSymbol
      speciesName
      speciesId
      homologyType
      isHighConfidence
      queryPercentageIdentity
      targetPercentageIdentity
    }

    chemicalProbes {
      id
      mechanismOfAction
      isHighQuality
      probesDrugsScore
      probeMinerScore
      origin
      drugId
    }

    depMapEssentiality {
      tissueId
      tissueName
      screens {
        cellLineName
        geneEffect
        expression
        diseaseFromSource
        diseaseCellLineId
        depmapId
      }
    }

    tep { name uri therapeuticArea }

    # ─── ADDED: full-coverage Option A (2026-05) ───────────────────────
    # PPI list — disjoint from STRING/BioGRID via OT's IntAct + Reactome
    # integration; carries score, source, intABiologicalRole.
    interactions(page: { size: 50, index: 0 }) {
      count
      rows {
        intA
        intABiologicalRole
        intB
        intBBiologicalRole
        score
        count
        sourceDatabase
        speciesA { taxonId }
        speciesB { taxonId }
      }
    }

    # Similar targets (OT's own embedding-based neighbours, returns
    # [Similarity!]! where each Similarity has score + object)
    similarEntities(threshold: 0.5, size: 20) {
      score
      object { ... on Target { id approvedSymbol approvedName } }
    }

    # Literature mention counts (Publications object)
    literatureOcurrences { count }

    # Alternate-gene IDs (pseudogenes / paralogs) and Ensembl transcript IDs.
    # (NB: `additionalIds` looks like a Target field in introspection but is
    # actually a *query argument* to similarEntities/literatureOcurrences/
    # evidences — it is NOT a selectable field on Target itself.)
    alternativeGenes
    transcriptIds

    # ─── ADDED: deeper sub-field coverage (2026-05-18) ───────────────────
    # NB: Target.evidences requires `efoIds: [String!]!` — it is a
    # target-DISEASE pair query, not a target-only field. To drill into
    # the evidence rows behind an association, call OT with both the
    # target and disease IDs; do not request it here.

    # GWAS credible sets colocalising with this target.
    credibleSets(page: { size: 10, index: 0 }) {
      count
      rows {
        studyLocusId
        studyId
        studyType
        chromosome
        position
        pValueMantissa
        pValueExponent
        finemappingMethod
        beta
        zScore
      }
    }

    # Full transcript list (canonical is already covered separately).
    transcripts {
      transcriptId
      translationId
      biotype
      isEnsemblCanonical
      isUniprotReviewed
      uniprotId
      uniprotIsoformId
      alphafoldId
    }

    # Protein-coding coordinates (per-residue annotations from UniProt etc.).
    proteinCodingCoordinates(page: { size: 25, index: 0 }) {
      count
      rows {
        aminoAcidPosition
        referenceAminoAcid
        alternateAminoAcid
        uniprotAccessions
        variantEffect
        datasources { datasourceId datasourceNiceName datasourceCount }
      }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Disease enriched info (therapeutic areas, phenotypes, synonyms, xrefs)
# ─────────────────────────────────────────────────────────────────────────────
DISEASE_INFO_QUERY = """
query ($id: String!) {
  disease(efoId: $id) {
    id
    name
    description
    isTherapeuticArea
    dbXRefs
    therapeuticAreas { id name }
    synonyms {
      relation
      terms
    }
    phenotypes(page: { size: 50, index: 0 }) {
      count
      rows {
        phenotypeHPO { id name description }
        phenotypeEFO { id name }
      }
    }

    # ─── ADDED: full-coverage Option A (2026-05) ─────────────────────────
    # Direct ontology parents/children — return [Disease!]!, need sub-fields.
    # Fixes the MONDO/DOID "subtypes of" failure mode (F6).
    parents { id name }
    children { id name }
    # Transitive closures: [String!]! (IDs only).
    ancestors
    descendants
    resolvedAncestors { id name }

    # Anatomical-location bindings (for tissue-restricted diseases)
    directLocationIds
    indirectLocationIds

    # Similar diseases via OT embeddings (returns [Similarity!]!)
    similarEntities(threshold: 0.5, size: 20) {
      score
      object { ... on Disease { id name } }
    }

    # Literature mention counts (Publications object)
    literatureOcurrences { count }

    # Active OT research projects on this disease
    otarProjects {
      otarCode
      projectName
      reference
      integratesInPPP
    }

    # ─── ADDED: full anatomical-location objects (2026-05-18) ────────────
    # `directLocationIds` / `indirectLocationIds` give only the UBERON IDs;
    # `directLocations` / `indirectLocations` resolve them to Disease nodes
    # with human-readable names so callers don't need a second round-trip.
    directLocations { id name }
    indirectLocations { id name }

    # NB: Disease.evidences is intentionally NOT requested here — OT's
    # schema requires `ensemblIds: [String!]!`, which makes it a
    # target-disease pair query, not a disease-only metadata field. Use
    # the Target.evidences entry point (in target_tool) for evidence
    # drill-down.
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Drug enriched info (synonyms, trade names, approval year, warnings, ADRs)
# ─────────────────────────────────────────────────────────────────────────────
DRUG_ENRICHED_QUERY = """
query ($chemblId: String!) {
  drug(chemblId: $chemblId) {
    id
    name
    synonyms
    tradeNames
    description
    drugType
    maximumClinicalStage
    crossReferences { source ids }
    parentMolecule { id name }
    drugWarnings {
      warningType
      description
      year
      toxicityClass
      efoId
      efoTerm
      country
      references { id source }
    }
    adverseEvents(page: { size: 25, index: 0 }) {
      count
      criticalValue
      rows { name count logLR meddraCode }
    }
    mechanismsOfAction {
      rows {
        mechanismOfAction
        targetName
        targets { id approvedSymbol approvedName }
        references { source }
      }
    }
    indications {
      count
      rows {
        maxClinicalStage
        disease { id name }
        clinicalReports {
          trialOverallStatus
          trialPhase
          clinicalStage
        }
      }
    }

    # ─── ADDED: full-coverage Option A (2026-05) ─────────────────────────
    # Drug derivatives (e.g. esters, salts) — for chemistry repurposing
    childMolecules { id name }

    # Similar drugs via OT embeddings (returns [Similarity!]!)
    similarEntities(threshold: 0.5, size: 20) {
      score
      object { ... on Drug { id name } }
    }

    # Literature mention counts (Publications object)
    literatureOcurrences { count }

    # (NB: `additionalIds` is not a selectable Drug field — it is an input
    # argument to similarEntities / literatureOcurrences.)

    # ─── ADDED: drug-side pharmacogenomics (2026-05-18) ──────────────────
    # Drug.pharmacogenomics surfaces PGx variants/genotypes linked to the
    # drug itself (PharmGKB). Complements Target.pharmacogenomics which
    # answers the inverse query (PGx-relevant variants on a given target).
    pharmacogenomics {
      pgxCategory
      evidenceLevel
      variantId
      variantRsId
      genotype
      genotypeAnnotationText
      phenotypeText
      isDirectTarget
      datasourceId
      target { id approvedSymbol approvedName }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Variant info — entirely new category (variant-genetics)
# Pulls allele frequencies, predicted consequences, credible-set membership.
# Used for follow-on queries when ClinVar or CIViC return a rsID/variant ID.
# ─────────────────────────────────────────────────────────────────────────────
VARIANT_INFO_QUERY = """
query ($variantId: String!) {
  variant(variantId: $variantId) {
    id
    chromosome
    position
    referenceAllele
    alternateAllele
    rsIds
    hgvsId
    mostSevereConsequence { id label }
    alleleFrequencies {
      populationName
      alleleFrequency
    }
    dbXrefs { id source }
    transcriptConsequences {
      transcriptId
      transcriptIndex
      isEnsemblCanonical
      lofteePrediction
      siftPrediction
      polyphenPrediction
      impact
      consequenceScore
      aminoAcidChange
      target { id approvedSymbol }
      variantConsequences { id label }
    }
    credibleSets(page: { size: 25, index: 0 }) {
      count
      rows {
        studyLocusId
        studyId
        studyType
      }
    }

    # ─── ADDED: deeper variant coverage (2026-05-18) ─────────────────────
    # VEP-style assessment block (functional effect predictions).
    variantEffect {
      method
      assessment
      assessmentFlag
      score
      normalisedScore
      target { id approvedSymbol }
    }
    # Human-readable description of the variant.
    variantDescription
    # PharmGKB PGx evidence linked to the variant.
    pharmacogenomics {
      pgxCategory
      evidenceLevel
      genotype
      genotypeAnnotationText
      phenotypeText
      datasourceId
      target { id approvedSymbol }
      drugs { drugId drugFromSource }
    }
    # Raw evidence rows for this variant (no required filters; paged).
    evidences(size: 20) {
      count
      rows {
        id
        datatypeId
        datasourceId
        score
        publicationYear
        literature
        disease { id name }
        target { id approvedSymbol }
        drug { id name }
      }
    }
    # Enhancer→gene predictions (regulatory variants).
    enhancerToGenes(page: { size: 20, index: 0 }) {
      count
      rows {
        score
        chromosome
        start
        end
        distanceToTss
        intervalType
        biosampleName
        datasourceId
        pmid
        target { id approvedSymbol }
      }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Study info — GWAS study metadata + summary-stats availability
# ─────────────────────────────────────────────────────────────────────────────
STUDY_INFO_QUERY = """
query ($studyId: String!) {
  study(studyId: $studyId) {
    id
    studyType
    traitFromSource
    nCases
    nControls
    nSamples
    publicationFirstAuthor
    publicationDate
    publicationTitle
    publicationJournal
    pubmedId
    hasSumstats
    summarystatsLocation
    ldPopulationStructure { ldPopulation relativeSampleSize }
    diseases { id name }
    cohorts

    # ─── ADDED: deeper study coverage (2026-05-18) ───────────────────────
    # Project / analysis metadata.
    projectId
    initialSampleSize
    traitFromSourceMappedIds
    analysisFlags
    condition
    qualityControls
    # Per-stage sample sizes with ancestry.
    discoverySamples { sampleSize ancestry }
    replicationSamples { sampleSize ancestry }
    # Summary-stats QC values.
    sumstatQCValues { QCCheckName QCCheckValue }
    # eQTL / QTL studies link to a target gene + the biosample
    # (tissue / cell line) where the QTL was measured.
    target { id approvedSymbol approvedName }
    biosample { biosampleId biosampleName description }
    # Background traits (for case-control GWAS) and the study's own
    # fine-mapped credible sets.
    backgroundTraits { id name }
    credibleSets(page: { size: 25, index: 0 }) {
      count
      rows { studyLocusId chromosome position pValueMantissa pValueExponent }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Search — cross-entity discovery (top-level OT API)
# ─────────────────────────────────────────────────────────────────────────────
SEARCH_QUERY = """
query ($queryString: String!, $entityNames: [String!]) {
  search(queryString: $queryString, entityNames: $entityNames) {
    total
    hits {
      id
      entity
      name
      description
      score
      object {
        ... on Target { approvedSymbol approvedName biotype }
        ... on Disease { name description }
        ... on Drug { name drugType maximumClinicalStage }
        ... on Variant { id rsIds chromosome }
      }
    }
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Target prioritisation — comprehensive 17-attribute breakdown
# (Target.prioritisation is already in TARGET_INFO_QUERY; this fetches it
# as a standalone for quicker single-call use.)
# ─────────────────────────────────────────────────────────────────────────────
TARGET_PRIORITISATION_QUERY = """
query ($id: String!) {
  target(ensemblId: $id) {
    id
    approvedSymbol
    prioritisation { items { key value } }
    tractability { label modality value }
    isEssential
  }
}
"""

# ─────────────────────────────────────────────────────────────────────────────
# Evidence records for a specific target-disease pair
# ─────────────────────────────────────────────────────────────────────────────
EVIDENCES_QUERY = """
query ($ensemblId: String!, $efoIds: [String!]!, $size: Int!) {
  target(ensemblId: $ensemblId) {
    evidences(efoIds: $efoIds, size: $size) {
      count
      rows {
        id
        datasourceId
        datatypeId
        score
        target { id approvedSymbol }
        disease { id name }
        literature
        pathways { id name }
        variantRsId
      }
    }
  }
}
"""

# OpenTargets API v26.03+: Drug.knownDrugs moved to Drug.indications
DRUG_INDICATIONS_QUERY_V26 = """
query ($chemblId: String!) {
  drug(chemblId: $chemblId) {
    indications {
      count
      rows {
        id
        maxClinicalStage
        disease { id name }
        clinicalReports {
          trialOverallStatus
          trialPhase
          clinicalStage
        }
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

# OpenTargets API v26.03+: Target.knownDrugs moved to Target.drugAndClinicalCandidates
TARGET_DRUGS_QUERY_V26 = """
query ($id: String!) {
  target(ensemblId: $id) {
    drugAndClinicalCandidates {
      count
      rows {
        id
        maxClinicalStage
        drug {
          id
          name
          mechanismsOfAction {
            rows {
              actionType
              mechanismOfAction
              targets { id }
            }
          }
        }
        diseases {
          diseaseFromSource
          disease { id name }
        }
        clinicalReports {
          trialOverallStatus
          trialPhase
          clinicalStage
        }
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


# OpenTargets API v26.03+: Disease.knownDrugs moved to Disease.drugAndClinicalCandidates
DISEASE_DRUG_AND_CLINICAL_CANDIDATES_QUERY_V26 = """
query ($efoId: String!) {
  disease(efoId: $efoId) {
    id
    name
    drugAndClinicalCandidates {
      count
      rows {
        id
        maxClinicalStage
        drug {
          id
          name
          drugType
          mechanismsOfAction {
            rows {
              mechanismOfAction
              targets { id approvedSymbol }
            }
          }
        }
        clinicalReports {
          trialOverallStatus
          trialPhase
          clinicalStage
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
    associatedTargets(enableIndirect: true, page: { index: $index, size: $size }) {
      count
      rows {
        score
        target { id approvedSymbol approvedName }
      }
    }
  }
}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Platform-wide queries added 2026-05-15
#   - meta            : API + data version
#   - credibleSet(s)  : single + paged
#   - targets/diseases/drugs/studies/clinicalReports — batch lookups
#   - mapIds, facets, associationDatasources, interactionResources, geneOntologyTerms
# Field names verified against the live schema (api.platform.opentargets.org/api/v4/graphql).
# ─────────────────────────────────────────────────────────────────────────────

META_QUERY = """
query Meta {
  meta {
    name
    apiVersion { x y z }
    dataVersion { year month iteration }
  }
}
"""

CREDIBLESET_QUERY = """
query CredibleSet($id: String!) {
  credibleSet(studyLocusId: $id) {
    studyLocusId
    studyId
    studyType
    chromosome
    position
    region
    pValueMantissa
    pValueExponent
    beta
    standardError
    zScore
    finemappingMethod
    confidence
    credibleSetIndex
    credibleSetlog10BF
    purityMeanR2
    purityMinR2
    sampleSize
    locusStart
    locusEnd
    qtlGeneId
    isTransQtl
    variant { id chromosome position referenceAllele alternateAllele }
  }
}
"""

CREDIBLESETS_QUERY = """
query CredibleSets(
  $index: Int!
  $size: Int!
  $studyLocusIds: [String!]
  $studyIds: [String!]
  $variantIds: [String!]
  $studyTypes: [StudyTypeEnum!]
  $regions: [String!]
) {
  credibleSets(
    page: { index: $index, size: $size }
    studyLocusIds: $studyLocusIds
    studyIds: $studyIds
    variantIds: $variantIds
    studyTypes: $studyTypes
    regions: $regions
  ) {
    count
    rows {
      studyLocusId
      studyId
      studyType
      chromosome
      position
      pValueMantissa
      pValueExponent
      beta
      finemappingMethod
      confidence
      variant { id }
    }
  }
}
"""

TARGETS_BATCH_QUERY = """
query TargetsBatch($ids: [String!]!) {
  targets(ensemblIds: $ids) {
    id
    approvedSymbol
    approvedName
    biotype
    functionDescriptions
    proteinIds { source id }
  }
}
"""

DISEASES_BATCH_QUERY = """
query DiseasesBatch($ids: [String!]!) {
  diseases(efoIds: $ids) {
    id
    name
    description
    therapeuticAreas { id name }
    synonyms { terms relation }
  }
}
"""

DRUGS_BATCH_QUERY = """
query DrugsBatch($ids: [String!]!) {
  drugs(chemblIds: $ids) {
    id
    name
    drugType
    description
    synonyms
    tradeNames
    maximumClinicalStage
    crossReferences { source ids }
  }
}
"""

STUDIES_BATCH_QUERY = """
query StudiesBatch(
  $index: Int!
  $size: Int!
  $studyId: String
  $diseaseIds: [String!]
  $enableIndirect: Boolean
) {
  studies(
    page: { index: $index, size: $size }
    studyId: $studyId
    diseaseIds: $diseaseIds
    enableIndirect: $enableIndirect
  ) {
    count
    rows {
      id
      studyType
      traitFromSource
      publicationFirstAuthor
      publicationDate
      publicationJournal
      nSamples
      cohorts
    }
  }
}
"""

MAP_IDS_QUERY = """
query MapIds($terms: [String!]!, $entities: [String!]) {
  mapIds(queryTerms: $terms, entityNames: $entities) {
    total
    aggregations { entities { name total } }
    mappings {
      term
      hits { id name entity score }
    }
  }
}
"""

FACETS_QUERY = """
query Facets($queryString: String, $entities: [String!], $category: String, $index: Int!, $size: Int!) {
  facets(queryString: $queryString, entityNames: $entities, category: $category, page: { index: $index, size: $size }) {
    total
    categories { name total }
    hits { id label category score highlights }
  }
}
"""

ASSOCIATION_DATASOURCES_QUERY = """
query AssociationDatasources {
  associationDatasources { datasource datatype }
}
"""

# Baseline tissue/cell-type expression (replaces the deprecated `expressions` field).
# Returns expression values per tissue biosample across GTEx, RNA-seq, proteomics, etc.
# Paged: 200 rows per call covers most targets; sorted by distribution_score DESC.
BASELINE_EXPRESSION_QUERY = """
query BaselineExpression($id: String!, $index: Int!, $size: Int!) {
  target(ensemblId: $id) {
    baselineExpression(page: { index: $index, size: $size }) {
      count
      rows {
        datasourceId
        datatypeId
        tissueBiosampleFromSource
        tissueBiosample { biosampleId biosampleName }
        tissueBiosampleParent { biosampleId biosampleName }
        celltypeBiosampleFromSource
        celltypeBiosample { biosampleId biosampleName }
        median
        max
        specificity_score
        distribution_score
        unit
      }
    }
  }
}
"""

INTERACTION_RESOURCES_QUERY = """
query InteractionResources {
  interactionResources { sourceDatabase databaseVersion }
}
"""

GENE_ONTOLOGY_TERMS_QUERY = """
query GeneOntologyTerms($ids: [String!]!) {
  geneOntologyTerms(goIds: $ids) { id label }
}
"""

CLINICAL_REPORT_QUERY = """
query ClinicalReport($id: String!) {
  clinicalReport(clinicalReportId: $id) {
    id
    title
    type
    source
    url
    year
    countries
    clinicalStage
    phaseFromSource
    trialPhase
    trialOfficialTitle
    trialStudyType
    trialPrimaryPurpose
    trialOverallStatus
    trialStartDate
    trialNumberOfArms
    trialDescription
    hasExpertReview
  }
}
"""

CLINICAL_REPORTS_QUERY = """
query ClinicalReports($ids: [String!]!) {
  clinicalReports(clinicalReportsIds: $ids) {
    id
    title
    type
    source
    url
    clinicalStage
    trialPhase
    trialOverallStatus
  }
}
"""

SEARCH_QUERY_TOOL = """
query Search($queryString: String!, $entities: [String!], $index: Int!, $size: Int!) {
  search(queryString: $queryString, entityNames: $entities, page: { index: $index, size: $size }) {
    total
    aggregations { entities { name total } }
    hits { id name entity score category description }
  }
}
"""
