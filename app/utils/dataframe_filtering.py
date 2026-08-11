

# dataframe_filtering.py

"""
Production-grade database join and filter operations.

This module provides memory-efficient joining and filtering of database tables
according to query plans, with strict validation and cross-join detection.

Updates (high-value, low-risk):
1) Cached cardinality estimates (per-query, concurrency-safe via ContextVar)
2) Better root selection (choose smallest root table by estimated rows)
"""

import os
import re
import logging
from typing import Any, Dict, List, Tuple, Optional, Set
from functools import reduce
from dataclasses import dataclass
from contextvars import ContextVar

_HGVS_STRIP_RE = re.compile(r"(dup|del|ins)[acgt]+$", flags=re.IGNORECASE)


class NoFilterTermsError(ValueError):
    """Raised when entity expansion produced zero usable filter terms.

    Distinct from a generic ValueError so callers (e.g. per_db_tool's
    orchestrator) can degrade this to a "no rows matched"-style result
    instead of surfacing it as a hard query failure — the query was
    understood, it just couldn't be matched to a specific DB entity.
    """

# Generic English singular/plural counterpart for a substring-match term (e.g.
# question phrasing "gliomas" vs. ClinVar's stored singular "glioma", or vice
# versa). Deliberately conservative: skip endings where blind "-s" stripping
# produces a wrong stem (e.g. "sepsis", "nucleus", "class").
_PLURAL_UNSAFE_SUFFIXES = ("ss", "us", "is", "os")


def _plural_variant(v: str) -> Optional[str]:
    if v.endswith("s") and len(v) > 3 and not v.endswith(_PLURAL_UNSAFE_SUFFIXES):
        return v[:-1]
    if not v.endswith("s") and v and v[-1].isalpha():
        return v + "s"
    return None


# Standard IUPAC single-letter -> three-letter amino acid code. This is a
# fixed, gene-agnostic biochemistry convention (20 canonical residues + the
# stop codon) — NOT a per-gene or per-variant lookup table. Used to translate
# shorthand protein-change notation a user types (e.g. "V600E", "T790M",
# bare "C634") into the three-letter token embedded in the HGVS p.-notation
# ClinVar (and other DBs) store inside variant_name, e.g.
# "NM_004333.6(BRAF):c.1799T>A (p.Val600Glu)". Without this translation, a
# bare one-letter shorthand never substring-matches the stored three-letter
# HGVS string and silently zeroes the query.
_AA_1TO3 = {
    "A": "ala", "R": "arg", "N": "asn", "D": "asp", "C": "cys",
    "Q": "gln", "E": "glu", "G": "gly", "H": "his", "I": "ile",
    "L": "leu", "K": "lys", "M": "met", "F": "phe", "P": "pro",
    "S": "ser", "T": "thr", "W": "trp", "Y": "tyr", "V": "val",
    "*": "ter", "X": "ter",
}

# Shorthand protein-change notation, with or without a leading "p.":
#   v600e   -> substitution (ref=v, pos=600, alt=e)
#   c634    -> position-only / hotspot cluster (ref=c, pos=634, no alt)
#   r213*   -> nonsense (ref=r, pos=213, alt=*)
# Anchored on the full (already-lowercased) token so it never fires on a
# free-text term that merely contains a letter+digit run.
_PROTEIN_SHORTHAND_RE = re.compile(r"^(?:p\.?\s*)?([a-z])(\d+)([a-z*]{0,3})$")


def _protein_shorthand_regex(v: str) -> Optional[str]:
    """Build a ready-to-use (already-anchored) regex matching the three-letter
    HGVS p.-notation form of a one-letter protein-change shorthand token.

    Returns None when `v` isn't shaped like protein shorthand (HGVS c./g.
    notation, rsIDs, disease names, etc. all fail the match and fall through
    to the existing plain substring patterns unchanged).
    """
    m = _PROTEIN_SHORTHAND_RE.match(v.strip())
    if not m:
        return None
    ref, pos, alt = m.groups()
    ref3 = _AA_1TO3.get(ref.upper())
    if not ref3:
        return None
    if not alt:
        # No target residue named (e.g. a bare hotspot codon like "C634") —
        # match the reference residue+position followed by ANY three-letter
        # amino acid code, i.e. any substitution reported at that position.
        return r"\b" + ref3 + pos + r"[a-z]{3}\b"
    alt3 = _AA_1TO3.get(alt.upper())
    if not alt3:
        return None
    return r"\b" + ref3 + pos + alt3 + r"\b"


import polars as pl

logger = logging.getLogger(__name__)

# Configuration
MAX_UNIQUE_ROWS = int(os.getenv("MAX_UNIQUE_ROWS", "1000000"))
JOIN_BATCH_SIZE = int(os.getenv("JOIN_BATCH_SIZE", "100000"))
STRICT_JOIN_MODE = os.getenv("STRICT_JOIN_MODE", "true").lower() == "true"
# Raised 2026-05-15 from 5_000 to 100_000. The lower bound was originally a
# defensive cap against accidental cartesian joins, but legitimate biomedical
# fan-outs (one gene → thousands of bioactivity rows in chembl/ttd, one drug
# → thousands of clinical-trial rows in ctd, etc.) routinely cross 5_000×
# expansion and were being rejected with a confusing "Set CROSS_JOIN_THRESHOLD
# higher" error. Several services were already overriding to 100_000 in
# compose; making this the default eliminates the inconsistency. The
# cartesian-join detector still fires on truly pathological cases via
# MAX_RESULT_SIZE (10_000_000).
CROSS_JOIN_THRESHOLD = float(os.getenv("CROSS_JOIN_THRESHOLD", "100000"))
MAX_RESULT_SIZE = int(os.getenv("MAX_RESULT_SIZE", "10000000"))
ENABLE_STREAMING = os.getenv("ENABLE_STREAMING", "true").lower() == "true"

# Per-query cardinality cache (concurrency-safe)
_CARDINALITY_CACHE: ContextVar[Dict[int, int]] = ContextVar("_CARDINALITY_CACHE", default={})

@dataclass
class FilterStat:
    column: str
    input_values: list
    rows_before: int
    rows_after: int
    table: str = ""


@dataclass
class JoinMetrics:
    """Metrics for monitoring join operations."""
    pre_join_rows: int
    post_join_rows: int
    parent_table: str
    child_table: str

    @property
    def explosion_factor(self) -> float:
        """Calculate how much the join exploded the data."""
        if self.pre_join_rows == 0:
            return 0.0
        return self.post_join_rows / self.pre_join_rows

    @property
    def is_suspicious(self) -> bool:
        """Check if join looks like a cross-join or data explosion."""
        return self.explosion_factor > CROSS_JOIN_THRESHOLD


class DatabaseJoinError(Exception):
    """Base exception for database join operations."""
    pass


class CrossJoinDetectedError(DatabaseJoinError):
    """Detected a suspicious cross-join or data explosion."""
    pass


class MissingJoinError(DatabaseJoinError):
    """Required join columns not found."""
    pass



def estimate_cardinality(df: pl.LazyFrame) -> int:
    """
    Estimate row count for a LazyFrame efficiently, with per-query caching.

    NOTE:
    - This calls collect() for count, so caching is critical.
    - Cache is stored in a ContextVar, so concurrent requests don't conflict.

    The cache value is ``(count, df_ref)`` — the second slot holds a strong
    reference to the LazyFrame so its ``id()`` cannot be reused while the
    entry is live. Without that, a transient LazyFrame can be GC'd, a fresh
    one allocated at the same memory address, and the lookup would return a
    stale count — corrupting MAX_RESULT_SIZE guards and join-explosion
    detection. The cache is reset per query in `join_and_filter_database`
    (`_CARDINALITY_CACHE.set({})`), so the retained refs are released at the
    end of every request.
    """
    cache = _CARDINALITY_CACHE.get()
    df_id = id(df)

    entry = cache.get(df_id)
    if entry is not None:
        return entry[0]

    try:
        count = int(df.select(pl.len()).collect().item())
        cache[df_id] = (count, df)
        return count
    except Exception as e:
        logger.warning(f"Could not estimate cardinality: {e}")
        cache[df_id] = (-1, df)
        return -1


