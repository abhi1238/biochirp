"""BioChirp Orphanet data tool — schema_kg variant."""
import re
from app.per_db_tool import (
    setup_service_globals, SchemaKgConfig, make_schema_kg_handler,
)

from .database_loader import return_preprocessed_orphanet


SERVICE_NAME, DB_NAME, SUMMARIZER_MODEL_NAME, prompt_md, get_orphanet_db = \
    setup_service_globals("orphanet", "Orphanet", return_preprocessed_orphanet)


_ORPHANET_CAPABILITIES = (
    "- Rare disease catalog: ORPHA IDs + disease names (disease_master_table)\n"
    "- Disease-gene associations (gene symbols + Ensembl/Entrez IDs + association type, "
    "e.g. disease-causing germline mutation, susceptibility factor)\n"
    "- Disease-phenotype associations (HPO terms + HP IDs + frequency class)\n"
    "- Epidemiology: prevalence / incidence estimates (prevalence type, class, value, "
    "geographic scope)\n"
    "- Age of onset and mode of inheritance (e.g. autosomal recessive/dominant, X-linked)\n"
    "- Cross-references to external vocabularies (OMIM, ICD-10, ICD-11, MeSH, UMLS, "
    "MedDRA, GARD) with mapping relation\n"
    "- Disease classification hierarchy (Orphanet groups, disorder type, parent disease) "
    "and natural-history subtype relationships"
)
_ORPHANET_LIMITATIONS = (
    "drug-target associations, compound bioactivity, protein 3D structures, "
    "variant pathogenicity, gene expression levels, protein-protein interactions, "
    "or general biology knowledge"
)

# Eponymous and abbreviated disease names → Orphanet canonical names.
# Applied case-insensitively to rephrased_query BEFORE the schema_mapper LLM sees it
# (same mechanism as CTD's term_rewrite for MeSH disease names). These names are not
# recognised by the mapper's verbatim-copy rule, causing wrong-entity FAIL verdicts.
_ORPHANET_TERM_REWRITE = {
    # MPS II: Orphanet splits by severity — use Arabic "type 2" prefix so fuzzy
    # matching finds both "type 2, attenuated form" and "type 2, severe form".
    "Hunter disease":          "Mucopolysaccharidosis type 2",
    "Hunter syndrome":         "Mucopolysaccharidosis type 2",
    "MPS II":                  "Mucopolysaccharidosis type 2",
    # MPS I: Orphanet canonical names are "Hurler syndrome" / "Hurler-Scheie syndrome"
    # — "Mucopolysaccharidosis type I" (Roman) does NOT exist in the catalog.
    "Hurler disease":          "Hurler syndrome",
    "MPS I":                   "Hurler syndrome",
    # Pompe: Orphanet splits by onset; prefix matches both infantile and late-onset.
    "Pompe disease":           "Glycogen storage disease due to acid maltase deficiency",
    "acid maltase deficiency": "Glycogen storage disease due to acid maltase deficiency",
    # Trichothiodystrophy eponyms / acronyms
    "Tay syndrome":            "Trichothiodystrophy",
    "IBIDS syndrome":          "Trichothiodystrophy",
    "IBIDS":                   "Trichothiodystrophy",
    # Liebenberg syndrome exact canonical name
    "Liebenberg syndrome":     "Brachydactyly-elbow wrist dysplasia syndrome",
    # Li-Fraumeni / FFI — exact Orphanet canonical names
    "SBLA syndrome":           "Li-Fraumeni syndrome",
    "FFI":                     "Fatal familial insomnia",
    # Weill-Marchesani: "Marchesani syndrome" is the common short form
    "Marchesani syndrome":     "Weill-Marchesani syndrome",
    # Tuberous sclerosis: Orphanet canonical name adds "complex"
    "tuberous sclerosis":      "Tuberous sclerosis complex",
    # DOOR → DOORS: Orphanet uses the plural acronym
    "DOOR syndrome":           "DOORS syndrome",
    # Wilson's disease: Orphanet canonical drops the apostrophe-S
    "Wilson's disease":        "Wilson disease",
    # Stiff man syndrome → Orphanet current canonical name
    "Stiff man syndrome":      "Stiff person spectrum disorder",
    "Stiff-man syndrome":      "Stiff person spectrum disorder",
    # Autosomal dominant Alzheimer's disease → Orphanet canonical
    "autosomal dominant Alzheimer's disease": "Early-onset autosomal dominant Alzheimer disease",
    # Fanconi anemia: apostrophe-S form used in some BioASQ questions
    "Fanconi's anemia":        "Fanconi anemia",
    # CPVT acronym → full Orphanet canonical name (prevents 11k-row no-filter dump)
    "CPVT":                    "Catecholaminergic polymorphic ventricular tachycardia",
    # COACH syndrome → Orphanet maps COACH under Joubert with hepatic involvement
    "COACH syndrome":          "Joubert syndrome with hepatic involvement",
    # Ohdo syndrome → Orphanet canonical (blepharophimosis subtype)
    "Ohdo syndrome":           "Blepharophimosis-intellectual disability syndrome, Ohdo type",
    # Friedreich's ataxia → Orphanet drops the apostrophe-S
    "Friedreich's ataxia":     "Friedreich ataxia",
    # Tourette's → Orphanet uses "Tourette syndrome" (no apostrophe-S)
    "Tourette's syndrome":     "Tourette syndrome",
    "Gilles de la Tourette":   "Tourette syndrome",
    # IFAP: abbreviation for "Ichthyosis follicularis-alopecia-photophobia syndrome".
    # Longer form first so the double-substitution guard fires before the shorter key.
    "IFAP syndrome":           "Ichthyosis follicularis-alopecia-photophobia syndrome",
    "IFAP":                    "Ichthyosis follicularis-alopecia-photophobia syndrome",
    # SNORD116: gene_master stores the cluster with the "@" suffix (SNORD116@).
    # Rewrite the plain symbol so fuzzy matching finds the canonical entry.
    "SNORD116":                "SNORD116@",
    # CDG: common abbreviation for "Congenital disorder of glycosylation" (ORPHA:137)
    "CDG":                     "Congenital disorder of glycosylation",
    # Short QT syndrome: Orphanet canonical name adds "Congenital" prefix (ORPHA:51083)
    "short QT syndrome":       "Congenital short QT syndrome",
    "Short QT syndrome":       "Congenital short QT syndrome",
}

