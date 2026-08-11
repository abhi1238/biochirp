#!/usr/bin/env bash
# Roll an updated .dockerignore template out to every build context listed
# in docker-compose.yml. Run from repo root.
#
#   bash scripts/sync_dockerignore.sh path/to/new_template
#
# If no template path is given, the first context's existing .dockerignore
# is used as the source of truth.
set -euo pipefail
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

TEMPLATE=${1:-}
python3 - "$TEMPLATE" <<'PY'
import os, sys, yaml, shutil
template = sys.argv[1] or None
with open("docker-compose.yml") as f:
    data = yaml.safe_load(f)
ctxs = sorted({(b.get("context") or "").lstrip("./") for v in data["services"].values() if isinstance(b:=v.get("build"), dict)})
ctxs = [c for c in ctxs if c]
if not template:
    template = os.path.join(ctxs[0], ".dockerignore")
    print(f"Using {template} as template")
if not os.path.exists(template):
    sys.exit(f"Template {template} not found")
for c in ctxs:
    dst = os.path.join(c, ".dockerignore")
    if os.path.abspath(dst) == os.path.abspath(template):
        continue
    shutil.copyfile(template, dst)
    print(f"  + {dst}")
PY