def validate_join_columns(
    left_schema: Dict[str, Any],
    right_schema: Dict[str, Any],
    left_on: List[str],
    right_on: List[str],
    parent_table: str,
    child_table: str
) -> None:
    """
    Validate that join columns exist in both tables.
    """
    for col in left_on:
        if col not in left_schema:
            raise MissingJoinError(
                f"Join column '{col}' not found in parent table '{parent_table}'. "
                f"Available columns: {sorted(left_schema.keys())}"
            )

    for col in right_on:
        if col not in right_schema:
            raise MissingJoinError(
                f"Join column '{col}' not found in child table '{child_table}'. "
                f"Available columns: {sorted(right_schema.keys())}"
            )


def detect_cross_join(metrics: JoinMetrics) -> None:
    """
    Detect and warn about suspicious joins.
    """
    if metrics.is_suspicious:
        msg = (
            f"Suspicious join detected: {metrics.parent_table} -> {metrics.child_table}. "
            f"Rows exploded from {metrics.pre_join_rows:,} to {metrics.post_join_rows:,} "
            f"({metrics.explosion_factor:.2f}x increase). "
            f"This may indicate a cross-join or missing filter. "
            f"Set CROSS_JOIN_THRESHOLD higher to allow this."
        )
        logger.error(msg)
        raise CrossJoinDetectedError(msg)

    if metrics.explosion_factor > 1.0:
        logger.info(
            f"Join {metrics.parent_table} -> {metrics.child_table}: "
            f"{metrics.pre_join_rows:,} -> {metrics.post_join_rows:,} rows "
            f"({metrics.explosion_factor:.2f}x)"
        )


def optimize_join_order(
    remaining_tables: Set[str],
    joined_tables: Set[str],
    parents: Dict[str, Optional[str]],
    pre_filtered_dfs: Dict[str, pl.LazyFrame]
) -> List[str]:
    """
    Optimize join order by preferring smaller tables first,
    subject to the tree constraint (parent must already be joined).
    """
    ready_tables = [
        t for t in remaining_tables
        if parents.get(t) in joined_tables
    ]

    if not ready_tables:
        return []

    table_sizes = []
    for table in ready_tables:
        size = estimate_cardinality(pre_filtered_dfs[table])
        table_sizes.append((table, size))

    # Sort by size (ascending), with -1 (unknown) at the end.
    # Final tie-break by table name to avoid nondeterminism from set iteration.
    table_sizes.sort(key=lambda x: (x[1] < 0, x[1], x[0]))
    ordered = [t for t, _ in table_sizes]

    logger.debug(f"Join order for next batch: {ordered}")
    return ordered


def _reroot_join_tree(parents: Dict[str, Optional[str]], new_root: str) -> Dict[str, Optional[str]]:
    """Re-root the join tree at ``new_root`` by reversing the parent pointers
    along the path from ``new_root`` up to the current root. Branches off that
    path keep their parents (still correctly oriented relative to the new root).

    For INNER joins the join RESULT is identical regardless of which table is the
    root, but rooting at the most-selective (smallest post-filter) table avoids
    intermediate join explosions — e.g. "chemicals that increase TNF" rooting on
    chemical_master (179k, unfiltered) instead of gene_master[TNF]=1 row, which
    blows chemical_master×chemical_gene up to 268M rows. Join keys are symmetric
    (full_jk holds both directions), so reversing edge direction is safe.
    """
    p = dict(parents)
    cur, prev, seen = new_root, None, set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        nxt = p.get(cur)
        p[cur] = prev
        prev, cur = cur, nxt
    return p


def perform_join_with_validation(
    join_chain: pl.LazyFrame,
    right_df: pl.LazyFrame,
    left_on: List[str],
    right_on: List[str],
    parent_table: str,
    child_table: str,
    how: str = "inner",
) -> Tuple[pl.LazyFrame, JoinMetrics]:
    """
    Perform join with validation and metrics collection.

    `how` defaults to "inner". Pass "left" for decoration / identifier-bridge
    child tables (configured via config.schema.database_decoration_tables) so
    entities missing from the bridge get NULL columns instead of being dropped.
    """
    pre_join_rows = estimate_cardinality(join_chain)

    if left_on == right_on:
        result = join_chain.join(right_df, on=left_on, how=how)
    else:
        result = join_chain.join(right_df, left_on=left_on, right_on=right_on, how=how)

    post_join_rows = estimate_cardinality(result)

    metrics = JoinMetrics(
        pre_join_rows=pre_join_rows,
        post_join_rows=post_join_rows,
        parent_table=parent_table,
        child_table=child_table
    )

    detect_cross_join(metrics)
    return result, metrics