# Re-sort phenotype rows by clinical frequency so the most relevant features
# surface first. Without this, rows arrive in ANN relevance order and the
# synthesizer's top-2 (for large result sets) may name low-frequency phenotypes.
_ORPHANET_SORT_ORDER = [
    {
        "col": "frequency",
        "order": [
            "Obligate (100%)",
            "Very frequent (99-80%)",
            "Frequent (79-30%)",
            "Occasional (29-5%)",
            "Very rare (<4-1%)",
            "Excluded (0%)",
        ],
    },
]

# Orphanet stores disease type numbers as Arabic ("type 2", "type 6") but the
# mapper LLM normalizes them to Roman ("type II", "type VI"). This narrow hook
# converts the filter_val AFTER expand so the substring match finds the right rows.
_ROMAN_TYPE_RE = re.compile(
    r'\btype\s+(I{1,3}V?|VI{0,3}|IX|IV|VIII|VII|VI|V|III|II|I)\b', re.IGNORECASE
)
_ROMAN_TO_ARABIC = {
    "I": "1", "II": "2", "III": "3", "IV": "4", "V": "5",
    "VI": "6", "VII": "7", "VIII": "8", "IX": "9",
}


async def _orphanet_narrow(ctx) -> None:
    fv = getattr(ctx, "filter_val", None)
    if not fv:
        return

    # Apply schema_kg plan narrowing (normally handled by generic _post_expand
    # but skipped when a narrow hook is present). This sets ctx.out_cols from
    # the schema_mapper's output_plan so only relevant columns are selected —
    # without this, ctx.out_cols includes all ~30 Orphanet schema columns.
    sk_plan = (ctx.extras or {}).get("sk_plan")

    # Keyword overrides: schema_mapper's ANN-driven few-shot examples
    # nondeterministically select wrong tables for these query patterns.
    # Hard override ensures the correct table is always used.
    if sk_plan:
        _q = (getattr(ctx.input, "cleaned_query", "") or "").lower()
        _plan_tables = set(sk_plan.get("plan_tables") or [])

        # "What is the inheritance pattern of X?" → disease_onset_inheritance ONLY.
        # Mapper nondeterministically picks disease_master (returns disease names, not inheritance)
        # or disease_onset_inheritance (correct). Hard override ensures consistent results.
        _doi = "disease_onset_inheritance_orphanet"
        if ("inheritance pattern" in _q or "mode of inheritance" in _q) and _doi not in _plan_tables:
            ctx.log.info("[orphanet] inheritance keyword override: injecting disease_onset_inheritance_orphanet")
            sk_plan["plan_tables"] = {_doi, "disease_master_table_orphanet"}
            sk_plan["join_path"] = [("disease_master_table_orphanet", _doi, "disease_id")]
            _op = sk_plan.setdefault("output_plan", {})
            _op.clear()
            _op[_doi] = ["attribute", "value"]
            _fp = sk_plan.setdefault("filter_plan", {})
            _fp.clear()
            _fp["disease_master_table_orphanet"] = {}
            _dvals = ctx.filter_val.get("disease_name") or []
            if isinstance(_dvals, list):
                _dvals = _dvals[:1]
            if _dvals:
                _fp["disease_master_table_orphanet"]["disease_name"] = _dvals
                ctx.filter_val["disease_name"] = _dvals
            _pv = sk_plan.setdefault("parsed_value", {})
            _pv.update({"attribute": "requested", "value": "requested"})

        # "Is X genetically/clinically heterogeneous?" → gene_disease_association ONLY.
        # Two mapper failure modes:
        #   (A) disease_master alone — no gene data, always returns "No"
        #   (B) disease_master + gene_disease + disease_phenotype + gene_master → cross-join
        #       (23 genes × 106 phenotypes = 2438 rows) with mixed-column synthesizer output
        # Must update BOTH plan_tables AND join_path — to_production_plan uses join_path
        # to determine BFS order; stale join_path silently produces the wrong tables.
        elif "heterogen" in _q:
            ctx.log.info("[orphanet] heterogeneity keyword override: injecting gene_disease_association_orphanet")
            sk_plan["plan_tables"] = {"gene_disease_association_orphanet",
                                      "disease_master_table_orphanet"}
            sk_plan["join_path"] = [("disease_master_table_orphanet",
                                     "gene_disease_association_orphanet", "disease_id")]
            _op = sk_plan.setdefault("output_plan", {})
            _op.clear()
            _op["gene_disease_association_orphanet"] = ["gene_symbol", "gene_disease_association_type"]
            _fp = sk_plan.setdefault("filter_plan", {})
            _fp.clear()
            _fp["disease_master_table_orphanet"] = {}
            _dvals = ctx.filter_val.get("disease_name") or []
            if _dvals:
                if isinstance(_dvals, list):
                    _dvals = _dvals[:1]  # use only primary disease — prevents cross-product
                _fp["disease_master_table_orphanet"]["disease_name"] = _dvals
                ctx.filter_val["disease_name"] = _dvals
            sk_plan.setdefault("parsed_value", {}).update({"gene_symbol": "requested"})

        # "List comorbidities / co-morbid conditions" → disease_phenotype_association
        # ANN maps this to disease_natural_history (classification hierarchy) which
        # holds sub-types, not phenotypes. Force phenotype table + disease_master.
        elif "comorbid" in _q or "co-morbid" in _q:
            ctx.log.info("[orphanet] comorbidity keyword override: injecting disease_phenotype_association_orphanet")
            sk_plan["plan_tables"] = {"disease_phenotype_association_orphanet",
                                      "disease_master_table_orphanet"}
            sk_plan["join_path"] = [("disease_master_table_orphanet",
                                     "disease_phenotype_association_orphanet", "disease_id")]
            _op = sk_plan.setdefault("output_plan", {})
            _op.clear()
            _op["disease_phenotype_association_orphanet"] = ["hpo_term", "frequency"]
            _fp = sk_plan.setdefault("filter_plan", {})
            _fp.clear()
            _fp["disease_master_table_orphanet"] = {}
            # Use primary sk_plan parsed_value disease_name (canonical term); avoid
            # filter_val which may have been expanded to 3 sub-types by natural_history
            _pv_dname = (sk_plan.get("parsed_value") or {}).get("disease_name")
            _dvals = (_pv_dname if isinstance(_pv_dname, list) else [_pv_dname]) if _pv_dname else []
            if not _dvals:
                _dvals = ctx.filter_val.get("disease_name") or []
                if isinstance(_dvals, list):
                    _dvals = _dvals[:1]  # only primary disease
            ctx.log.info("[orphanet] comorbidity override disease_name=%r", _dvals)
            if _dvals:
                _fp["disease_master_table_orphanet"]["disease_name"] = _dvals
                ctx.filter_val["disease_name"] = _dvals
            _pv = sk_plan.setdefault("parsed_value", {})
            _pv.update({"hpo_term": "requested", "frequency": "requested"})

        # "differential diagnosis" → disease_phenotype_association (HPO phenotypes for the
        # queried disease). Mapper nondeterministically picks disease_natural_history (holds
        # only parent-child hierarchy, not diagnostic differentials). Phenotypes give the
        # synthesizer the clinical features needed to reason about overlap with other diseases.
        elif "differential" in _q:
            ctx.log.info("[orphanet] differential-diagnosis override: injecting disease_phenotype_association_orphanet")
            sk_plan["plan_tables"] = {"disease_phenotype_association_orphanet",
                                      "disease_master_table_orphanet"}
            sk_plan["join_path"] = [("disease_master_table_orphanet",
                                     "disease_phenotype_association_orphanet", "disease_id")]
            _op = sk_plan.setdefault("output_plan", {})
            _op.clear()
            _op["disease_phenotype_association_orphanet"] = ["hpo_term", "frequency"]
            _fp = sk_plan.setdefault("filter_plan", {})
            _fp.clear()
            _fp["disease_master_table_orphanet"] = {}
            _dvals = ctx.filter_val.get("disease_name") or []
            if isinstance(_dvals, list):
                _dvals = _dvals[:1]
            if _dvals:
                _fp["disease_master_table_orphanet"]["disease_name"] = _dvals
                ctx.filter_val["disease_name"] = _dvals
            sk_plan.setdefault("parsed_value", {}).update({"hpo_term": "requested", "frequency": "requested"})

        # "what tissue is affected/involved" → disease_classification_orphanet so
        # disorder_type and parent_disease_name surface the connective-tissue / organ-system
        # class of the disease (e.g. "Connective tissue disorder" for Marfan syndrome).
        # Mapper picks disease_phenotype_association (individual HPO terms) which never
        # names the tissue category.
        elif "tissue" in _q:
            ctx.log.info("[orphanet] tissue override: injecting disease_classification_orphanet")
            sk_plan["plan_tables"] = {"disease_classification_orphanet",
                                      "disease_master_table_orphanet"}
            sk_plan["join_path"] = [("disease_master_table_orphanet",
                                     "disease_classification_orphanet", "disease_id")]
            _op = sk_plan.setdefault("output_plan", {})
            _op.clear()
            _op["disease_classification_orphanet"] = ["classification", "disorder_type", "parent_disease_name"]
            _fp = sk_plan.setdefault("filter_plan", {})
            _fp.clear()
            _fp["disease_master_table_orphanet"] = {}
            _dvals = ctx.filter_val.get("disease_name") or []
            if isinstance(_dvals, list):
                _dvals = _dvals[:1]
            if _dvals:
                _fp["disease_master_table_orphanet"]["disease_name"] = _dvals
                ctx.filter_val["disease_name"] = _dvals
            sk_plan.setdefault("parsed_value", {}).update({
                "classification": "requested",
                "disorder_type": "requested",
                "parent_disease_name": "requested",
            })

        # "tumor / extracolonic / neoplasm" queries → need HPO phenotype terms, not just
        # disease names. Two paths depending on what the mapper resolved:
        #   (A) disease path: filter_val has disease_name → disease_master → phenotype
        #   (B) gene path: filter_val has gene_symbol → gene_disease → disease_master → phenotype
        # Without this override the mapper returns only disease_name rows (Lynch syndrome),
        # never the tumor-type HPO terms the synthesizer needs to answer "what tumor types".
        elif any(kw in _q for kw in ("tumor", "extracolonic", "neoplasm")):
            _dpa = "disease_phenotype_association_orphanet"
            _dm  = "disease_master_table_orphanet"
            _gda = "gene_disease_association_orphanet"
            _dvals = ctx.filter_val.get("disease_name") or []
            if isinstance(_dvals, list):
                _dvals = _dvals[:1]
            _gvals = ctx.filter_val.get("gene_symbol") or []
            if isinstance(_gvals, list):
                _gvals = _gvals[:1]
            if _dvals:
                ctx.log.info("[orphanet] tumor override (disease path): disease=%r", _dvals)
                sk_plan["plan_tables"] = {_dpa, _dm}
                sk_plan["join_path"] = [(_dm, _dpa, "disease_id")]
                _op = sk_plan.setdefault("output_plan", {})
                _op.clear()
                _op[_dpa] = ["hpo_term", "frequency"]
                _op[_dm]  = ["disease_name"]
                _fp = sk_plan.setdefault("filter_plan", {})
                _fp.clear()
                _fp[_dm] = {"disease_name": _dvals}
                ctx.filter_val["disease_name"] = _dvals
                sk_plan.setdefault("parsed_value", {}).update({"hpo_term": "requested", "frequency": "requested"})
            elif _gvals:
                ctx.log.info("[orphanet] tumor override (gene path): gene=%r", _gvals)
                sk_plan["plan_tables"] = {_gda, _dm, _dpa}
                sk_plan["join_path"] = [(_gda, _dm, "disease_id"), (_dm, _dpa, "disease_id")]
                _op = sk_plan.setdefault("output_plan", {})
                _op.clear()
                _op[_dpa] = ["hpo_term", "frequency"]
                _op[_dm]  = ["disease_name"]
                _fp = sk_plan.setdefault("filter_plan", {})
                _fp.clear()
                _fp[_gda] = {"gene_symbol": _gvals}
                ctx.filter_val["gene_symbol"] = _gvals
                sk_plan.setdefault("parsed_value", {}).update({"hpo_term": "requested", "frequency": "requested"})

    if sk_plan and sk_plan.get("plan_tables"):
        from config.schema import database_schemas as _db_schemas
        schema_cols = {c for tbl in _db_schemas.get("orphanet", {}).values() for c in tbl}
        out_cols_set: set = set()
        filter_cols_set: set = set()
        for cols in sk_plan.get("output_plan", {}).values():
            out_cols_set.update(cols)
        for col, val in (sk_plan.get("parsed_value") or {}).items():
            if val == "requested":
                out_cols_set.add(col)
        for cols_map in sk_plan.get("filter_plan", {}).values():
            filter_cols_set.update(cols_map.keys())
        out_cols_set &= schema_cols
        filter_cols_set &= schema_cols
        plan_cols = out_cols_set | filter_cols_set
        if plan_cols:
            filter_only = filter_cols_set - out_cols_set
            ctx.out_cols = sorted(out_cols_set) + sorted(filter_only)
            # Always include `frequency` when phenotype data is in the plan so
            # _apply_sort_order can apply the frequency-tier ranking.  Without
            # this the column is dropped before the sort and rows arrive in
            # arbitrary parquet order.
            plan_tables = sk_plan.get("plan_tables") or []
            if any("disease_phenotype_association" in str(t) for t in plan_tables):
                if "frequency" not in ctx.out_cols and "frequency" in schema_cols:
                    ctx.out_cols.append("frequency")
            # Include inheritance `attribute`/`value` when the onset_inheritance
            # table is in the plan — they're not in CommonFields so expand drops
            # them but the filter_plan restore in _post_expand re-injects the
            # filter; we need them as output columns too.
            if any("disease_onset_inheritance" in str(t) for t in plan_tables):
                for _extra in ("attribute", "value"):
                    if _extra not in ctx.out_cols and _extra in schema_cols:
                        ctx.out_cols.append(_extra)
            new_fv = dict(ctx.filter_val)
            for k, v in list(new_fv.items()):
                if v == "requested" and k not in plan_cols:
                    new_fv[k] = None
            ctx.filter_val = new_fv

    # Roman→Arabic disease-type normalisation (e.g. "type II" → "type 2").
    disease_vals = ctx.filter_val.get("disease_name")
    if not disease_vals or not isinstance(disease_vals, list):
        return

    def _to_arabic(m: re.Match) -> str:
        roman = m.group(1).upper()
        return f"type {_ROMAN_TO_ARABIC.get(roman, roman)}"

    new_vals, changed = [], False
    for val in disease_vals:
        normalized = _ROMAN_TYPE_RE.sub(_to_arabic, val)
        new_vals.append(normalized)
        if normalized != val:
            changed = True

    if changed:
        ctx.filter_val["disease_name"] = new_vals
        ctx.log.info("[orphanet] narrow: Roman→Arabic disease type: %r → %r",
                     disease_vals, new_vals)


_ORPHANET_CONFIG = SchemaKgConfig(
    db=SERVICE_NAME,
    display_name=DB_NAME,
    get_db=get_orphanet_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
    capabilities=_ORPHANET_CAPABILITIES,
    limitations=_ORPHANET_LIMITATIONS,
    term_rewrite=_ORPHANET_TERM_REWRITE,
    sort_order=_ORPHANET_SORT_ORDER,
    narrow=_orphanet_narrow,
)

return_orphanet_result = make_schema_kg_handler(_ORPHANET_CONFIG)
