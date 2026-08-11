#!/usr/bin/env bash
# Package the BioChirp database/ tree for public release (Zenodo, GitHub Release,
# Docker image build context, etc.).
#
# Excludes any directory under database/ that contains a LICENSE_RESTRICTED file.
# Those directories hold data whose terms of use prohibit redistribution
# (see REDISTRIBUTION_RESTRICTED.md at repo root).
#
# Usage:
#   scripts/package_data_for_release.sh [OUTPUT.tar.gz]
#
# Default OUTPUT: dist_zenodo/biochirp_data_YYYYMMDD.tar.gz
#
# Exit codes:
#   0  success
#   1  repo layout problem
#   2  user aborted at confirmation prompt

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d database ]]; then
    echo "ERROR: $REPO_ROOT/database not found. Run from the BioChirp repo." >&2
    exit 1
fi

OUT="${1:-dist_zenodo/biochirp_data_$(date -u +%Y%m%d).tar.gz}"
mkdir -p "$(dirname "$OUT")"

# Find restricted directories (any dir under database/ containing LICENSE_RESTRICTED).
# -H: follow symlink if `database` itself is a symlink (it is, in dev setups
# where the heavy parquet tree lives on a separate volume).
mapfile -t RESTRICTED_DIRS < <(find -H database -name LICENSE_RESTRICTED -printf '%h\n' | sort)

echo "=========================================================="
echo "BioChirp data release packager"
echo "Repo:      $REPO_ROOT"
echo "Output:    $OUT"
echo "=========================================================="
echo
echo "Restricted directories (will be EXCLUDED from the bundle):"
if [[ ${#RESTRICTED_DIRS[@]} -eq 0 ]]; then
    echo "  (none)"
else
    for d in "${RESTRICTED_DIRS[@]}"; do
        size=$(du -sh "$d" 2>/dev/null | cut -f1)
        echo "  - $d  ($size)"
    done
fi
echo
echo "Reason: each contains a LICENSE_RESTRICTED marker file."
echo "See REDISTRIBUTION_RESTRICTED.md for the full license summary."
echo

# Confirm interactively when run on a TTY; non-interactive runs (CI) skip the prompt.
if [[ -t 0 ]]; then
    read -r -p "Proceed and create the tarball? [y/N] " ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "Aborted." ; exit 2 ;;
    esac
fi

# Build the --exclude args for tar.
TAR_EXCLUDES=()
for d in "${RESTRICTED_DIRS[@]}"; do
    TAR_EXCLUDES+=(--exclude="$d")
done

# Also include REDISTRIBUTION_RESTRICTED.md so downstream users see why
# the bundle has gaps.
EXTRA_FILES=()
[[ -f REDISTRIBUTION_RESTRICTED.md ]] && EXTRA_FILES+=(REDISTRIBUTION_RESTRICTED.md)

echo
echo "Creating tarball... (this may take a while for large parquet trees)"
# -h: dereference symlinks (database/ is often a symlink to a data volume).
tar -czhf "$OUT" \
    "${TAR_EXCLUDES[@]}" \
    database \
    "${EXTRA_FILES[@]}"

# Compute checksum + size for the manifest / Zenodo upload.
SIZE=$(du -h "$OUT" | cut -f1)
SHA=$(sha256sum "$OUT" | cut -d' ' -f1)

echo
echo "=========================================================="
echo "Bundle created"
echo "  path:   $OUT"
echo "  size:   $SIZE"
echo "  sha256: $SHA"
echo "=========================================================="
echo
echo "Excluded $((${#RESTRICTED_DIRS[@]})) restricted directory/ies from the bundle."
echo "End users who need those sources must obtain their own license; see"
echo "REDISTRIBUTION_RESTRICTED.md for registration links."