def fast_filter_dataframe(
    df: pl.LazyFrame,
    filters: Dict[str, Any],
    filter_stats: Optional[list] = None,
    table_name: Optional[str] = None,
) -> pl.LazyFrame:

    """
    Apply filters to LazyFrame efficiently.

    Special handling:
      - target_name, gene_name: Combined with OR
      - Other columns: Combined with AND
    """
    if not filters or not any(filters.values()):
        return df

    # Defensive: the interpreter (LLM) sometimes emits a single value as a
    # bare string instead of a one-element list. Without this coercion every
    # such filter is silently dropped below (the `isinstance(_, list)` guard),
    # which turns a perfectly valid query into a whole-table scan or empty
    # result. Sentinels and Nones are preserved as-is.
    _SENTINELS = {"requested"}
    coerced: Dict[str, Any] = {}
    for _k, _v in filters.items():
        if isinstance(_v, str):
            _sv = _v.strip()
            if _sv and _sv.lower() not in _SENTINELS:
                coerced[_k] = [_v]
            else:
                coerced[_k] = _v
        elif isinstance(_v, list):
            # Strip any sentinel entries inside the list so a value like
            # ["requested"] (produced upstream by mirror_columns on the bare
            # sentinel) doesn't get literal-matched against real names and
            # reduce the table to zero rows.
            cleaned = [x for x in _v
                       if not (isinstance(x, str) and x.strip().lower() in _SENTINELS)]
            coerced[_k] = cleaned
        else:
            coerced[_k] = _v
    filters = coerced

    or_columns = ["target_name", "gene_name"]
    or_masks = []
    and_mask = pl.lit(True)

    schema = df.schema


    # OR columns
    for col in or_columns:
        if col not in filters or col not in schema:
            continue

        filter_val = filters[col]
        if not isinstance(filter_val, list) or not filter_val:
            continue

        col_type = schema[col]
        if col_type not in (pl.Utf8, pl.String, pl.Categorical):
            logger.warning(
                f"Column '{col}' has type {col_type}, expected string type. "
                f"Skipping filter for this column."
            )
            continue

        vals_lower = [str(v).lower() for v in filter_val if v]
        if vals_lower:
            or_masks.append(pl.col(col).str.to_lowercase().is_in(vals_lower))

    # ── Numeric range filter ──────────────────────────────────────────────────
    # Columns in _NUMERIC_RANGE_COLS pair with an operator field from
    # ParsedValue (e.g. activity_value + activity_operator).  The operator is
    # the USER's constraint direction (<, >, <=, >=, =) and is NOT applied as a
    # string equality filter on the operator column in the DB.  Both the value
    # col and the operator col are skipped in the AND string loop below.
    _NUMERIC_RANGE_COLS: dict[str, str] = {
        "activity_value": "activity_operator",  # TTD IC50 / Ki / EC50 (nM)
    }
    _numeric_handled: set[str] = set()

    for _num_col, _op_field in _NUMERIC_RANGE_COLS.items():
        if _num_col not in filters or _num_col not in schema:
            continue

        _col_type = schema[_num_col]
        if _col_type not in (pl.Float32, pl.Float64, pl.Int32, pl.Int64,
                              pl.UInt32, pl.UInt64):
            continue  # column is string in this table — falls through to string loop

        _fv = filters[_num_col]
        if not isinstance(_fv, list) or not _fv:
            continue  # "requested" sentinel (string) or empty — no numeric filter

        _thresholds: list[float] = []
        for _v in _fv:
            try:
                _thresholds.append(float(_v))
            except (ValueError, TypeError):
                pass
        if not _thresholds:
            continue

        _threshold = _thresholds[0]

        # Operator from ParsedValue; default ≤ (sensible for IC50 queries)
        _op_val = filters.get(_op_field)
        if isinstance(_op_val, list):
            _op_val = _op_val[0] if _op_val else None
        _op_str = str(_op_val or "<=").strip()

        if _op_str in ("<", "lt"):
            _pred = pl.col(_num_col) < _threshold
        elif _op_str in (">", "gt"):
            _pred = pl.col(_num_col) > _threshold
        elif _op_str in ("<=", "lte", "le", "≤"):
            _pred = pl.col(_num_col) <= _threshold
        elif _op_str in (">=", "gte", "ge", "≥"):
            _pred = pl.col(_num_col) >= _threshold
        elif _op_str in ("=", "==", "eq"):
            _pred = pl.col(_num_col) == _threshold
        else:
            _pred = pl.col(_num_col) <= _threshold

        _nb = estimate_cardinality(df)
        df = df.filter(_pred)
        _na = estimate_cardinality(df)

        if filter_stats is not None:
            filter_stats.append(
                FilterStat(
                    column=_num_col,
                    input_values=[f"{_op_str}{_threshold}"],
                    rows_before=_nb,
                    rows_after=_na,
                    table=table_name or "",
                )
            )

        _numeric_handled.add(_num_col)
        _numeric_handled.add(_op_field)

    # AND columns
    # Columns whose stored value is a complex string (HGVS notation, free-text
    # variant descriptions, fusion-pair strings) which the user typically queries
    # by SUBSTRING rather than exact equality. e.g. ClinVar stores variant_name
    # as `NM_007294.3(BRCA1):c.5266dupC`; the user asks "c.5266dupC".
    _SUBSTRING_MATCH_COLS = {
        "variant_name", "hgvs",
        # HGNC: prev_symbol/alias_symbol/prev_name/alias_name store
        # pipe-delimited lists (e.g. "NCRNA00181|A1BGAS|A1BG-AS").
        "prev_symbol", "alias_symbol", "prev_name", "alias_name",
        # Round-8 (2026-05-16): disease_name now SUBSTRING-matched so
        # rare-disease subtype queries work. Orphanet / HPO store
        # subtype-numbered names like "Joubert syndrome 1", "Joubert
        # syndrome 16" — exact-match for "Joubert syndrome" returned 0
        # rows; substring-match returns the entire subtype family.
        # Same fix applies to "Marfan syndrome type 1/2", "Charcot-
        # Marie-Tooth disease type 1A/2A/...", "MODY 1/2/3", etc.
        "disease_name",
        # 2026-05-23: TTD drug_synonyms_association.synonym stores trade
        # names with a literal " (TN)" suffix (e.g. "Gleevec (TN)") and
        # mixes alias formats (CHEMBL IDs, NSC codes, BRN numbers, IUPAC
        # partial names, CAS-like digit strings). Exact-equality on
        # "Gleevec" returns 0 rows; substring-match returns the trade-
        # name row. Same rationale as disease_name above.
        "synonym",
        # 2026-06-29: CTD synonym association tables use table-specific
        # column names (chemical_synonym / gene_synonym / disease_synonym)
        # to avoid Steiner planner column-uniqueness violations. The physical
        # parquet column "synonym" is renamed at load time; these logical
        # names need the same substring-match behaviour as "synonym".
        "chemical_synonym", "gene_synonym", "disease_synonym",
        # 2026-06-22: CTD exposure/phenotype packed columns store multiple
        # entries per cell in 'name^id^source' caret format, pipe-joined
        # (e.g. 'Nitrates^D009566^MESH|perchlorate^C494474^MESH'). Exact is_in
        # can NEVER equal a packed cell, so these columns returned 0 rows for
        # EVERY query. Substring-match the literal term against the name
        # segment, exactly as the schema descriptions already document.
        "diseases", "phenotypes", "exposure_stressors", "exposure_markers",
        "co_mentioned_terms", "anatomy_terms", "inference_genes",
        # 2026-06-23: MSigDB geneset_name — source DB/ontology is a NAME PREFIX
        # (KEGG_, REACTOME_, GOBP_, WP_, …). Source-DB queries ('KEGG pathways')
        # inject a prefix token (e.g. 'KEGG_') via the msigdb pre_join hook; only
        # substring matching turns that into the full set of that source's gene
        # sets. Only MSigDB carries geneset_name, so this is scoped to it.
        "geneset_name",
        # 2026-06-24: `source` stores a full provenance URL (HPO/CTD), e.g.
        # 'http://www.orphadata.org/data/xml/en_product6.xml' or
        # 'ftp://ftp.ncbi.nlm.nih.gov/gene/DATA/mim2gene_medgen'. Users name the
        # source as "Orphanet"/"OMIM"/"orphadata"/"mim2gene" — exact-equality
        # never matches the URL. Substring-match the resolved token against the
        # URL. (Pair with a schema_rules grounding note mapping the friendly
        # name to the URL substring, e.g. Orphanet→orphadata, OMIM→mim2gene.)
        "source",
        # ClinVar: packed SO codes; users query partial terms like 'missense variant'
        "molecular_consequence",
        # ClinVar: underscore-encoded disease names
        "clndn",
        # ClinVar (2026-07-04): variant_disease_name stores compound MedGen/OMIM
        # names with qualifiers the user's phrase rarely reproduces verbatim
        # (e.g. "Disabling pansclerotic morphea OF CHILDHOOD", "...TYPE 1").
        # Same rationale as disease_name above — exact-equality returned 0 rows
        # whenever the expand/ANN step didn't resolve to the full stored string.
        "variant_disease_name",
        # Orphanet: long association type phrases; users query partial terms
        "gene_disease_association_type",
        # Orphanet: xref mapping codes prefixed with description; users query bare code 'E', 'NTBT', etc.
        "mapping_relation",
        # UniProt protein_function_uniprot: CC FUNCTION free text; users ask about role/activity/mechanism
        "function_text",
        # UniProt protein_interaction_uniprot: SUBUNIT free text and BINARY CC lines
        "description",
        # UniProt ptm_sites_uniprot: enzymes stores compound values (e.g. "PKA and PKB/AKT1");
        # users query partial enzyme names (e.g. "PKA", "CK1") that are substrings of the full value.
        "enzymes",
    }

    # ClinVar (2026-07-04): aggregate/disease/submission clinical_significance columns
    # store MULTI-COMPONENT ClinVar consensus calls, not just the single-tier enum
    # values documented in schema_grounding_notes. ClinVar joins conflicting-tier
    # classifications with '/' (e.g. 'pathogenic/likely pathogenic' — 39,700+ rows),
    # appends descriptor flags with ';' (e.g. 'pathogenic/likely pathogenic; risk
    # factor'), and appends qualifiers with ',' (e.g. 'pathogenic, low penetrance').
    # Plain equality (`is_in`) on the whole string only matches variants with a
    # bare single-tier call and silently drops every compound-labelled variant from
    # a "pathogenic" (or "likely pathogenic" / "risk factor" / ...) filter — this
    # is how clinically critical variants like JAK2 p.Val617Phe ('pathogenic/likely
    # pathogenic') and LRRK2 p.Gly2019Ser ('pathogenic/likely pathogenic; risk
    # factor') vanished from "is X pathogenic" queries even though 100% present in
    # the underlying data. Match per-COMPONENT (split on '/', ';', ',') rather than
    # per-substring so distinct tiers stay distinct (a 'benign' filter must NOT
    # match 'likely benign', and vice versa) while compound calls still match on
    # whichever component(s) they actually contain.
    _CLINSIG_COMPONENT_MATCH_COLS = {
        "aggregate_clinical_significance",
        "disease_clinical_significance",
        "submission_clinical_significance",
    }
    _CLINSIG_SPLIT_RE = re.compile(r"[/;,]")

    for col, filter_val in filters.items():
        if col in or_columns:
            continue
        if col in _numeric_handled:
            continue
        if col not in schema:
            continue
        if not isinstance(filter_val, list) or not filter_val:
            continue

        col_type = schema[col]
        if col_type not in (pl.Utf8, pl.String, pl.Categorical):
            logger.warning(
                f"Column '{col}' has type {col_type}, expected string type. "
                f"Skipping filter for this column."
            )
            continue

        vals_lower = [str(v).lower() for v in filter_val if v]
        if vals_lower:
            before = estimate_cardinality(df)

            if col in _CLINSIG_COMPONENT_MATCH_COLS:
                # OR over per-component exact matches (case-insensitive). Anchor each
                # value to a full '/' or ';' or ',' delimited token (start/end of
                # string or a delimiter on both sides) so 'pathogenic' matches the
                # 'pathogenic' component of 'pathogenic/likely pathogenic; risk
                # factor' but does NOT match the unrelated 'likely pathogenic' or
                # 'conflicting classifications of pathogenicity' components.
                _col_lc = pl.col(col).str.to_lowercase()
                _clinsig_patterns = [
                    r"(?:^|[/;,]\s*)" + re.escape(v) + r"(?:\s*[/;,]|$)"
                    for v in vals_lower
                ]
                _clinsig_masks = [
                    _col_lc.str.contains(p, literal=False) for p in _clinsig_patterns
                ]
                df = df.filter(reduce(lambda a, b: a | b, _clinsig_masks))
            elif col in _SUBSTRING_MATCH_COLS:
                # OR over substring matches (case-insensitive).
                # HGVS normalisation: ClinVar canonicalises "c.5266dupC" → "c.5266dup"
                # (the duplicated base letter is implicit). Generate both forms.
                _patterns: list[str] = []
                for v in vals_lower:
                    _patterns.append(v)
                    _stripped = _HGVS_STRIP_RE.sub(lambda m: m.group(1), v)
                    if _stripped != v:
                        _patterns.append(_stripped)
                    _plural = _plural_variant(v)
                    if _plural and _plural != v:
                        _patterns.append(_plural)
                _patterns = list(dict.fromkeys(_patterns))
                _col_lc = pl.col(col).str.to_lowercase()
                if col == "geneset_name":
                    # MSigDB geneset_name is an intentional OPEN-ENDED prefix match
                    # (KEGG_, REACTOME_, GOBP_, WP_, ...) — the matched token is
                    # immediately followed by more name characters (e.g. "kegg_"
                    # then "pathway_xyz" with no separator), so a trailing \b would
                    # never fire and would break every source-DB query. Keep plain
                    # literal substring matching here.
                    _sub_masks = [_col_lc.str.contains(p, literal=True) for p in _patterns]
                else:
                    # 2026-07-04: plain (unanchored) substring matching lets a short
                    # multi-word disease/phenotype/synonym term collide mid-word
                    # inside an unrelated longer name — e.g. "gliomas" (from
                    # "pediatric gliomas") is a literal substring of "Paragangliomas"
                    # (an unrelated SDHD-linked tumour), so the ClinVar
                    # variant_disease_name filter matched the wrong disease family
                    # entirely. Anchor each pattern with \b word boundaries so it
                    # must match a whole word/phrase, not an arbitrary character
                    # run inside a longer unrelated word. This still matches
                    # legitimate partial-phrase queries ("Joubert syndrome" inside
                    # "Joubert syndrome 1") since word boundaries naturally fall at
                    # the surrounding whitespace/punctuation.
                    _sub_masks = [
                        _col_lc.str.contains(r"\b" + re.escape(p) + r"\b", literal=False)
                        for p in _patterns
                    ]
                if col == "variant_name":
                    # 2026-07-05: shorthand protein-change notation (e.g. "V600E",
                    # "T790M", "M918T", a bare hotspot codon like "C634") never
                    # substring-matches the three-letter HGVS p.-notation ClinVar
                    # stores (e.g. "p.Val600Glu") — different alphabet entirely
                    # ("v600e" vs "val600glu"). This silently zeroed every query
                    # that named a specific variant alongside its gene (BRAF V600E,
                    # RET M918T/C634, PIK3CA H1047R/E545K, IDH1 R132H, ...) while the
                    # identical gene queried without the variant shorthand worked
                    # fine. Translate the shorthand to its three-letter form via the
                    # fixed IUPAC amino-acid code table and add it as an extra
                    # candidate pattern — generalises to any gene/variant using
                    # standard substitution shorthand, not a per-variant lookup.
                    for v in vals_lower:
                        _prot_re = _protein_shorthand_regex(v)
                        if _prot_re:
                            _sub_masks.append(_col_lc.str.contains(_prot_re, literal=False))
                _sub_or = reduce(lambda a, b: a | b, _sub_masks)
                df = df.filter(_sub_or)
            else:
                df = df.filter(
                    pl.col(col).str.to_lowercase().is_in(vals_lower)
                )

            after = estimate_cardinality(df)

            if filter_stats is not None:
                filter_stats.append(
                    FilterStat(
                        column=col,
                        input_values=vals_lower,
                        rows_before=before,
                        rows_after=after,
                        table=table_name or "",
                    )
                )


    if or_masks:
        or_mask = reduce(lambda a, b: a | b, or_masks)
        final_mask = and_mask & or_mask
    else:
        final_mask = and_mask

    out = df.filter(final_mask)

    # Record OR-column filter trace (target_name + gene_name are OR'd together
    # in a single mask above; without this synthetic stat the trace shown to
    # the user is empty for the very common gene/target-only queries).
    if filter_stats is not None and or_masks:
        try:
            before = estimate_cardinality(df)
            after  = estimate_cardinality(out)
            or_vals_seen = []
            for col in or_columns:
                if col in filters and isinstance(filters[col], list):
                    or_vals_seen.extend(str(v) for v in filters[col] if v)
            # Deduplicate but preserve order
            seen = set(); _vals = []
            for v in or_vals_seen:
                v_lc = v.lower()
                if v_lc not in seen:
                    seen.add(v_lc); _vals.append(v)
            filter_stats.append(
                FilterStat(
                    column="target_name OR gene_name",
                    input_values=_vals[:20],
                    rows_before=before,
                    rows_after=after,
                    table=table_name or "",
                )
            )
        except Exception as _e:
            logger.warning(f"OR-mask filter_stat append failed: {_e}")

    return out


