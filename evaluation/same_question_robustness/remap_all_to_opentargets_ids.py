#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

import sys

# Allow running as a standalone script without installing this repo as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opentargets_grounding import OpenTargetsNameIndex, remap_pickle_to_opentargets_ids


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Offline remapping: rebuild `dataframe_id` for each model using "
            "the IDs OpenTargets uses in your local benchmark."
        )
    )
    ap.add_argument(
        "--opentargets-pkl",
        default="evaluation/same_question_robustness/OpenTargets/OpenTargets_same_question_response_with_id.pkl",
        help="Path to OpenTargets *_with_id.pkl (used to build the name->id index).",
    )
    ap.add_argument(
        "--out-dir",
        default="evaluation/same_question_robustness/_ot_grounded",
        help="Output directory for grounded pickles.",
    )
    ap.add_argument(
        "--include-pathway",
        action="store_true",
        help="Also attempt to map `pathway_name` -> `pathway_id` using OpenTargets results.",
    )
    ap.add_argument(
        "--no-fallback",
        action="store_true",
        help="If set: do NOT fall back to existing dataframe_id values when OT index lookup fails.",
    )

    args = ap.parse_args()

    ot_pkl = Path(args.opentargets_pkl)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ot_index = OpenTargetsNameIndex.from_opentargets_with_id_pickle(str(ot_pkl))
    print("Built OT name index:", ot_index.stats())

    in_files = [
        Path("evaluation/same_question_robustness/OpenAI/OpenAI_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/Llama/Llama_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/Gemini/Gemini_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/Grok/Grok_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/biochatter/Biochatter_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/ctd/CTD_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/hcdt/HCDT_same_question_response_with_id.pkl"),
        Path("evaluation/same_question_robustness/ttd/TTD_same_question_response_with_id.pkl"),
        # OpenTargets too (fills in missing `dataframe_id` cases like pathways).
        Path("evaluation/same_question_robustness/OpenTargets/OpenTargets_same_question_response_with_id.pkl"),
    ]

    include_pathway = bool(args.include_pathway)
    fallback_to_existing = not bool(args.no_fallback)

    for in_path in in_files:
        if not in_path.exists():
            print(f"SKIP missing: {in_path}")
            continue
        out_path = out_dir / in_path.name.replace(".pkl", "_ot_grounded.pkl")
        summary = remap_pickle_to_opentargets_ids(
            in_path=str(in_path),
            out_path=str(out_path),
            ot_index=ot_index,
            include_pathway=include_pathway,
            fallback_to_existing=fallback_to_existing,
        )
        frac = summary["frac_rows_with_any_nan"]
        frac_s = f"{frac:0.3f}" if isinstance(frac, float) else "n/a"
        print(
            f"WROTE {out_path} | model={summary['model']} | "
            f"df_id_rows={summary['rows_in_dataframe_id']} | frac_any_nan={frac_s}"
        )

    print("\nNext: point your overlap notebook/script to files under:", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
