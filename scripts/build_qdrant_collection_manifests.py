#!/usr/bin/env python3
"""Emit a metadata sidecar for every Qdrant collection.

For each collection visible at $QDRANT_URL (default
http://localhost:6333), write:

  qdrant_manifests/<collection_name>.metadata.json

containing the information that a reviewer cannot otherwise recover from
the on-disk Qdrant snapshot alone:

  - collection name
  - vector dimension                  (from Qdrant config)
  - distance metric                   (from Qdrant config)
  - HNSW parameters: m, ef_construct  (from Qdrant config)
  - points_count                      (from Qdrant)
  - embedding model name              (mapped from collection name)
  - embedding model revision          (from config.settings.EMBEDDING_MODELS)
  - generated_at                      (UTC ISO 8601)
  - generator                         (this script + repo git commit)

Collection naming convention assumed: `emb_<repo-org>_<repo-name>` where
'/' in the HF repo name has been replaced with '_'. This is what the
existing ingest notebook produces.

A separate aggregate file qdrant_manifests/collections_manifest.json is
also written, with one entry per collection — useful for CI gates that
want to read a single file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from config.settings import EMBEDDING_MODELS  # noqa: E402


QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
QDRANT_STORAGE = REPO_ROOT / "qdrant_storage"
# Manifest sidecars are written next to (not inside) qdrant_storage,
# because qdrant_storage is bind-mounted into the Qdrant container and
# owned by root. Keeping manifests in a sibling directory under repo
# control means we can write/commit them as a normal user.
QDRANT_MANIFESTS = REPO_ROOT / "qdrant_manifests"


def http_get_json(path: str) -> dict:
    url = QDRANT_URL + path
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def collection_to_model(collection: str) -> str | None:
    """Reverse the `emb_<org>_<name>` naming convention back to `<org>/<name>`.

    There is ambiguity because both the HF '/' separator and any '-'/'_'
    inside the model name become '_' in the collection name. We resolve
    by checking each registered model and seeing which one matches.
    """
    if not collection.startswith("emb_"):
        return None
    tail = collection[len("emb_"):]
    for repo in EMBEDDING_MODELS.keys():
        sanitised = repo.replace("/", "_")
        if sanitised == tail:
            return repo
    return None


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


def build_one(collection_name: str) -> dict:
    info = http_get_json(f"/collections/{collection_name}")["result"]
    cfg = info["config"]
    vec = cfg["params"]["vectors"]
    hnsw = cfg.get("hnsw_config", {}) or {}

    model_name = collection_to_model(collection_name)
    entry = EMBEDDING_MODELS.get(model_name) if model_name else None

    return {
        "collection":       collection_name,
        "vector_dimension": vec["size"],
        "distance":         vec["distance"],
        "hnsw": {
            "m":           hnsw.get("m"),
            "ef_construct": hnsw.get("ef_construct"),
        },
        "points_count":     info.get("points_count"),
        "segments_count":   info.get("segments_count"),
        "embedding_model":  model_name,
        "embedding_revision": (entry or {}).get("revision"),
        "embedding_resolved_at": (entry or {}).get("resolved_at"),
        "embedding_status":  (entry or {}).get("status"),
        "embedding_role":    (entry or {}).get("role"),
        "generated_at":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator":        "scripts/build_qdrant_collection_manifests.py",
        "git_commit":       git_commit(),
        "qdrant_url":       QDRANT_URL,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not QDRANT_STORAGE.is_dir():
        print(f"WARNING: {QDRANT_STORAGE} not present — querying live Qdrant only.",
              file=sys.stderr)
    QDRANT_MANIFESTS.mkdir(parents=True, exist_ok=True)

    try:
        coll_resp = http_get_json("/collections")
    except urllib.error.URLError as e:
        print(f"ERROR: cannot reach Qdrant at {QDRANT_URL}: {e}", file=sys.stderr)
        print("Set QDRANT_URL or start the bioc_qdrant container.", file=sys.stderr)
        return 1

    collections = [c["name"] for c in coll_resp["result"]["collections"]]
    if not collections:
        print("WARNING: Qdrant has no collections.", file=sys.stderr)
        return 0

    aggregate = []
    for name in sorted(collections):
        meta = build_one(name)
        aggregate.append(meta)
        out = QDRANT_MANIFESTS / f"{name}.metadata.json"
        if args.dry_run:
            print(f"[dry-run] would write {out}")
        else:
            out.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        unpinned = " (UNPINNED!)" if not meta["embedding_revision"] else ""
        print(f"  {name:<48s}  dim={meta['vector_dimension']}  "
              f"dist={meta['distance']:<8s}  pts={meta['points_count']}  "
              f"model={meta['embedding_model']}{unpinned}")

    agg_path = QDRANT_MANIFESTS / "collections_manifest.json"
    if args.dry_run:
        print(f"[dry-run] would write {agg_path}")
    else:
        agg_path.write_text(
            json.dumps({"qdrant_url": QDRANT_URL,
                        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "collections": aggregate}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nWrote {agg_path}")

    # Warn about pin failures so CI can act on the exit code if desired.
    unpinned = [m["collection"] for m in aggregate if not m["embedding_revision"]]
    if unpinned:
        print(f"WARNING: collections with no resolved embedding pin: {unpinned}",
              file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