def deduplicate_results(result: pl.DataFrame, cols_to_use: List[str], db_name: str) -> pl.DataFrame:
    """
    Deduplicate results with memory-efficient handling.
    """
    if result.height == 0:
        logger.info(f"[{db_name}] Result is empty, skipping deduplication")
        return result

    if result.height > MAX_UNIQUE_ROWS:
        logger.warning(
            f"[{db_name}] Result has {result.height:,} rows, which exceeds "
            f"MAX_UNIQUE_ROWS ({MAX_UNIQUE_ROWS:,}). Skipping deduplication "
            f"to avoid memory issues. Consider adding more filters."
        )
        return result

    logger.info(f"[{db_name}] Deduplicating {result.height:,} rows...")
    # maintain_order=True is REQUIRED for reproducibility kappa — without it,
    # polars' unique() returns rows in arbitrary order, so the same query at
    # t=0 and t=1 produces a different top-K appendix.
    result_dedup = result.unique(subset=cols_to_use, maintain_order=True)

    removed = result.height - result_dedup.height
    if removed > 0:
        logger.info(
            f"[{db_name}] Removed {removed:,} duplicate rows "
            f"({removed/result.height*100:.1f}%)"
        )
    return result_dedup


def collect_with_memory_management(join_chain: pl.LazyFrame, db_name: str) -> pl.DataFrame:
    """
    Collect results with memory-efficient options.
    """
    logger.info(f"[{db_name}] Collecting results...")

    estimated_rows = estimate_cardinality(join_chain)
    if estimated_rows > MAX_RESULT_SIZE:
        raise DatabaseJoinError(
            f"Query would return {estimated_rows:,} rows, which exceeds "
            f"MAX_RESULT_SIZE ({MAX_RESULT_SIZE:,}). Please add more filters "
            f"or adjust MAX_RESULT_SIZE environment variable."
        )

    try:
        if ENABLE_STREAMING:
            result = join_chain.collect(streaming=True)
        else:
            result = join_chain.collect()
    except pl.exceptions.ComputeError as exc:
        # Polars streaming can fail with "Invalid thrift: protocol error" on the first
        # collect after a container restart (stale OS-level file state). Retry once
        # with non-streaming collect which forces a fresh file read.
        if "thrift" in str(exc).lower() or "protocol error" in str(exc).lower() or "specification" in str(exc).lower():
            logger.warning(f"[{db_name}] streaming collect failed ({exc}); retrying without streaming")
            result = join_chain.collect()
        else:
            raise

    logger.info(f"[{db_name}] Collected {result.height:,} rows")
    return result


def normalize_join_pairs(join_pairs: Dict[Any, Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """
    Normalize join_pairs keys to (left, right) tuples.

    Handles keys as:
      - tuple[str, str]
      - stringified tuple "(a, b)"
      - comma-joined string "a,b"
    """
    import ast

    normalized: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for k, v in join_pairs.items():
        if isinstance(k, tuple):
            if len(k) != 2:
                raise ValueError(f"Invalid join_pairs tuple key: {k}")
            left, right = k

        elif isinstance(k, str):
            if k.strip().startswith("("):
                try:
                    parsed = ast.literal_eval(k)
                    if not isinstance(parsed, tuple) or len(parsed) != 2:
                        raise ValueError
                    left, right = parsed
                except Exception:
                    raise ValueError(f"Invalid join_pairs key string: {k}")
            else:
                parts = [p.strip() for p in k.split(",")]
                if len(parts) != 2:
                    raise ValueError(f"Invalid join_pairs key format: {k}")
                left, right = parts
        else:
            raise TypeError(f"Unsupported join_pairs key type: {type(k)} ({k})")

        normalized[(left, right)] = v

    return normalized

def join_and_filter_database(
    dataset: Dict[str, Dict[str, pl.LazyFrame]],
    plan: Dict[str, Any],
    db_name: str,
    output_columns: List[str],
    filtered_outputs: Dict[str, Any]
) -> Tuple[pl.DataFrame, List[FilterStat]]:
    """
    Join and filter database tables according to query plan.

    Updates:
    - Per-query cardinality cache reset
    - Better root selection: choose smallest root (parent=None) by estimated rows
    """
    # Reset per-query cache (ContextVar to avoid cross-request collisions)
    _CARDINALITY_CACHE.set({})
    filter_stats: List[FilterStat] = []

    logger.info(f"[{db_name}] Starting join_and_filter_database")

    # Guard against filter fall-through (whole-DB dump bug). A "real" filter
    # value MUST be a non-empty list (or tuple) of non-empty, non-sentinel
    # strings. Common fall-through shapes seen in practice:
    #   - {} or None                        (no filter)
    #   - {"gene_name": "requested"}        (sentinel only)
    #   - {"gene_name": []}                 (empty list)
    #   - {"gene_name": [None]} / [""]      (empty list elements)
    # Any of these means the entity-expansion step produced nothing usable;
    # without this guard fast_filter_dataframe() returns the whole DB and the
    # tool reports the whole-DB row count as if it were a real result.
    def _is_real_filter_value(v) -> bool:
        # Accept a bare string filter value (LLM sometimes emits "EGFR"
        # instead of ["EGFR"]); apply_filters() coerces it before use.
        if isinstance(v, str):
            s = v.strip()
            return bool(s) and s.lower() != "requested"
        if not isinstance(v, (list, tuple)):
            return False
        for x in v:
            if x is None:
                continue
            s = str(x).strip()
            if not s:
                continue
            if s.lower() == "requested":
                continue
            return True
        return False

    if not filtered_outputs or not any(
        _is_real_filter_value(v) for v in filtered_outputs.values()
    ):
        logger.warning(
            f"[{db_name}] Empty/sentinel-only filter spec after entity "
            f"expansion; refusing whole-DB dump. "
            f"filtered_outputs={filtered_outputs!r}"
        )
        raise NoFilterTermsError(
            "No filter terms produced after entity expansion. "
            "The query entity could not be matched to this database."
        )

    # Schema-overlap guard: even if filter dict has real list values, those
    # keys must overlap with at least one column in at least one table in the
    # plan. Otherwise the filter is a silent no-op and fast_filter_dataframe
    # returns the full table (whole-DB dump). Observed in BioGRID, where the
    # PPI table uses `gene_a`/`gene_b` and a filter on `gene_name` matches
    # nothing.
    try:
        _all_schema_cols: Set[str] = set()
        # Read column names from the dataset structure directly.
        if db_name in dataset:
            for _tbl_key, _tbl_df in dataset[db_name].items():
                try:
                    _sch = _tbl_df.collect_schema() if hasattr(_tbl_df, "collect_schema") else _tbl_df.schema
                    for _c in (_sch.names() if hasattr(_sch, "names") else _sch):
                        _all_schema_cols.add(_c)
                except Exception:
                    pass
        _real_keys = {
            k for k, v in filtered_outputs.items() if _is_real_filter_value(v)
        }
        if _all_schema_cols and _real_keys and not (_real_keys & _all_schema_cols):
            logger.warning(
                f"[{db_name}] Filter keys {_real_keys} have NO overlap with DB "
                f"schema columns {sorted(_all_schema_cols)[:12]}...; refusing "
                f"whole-DB dump."
            )
            raise ValueError(
                f"Filter keys {sorted(_real_keys)} are not present in the "
                f"{db_name} schema; query entity could not be applied to any column."
            )
    except ValueError:
        raise
    except Exception as _e:
        logger.warning(f"[{db_name}] schema-overlap guard skipped: {_e}")

    if not plan or "tables" not in plan:
        raise ValueError("Invalid plan: missing 'tables' key")

    if db_name not in dataset:
        raise ValueError(
            f"Database '{db_name}' not found in dataset. "
            f"Available: {list(dataset.keys())}"
        )

    fq_tables = plan["tables"]
    table_info = plan["table_columns"]
    parents = plan["parents"]

    join_pairs_raw = plan.get("join_pairs", {})
    join_pairs = normalize_join_pairs(join_pairs_raw)

    if not fq_tables:
        raise ValueError("No tables in plan")

    logger.info(f"[{db_name}] Plan has {len(fq_tables)} tables: {fq_tables}")
    logger.info(f"[{db_name}] Requested output columns: {output_columns}")

    def get_df(fq_table: str) -> pl.LazyFrame:
        """Get LazyFrame for fully-qualified table name.

        Resolution order (fix 2026-05-13):
          1. {tbl}_{db_name}            — historical convention
          2. {tbl}                      — schema key already has db suffix
          3. {tbl}_{db_name}_{db_name}  — defensive double-suffix
        Some Phase-2 expansion tables carry the db suffix in the schema key
        itself; without this fallback the lookup `{tbl}_{db_name}` becomes
        e.g. `ppi_physical_string_string` and raises.
        """
        parts = fq_table.split(".")
        if len(parts) != 2:
            raise ValueError(f"Invalid table name format: '{fq_table}' (expected 'db.table')")

        tbl = parts[1]
        candidates = [f"{tbl}_{db_name}", tbl, f"{tbl}_{db_name}_{db_name}"]
        table_key = next((k for k in candidates if k in dataset[db_name]), None)
        if table_key is None:
            raise ValueError(
                f"Table '{tbl}' not found in dataset['{db_name}']. "
                f"Tried {candidates}. "
                f"Available tables: {sorted(dataset[db_name].keys())}"
            )

        df = dataset[db_name][table_key]
        if isinstance(df, pl.DataFrame):
            df = df.lazy()
        return df

    # Pre-filter all tables
    logger.info(f"[{db_name}] Pre-filtering {len(fq_tables)} tables...")
    pre_filtered_dfs: Dict[str, pl.LazyFrame] = {}

    # 2026-05-16 — Schema-projection: prune each LazyFrame to ONLY the
    # columns declared in config/schema.py for this table. Without this,
    # parquet files that carry extra columns beyond the schema declaration
    # (e.g. CTD chemical_disease_association has drug_name + disease_name
    # in the parquet but the schema declares only drug_id + disease_id +
    # evidence fields) cause polars to bail on the multi-table join with
    # `DuplicateError: column with name 'drug_name_right' already exists`.
    # Projecting to schema-declared columns enforces the loader contract
    # at runtime and dodges every such collision in one place.
    try:
        from config.schema import database_schemas as _schemas
        _db_schema = _schemas.get(db_name, {}) or {}
    except Exception:
        _db_schema = {}

    # 2026-05-23: per-DB decoration tables — LEFT-joined so missing entries
    # don't drop base rows. Also excluded from root candidacy so the chain
    # starts from a canonical table. See config.schema.database_decoration_tables.
    try:
        from config.schema import database_decoration_tables as _decoration_map
        _db_decoration: set[str] = set(_decoration_map.get(db_name, set()))
    except Exception:
        _db_decoration = set()

    def _is_decoration(fq_table: str) -> bool:
        return fq_table.split(".", 1)[-1] in _db_decoration

    def _project_to_schema(lf: pl.LazyFrame, fq: str) -> pl.LazyFrame:
        if not _db_schema:
            return lf
        tbl_short = fq.split(".")[-1]
        # Schema keys are unsuffixed (e.g. "chemical_disease_association"),
        # not the `_<db>` variant the loader uses.
        for k in (tbl_short, tbl_short.replace(f"_{db_name}", "")):
            decl = _db_schema.get(k)
            if decl:
                try:
                    available = set(
                        lf.collect_schema().names()
                        if hasattr(lf, "collect_schema")
                        else lf.schema.names()
                    )
                    keep = [c for c in decl if c in available]
                    if keep and len(keep) < len(available):
                        return lf.select(keep)
                except Exception:
                    pass
                return lf
        return lf

    for fq in fq_tables:
        df = get_df(fq)
        df = _project_to_schema(df, fq)
        filtered_df = fast_filter_dataframe(
            df,
            filtered_outputs,
            filter_stats=filter_stats,
            table_name=fq.split('.')[-1],
        )

        pre_filtered_dfs[fq] = filtered_df

        # Optional logging (now cached)
        pre_count = estimate_cardinality(df)
        post_count = estimate_cardinality(filtered_df)
        if pre_count > 0 and post_count >= 0:
            reduction = (1 - post_count / pre_count) * 100
            logger.info(
                f"[{db_name}] Pre-filtered {fq}: "
                f"{pre_count:,} -> {post_count:,} rows ({reduction:.1f}% reduction)"
            )

    # Single-table case
    if len(fq_tables) == 1:
        logger.info(f"[{db_name}] Single table query (no joins needed)")
        join_chain = pre_filtered_dfs[fq_tables[0]]

    else:
        logger.info(f"[{db_name}] Multi-table query with {len(fq_tables)-1} join(s)")

        # --- Root at the most-selective table to avoid join explosions ---
        # The planner's tree direction is arbitrary; for INNER joins the result is
        # identical regardless of root, but starting the chain from the smallest
        # POST-FILTER table prevents intermediate explosions (e.g. "chemicals that
        # increase TNF" rooting on chemical_master (179k) → chemical_master ×
        # chemical_gene = 268M rows; rooting on gene_master[TNF]=1 row stays tiny).
        # Only applied when the plan has NO decoration/LEFT-join tables, so the
        # decoration-aware logic below is untouched for those plans.
        if not any(_is_decoration(t) for t in fq_tables):
            _best = min(fq_tables, key=lambda t: estimate_cardinality(pre_filtered_dfs[t]))
            if parents.get(_best) is not None:
                parents = _reroot_join_tree(parents, _best)
                logger.info(
                    f"[{db_name}] Re-rooted join tree at most-selective table "
                    f"{_best} ({estimate_cardinality(pre_filtered_dfs[_best])} rows) "
                    f"to avoid join explosion"
                )

        # --- Better root selection (smallest valid root) ---
        root_candidates = [t for t in fq_tables if parents.get(t) is None]
        if not root_candidates:
            raise ValueError("No root table found in plan. Expected at least one table with parent=None")

        # 2026-05-23: never pick a decoration table as root — the join chain
        # would then start with the bridge (e.g. drug_crossmatching) and the
        # subsequent INNER joins would drop every entity missing from it
        # (biologics, CAR-T, etc.).
        _non_dec_roots = [t for t in root_candidates if not _is_decoration(t)]
        if _non_dec_roots:
            root_candidates = _non_dec_roots
        elif _db_decoration:
            # All planner-marked roots are decoration. Re-root by promoting
            # the smallest non-decoration table that is directly attached
            # to a decoration root, and demoting the old root into a child
            # (which our LEFT-join path below will then handle correctly).
            _dec_roots = set(root_candidates)
            _swap = [t for t in fq_tables
                     if not _is_decoration(t) and parents.get(t) in _dec_roots]
            if _swap:
                _new_root = min(_swap, key=lambda t: estimate_cardinality(pre_filtered_dfs[t]))
                _old_root = parents[_new_root]
                parents[_new_root] = None
                parents[_old_root] = _new_root
                root_candidates = [_new_root]
                logger.info(
                    f"[{db_name}] Re-rooting: decoration table {_old_root} was the only "
                    f"planner-marked root; promoting {_new_root} so {_old_root} can "
                    f"LEFT-join as decoration instead of dropping rows"
                )

        root = min(root_candidates, key=lambda t: estimate_cardinality(pre_filtered_dfs[t]))
        logger.info(
            f"[{db_name}] Selected root table: {root} "
            f"(estimated rows={estimate_cardinality(pre_filtered_dfs[root])})"
        )

        join_chain = pre_filtered_dfs[root]
        joined_tables = {root}
        remaining_tables = set(fq_tables) - joined_tables
        all_join_metrics: List[JoinMetrics] = []

        while remaining_tables:
            ordered_next = optimize_join_order(remaining_tables, joined_tables, parents, pre_filtered_dfs)
            if not ordered_next:
                missing = remaining_tables
                raise ValueError(
                    f"Cannot join remaining tables: {missing}. "
                    f"Their parents have not been joined yet. "
                    f"Check that parent relationships in plan are correct."
                )

            child_table = ordered_next[0]
            parent_table = parents.get(child_table)

            if parent_table not in joined_tables:
                raise ValueError(
                    f"Parent '{parent_table}' not joined yet for child '{child_table}'. "
                    f"This should not happen after join order optimization."
                )

            join_key = (parent_table, child_table)
            reverse_key = (child_table, parent_table)

            found_join = False
            left_on: Optional[List[str]] = None
            right_on: Optional[List[str]] = None

            if join_key in join_pairs:
                join_spec = join_pairs[join_key]
                left_on = join_spec["left_on"]
                right_on = join_spec["right_on"]
                found_join = True
            elif reverse_key in join_pairs:
                join_spec = join_pairs[reverse_key]
                left_on = join_spec["right_on"]
                right_on = join_spec["left_on"]
                found_join = True

            if not found_join:
                logger.warning(f"[{db_name}] No explicit join_pairs for ({parent_table}, {child_table})")

                if STRICT_JOIN_MODE:
                    error_msg = (
                        f"No join_pairs defined between '{parent_table}' and '{child_table}'. "
                        f"Available join_pairs keys: {list(join_pairs.keys())}. "
                        f"Set STRICT_JOIN_MODE=false to allow auto-inference (not recommended)."
                    )
                    raise MissingJoinError(error_msg)

                # Fallback inference (risky)
                parent_join_cols = table_info.get(parent_table, {}).get("join_columns", [])
                child_join_cols = table_info.get(child_table, {}).get("join_columns", [])

                parent_schema = set(join_chain.schema.keys())
                child_schema = set(pre_filtered_dfs[child_table].schema.keys())

                common = (set(parent_join_cols) | set(child_join_cols)) & parent_schema & child_schema
                if not common:
                    common = parent_schema & child_schema

                if not common:
                    raise MissingJoinError(
                        f"Cannot infer join columns between {parent_table} and {child_table}. "
                        f"Parent schema: {sorted(parent_schema)}, "
                        f"Child schema: {sorted(child_schema)}, "
                        f"No common columns found."
                    )

                left_on = right_on = sorted(list(common))
                logger.warning(
                    f"[{db_name}] Inferred join columns: {left_on} "
                    f"(THIS IS RISKY - add explicit join_pairs!)"
                )

            # Validate join columns exist
            validate_join_columns(join_chain.schema, pre_filtered_dfs[child_table].schema, left_on, right_on, parent_table, child_table)

            logger.info(
                f"[{db_name}] Joining {parent_table} -> {child_table} "
                f"on left={left_on}, right={right_on}"
            )

            # 2026-05-23: LEFT-join decoration child tables so entities missing
            # from the bridge (e.g. biologic drugs absent from drug_crossmatching)
            # survive with NULL decoration columns instead of being dropped.
            _join_how = "left" if _is_decoration(child_table) else "inner"
            if _join_how == "left":
                logger.info(
                    f"[{db_name}] LEFT-joining decoration table {child_table} "
                    f"(would inner-drop entities missing from this bridge)"
                )

            join_chain, metrics = perform_join_with_validation(
                join_chain,
                pre_filtered_dfs[child_table],
                left_on,
                right_on,
                parent_table,
                child_table,
                how=_join_how,
            )

            all_join_metrics.append(metrics)
            joined_tables.add(child_table)
            remaining_tables.remove(child_table)

            # Surface the join as a "trace step" so the chat UI can show the
            # user *why* the row count grew (e.g. 2 targets × ~25 drugs each
            # = ~50 rows). The column field is encoded as "JOIN(parent→child)"
            # — the frontend recognises this prefix and renders it differently
            # from a filter row.
            try:
                if filter_stats is not None:
                    filter_stats.append(
                        FilterStat(
                            column=f"JOIN({parent_table.split('.')[-1]}→{child_table.split('.')[-1]})",
                            input_values=[],
                            rows_before=int(metrics.pre_join_rows),
                            rows_after=int(metrics.post_join_rows),
                        )
                    )
            except Exception as _e:
                logger.warning(f"join trace append failed: {_e}")

        if all_join_metrics:
            avg_explosion = sum(m.explosion_factor for m in all_join_metrics) / len(all_join_metrics)
            logger.info(f"[{db_name}] Completed all joins. Average explosion factor: {avg_explosion:.2f}x")

    final_schema = join_chain.schema
    cols_to_use = [col for col in output_columns if col in final_schema]

    # Also include filter columns that were applied (have list values) and exist in the
    # final schema but weren't already selected as output columns.
    filter_applied_cols = [
        col for col, val in filtered_outputs.items()
        if isinstance(val, list) and val and col in final_schema and col not in cols_to_use
    ]
    if filter_applied_cols:
        logger.info(f"[{db_name}] Adding applied filter columns to output: {filter_applied_cols}")
        cols_to_use = cols_to_use + filter_applied_cols

    if not cols_to_use:
        logger.warning(
            f"[{db_name}] No requested output columns found in result. "
            f"Requested: {output_columns}, Available: {list(final_schema.keys())}"
        )
        return pl.DataFrame({col: [] for col in output_columns}), filter_stats

    # ── Rank by known confidence/score columns BEFORE selecting output columns
    # so the downstream `.head(K)` preview surfaces the strongest evidence rows
    # (e.g. canonical interactors for PPI), not arbitrary DB-internal order.
    # Score columns are listed in priority order; first match wins. "True" =
    # higher is better (descending sort), False = ascending.
    _RANK_COLUMNS = [
        ("combined_score",     True),   # STRING PPI confidence (0–1000)
        ("curation_effort",    True),   # OmniPath: count of curated sources
        ("score",              True),   # generic confidence column
        ("confidence",         True),   # generic
        ("evidence_score",     True),   # generic
        ("frequency",          True),   # HPO gene-phenotype frequency
        ("evidence_level",     False),  # CIViC: lower number = stronger
        ("citation_count",     True),
        ("pubmed_count",       True),
        ("n_pubmed",           True),
        ("publications",       True),
        # 2026-05-19: BioGRID — `throughput_ord` is a numeric ordinal supplied
        # by the DuckDB view (0 = both H+L, 1 = Low, 2 = High). Sort ASCENDING
        # so 0 first, then Low, then High. Replaces the broken raw-string sort
        # on `throughput` (kept as a last-resort fallback below for any DB that
        # still emits the raw column).
        ("throughput_ord",     False),
        # NOTE: the legacy `throughput` entry below was BUGGY — `False`
        # (ascending) puts 'High Throughput' before 'Low Throughput'
        # alphabetically. Flipped to True so on the fallback path Low
        # Throughput at least sorts above High Throughput.
        ("throughput",         True),
    ]
    _rank_col = None
    _rank_desc = True
    for _c, _desc in _RANK_COLUMNS:
        if _c in final_schema:
            _rank_col = _c
            _rank_desc = _desc
            break
    if _rank_col is not None:
        try:
            # Multi-column sort: rank_col first, then a stable secondary key
            # (a deterministic "tiebreaker" column) so top-K is byte-identical
            # across re-runs. Without this, ties on the score column resolve
            # arbitrarily and reproducibility kappa drops to ~0.6.
            secondary_keys: list[str] = []
            for tiebreak in ("gene_partner_id", "protein_partner_id", "gene_b",
                             "gene_symbol", "phenotype_id", "phenotype_name",
                             "drug_name", "disease_id"):
                if tiebreak in final_schema and tiebreak != _rank_col:
                    secondary_keys.append(tiebreak)
                if len(secondary_keys) >= 2:
                    break
            sort_cols = [_rank_col] + secondary_keys
            descending = [_rank_desc] + [False] * len(secondary_keys)
            join_chain = join_chain.sort(sort_cols, descending=descending, nulls_last=True)
            if _rank_col not in cols_to_use:
                cols_to_use = cols_to_use + [_rank_col]
            logger.info(
                f"[{db_name}] Ranked result by {sort_cols} "
                f"(descending={descending}) before column selection"
            )
        except Exception as _e:
            logger.warning(f"[{db_name}] Could not sort by '{_rank_col}': {_e}")

    join_chain = join_chain.select(cols_to_use)

    try:
        result = collect_with_memory_management(join_chain, db_name)

        if result.height == 0:
            logger.warning(f"[{db_name}] Query returned 0 rows after joins and filters")
            return pl.DataFrame({col: [] for col in cols_to_use}), filter_stats

        # ── Multi-condition INTERSECTION (require_all) ────────────────────
        # "both X and Y" → keep only OUTPUT entities linked to ALL N named
        # entities, not just one. plan["require_all_fields"] = {field: N} is set
        # upstream ONLY on explicit intersection intent ("both"/"all of"/…); a
        # plain list query never sets it, so the default OR (is_in) is untouched
        # → no regression. The output entity is identified by the 'requested'
        # filter columns: group by those, require the field to span all N
        # distinct values. N is the ORIGINAL entity count (synonyms of one entity
        # cannot inflate it). Generic across DBs; no per-DB/per-entity hardcoding.
        _require_all = (plan or {}).get("require_all_fields") or {}
        if _require_all:
            _requested_cols = [
                k for k, v in (filtered_outputs or {}).items()
                if v == "requested" and k in result.columns
            ]
            # 2026-06-24: the group key must be the OUTPUT entity only. The
            # intersected field AND its id/name companion (same entity stem —
            # e.g. phenotype_name & phenotype_id both → "phenotype") must be
            # excluded: otherwise each (output, intersected-value) pair is its
            # own group, so n_unique(field) is always 1 and the `>= N` test can
            # never pass → "both X and Y" silently returns 0. The companion is
            # routinely co-requested via name⇄id co_output rules. Bug surfaced
            # by HPO "diseases with BOTH intellectual disability and seizures".
            def _entity_stem(c: str) -> str:
                for _suf in ("_name", "_symbol", "_id"):
                    if c.endswith(_suf) and len(c) > len(_suf):
                        return c[: -len(_suf)]
                return c
            # Organism columns that must join the group key (Case 2): an output
            # entity like geneset_name is duplicated per organism, so the ∩ has
            # to be computed WITHIN a species — otherwise a human-gene row and a
            # mouse-gene row co-group and "both X and Y" straddles species and
            # collapses to 0. No-op on single-organism DBs (none of these exist).
            _ORG_COLS = ("geneset_organism", "gene_organism", "organism",
                         "geneset_organism_name", "organism_name")
            for _raf, _n in _require_all.items():
                _raf_stem = _entity_stem(_raf)
                _group_cols = [c for c in _requested_cols
                               if _entity_stem(c) != _raf_stem]
                _group_cols += [c for c in _ORG_COLS
                                if c in result.columns and c not in _group_cols]
                if not (_group_cols and _raf in result.columns
                        and isinstance(_n, int) and _n >= 2):
                    continue
                # Honest-empty guard (Case 1a): if fewer than N distinct values of
                # the intersected field are actually present in the frame, one of
                # the named entities never resolved into the result (e.g. a gene
                # silently dropped during synonym expansion). The "both" condition
                # is then unsatisfiable as posed — return an explicit empty rather
                # than letting the plain OR (union) result stand and read as a
                # confident answer for only the entities that DID resolve.
                _present = (result.get_column(_raf).cast(pl.Utf8)
                            .str.to_lowercase().n_unique())
                if _present < _n:
                    logger.warning(
                        f"[{db_name}] require_all '{_raf}': only {_present} of {_n} "
                        f"named values present in frame — an entity did not resolve; "
                        f"returning honest-empty instead of the OR-union."
                    )
                    return pl.DataFrame({col: [] for col in cols_to_use}), filter_stats
                try:
                    _keep = (
                        result
                        .group_by(_group_cols)
                        .agg(pl.col(_raf).cast(pl.Utf8).str.to_lowercase()
                             .n_unique().alias("__nmatch"))
                        .filter(pl.col("__nmatch") >= _n)
                        .select(_group_cols)
                    )
                    _before = result.height
                    result = result.join(_keep, on=_group_cols, how="inner")
                    logger.info(
                        f"[{db_name}] require_all '{_raf}' (∩ of {_n} via {_group_cols}): "
                        f"{_before:,} → {result.height:,} rows"
                    )
                except Exception as _re_exc:
                    logger.warning(f"[{db_name}] require_all '{_raf}' skipped: {_re_exc}")
            if result.height == 0:
                logger.warning(f"[{db_name}] intersection (require_all) left 0 rows")
                return pl.DataFrame({col: [] for col in cols_to_use}), filter_stats

        result = deduplicate_results(result, cols_to_use, db_name)

        # ── Deterministic ordering guarantee ──────────────────────────────
        # When no rank/score column was available (`_rank_col is None`), the
        # row order coming out of the join chain is whatever polars' hash
        # join + `.unique()` produced. That order is NOT stable across
        # worker processes, `ttl_cached_db` refreshes, or polars thread
        # counts — so the downstream `.head(K)` preview and the K rows fed
        # to the synthesizer would be an arbitrary, non-reproducible subset,
        # and two identical queries could yield different answers.
        #
        # A final total-order sort on every output column makes the result
        # byte-identical for a given filter set (the same guarantee the
        # rank-sort path above provides via `combined_score` + tiebreakers).
        # Skipped when `_rank_col` is set — there the rank-sort already
        # established the intentional ordering, and re-sorting would destroy
        # it. `nulls_last=True` matches the rank-sort path's null handling.
        if _rank_col is None and result.height > 1 and cols_to_use:
            try:
                result = result.sort(cols_to_use, nulls_last=True)
                logger.info(
                    f"[{db_name}] Applied deterministic final sort on "
                    f"{len(cols_to_use)} output column(s) "
                    f"(no rank column present)"
                )
            except Exception as _sort_err:
                logger.warning(
                    f"[{db_name}] Deterministic final sort failed "
                    f"({_sort_err}) — result order is not reproducible"
                )

        logger.info(f"[{db_name}] Final result: {result.height:,} rows × {len(cols_to_use)} columns")
        return result, filter_stats


    except Exception as e:
        logger.exception(f"[{db_name}] Failed to collect results: {e}")
        raise DatabaseJoinError(f"Failed to execute query: {e}") from e
