#!/usr/bin/env bash
# Build the portable Gradus bundle that bench/harbor_agent.py uploads into task
# containers, then export it to bench/.cache/gradus-bundle.
#
# Why a prebuilt bundle: Terminal-Bench images include bare ubuntu:24.04 with no
# curl, git or python, and apt is unreachable from inside some tasks. Installing
# Gradus at task time therefore cannot work; uploading a fixed bundle can.
#
# Why the standalone interpreter: a venv built on Debian links libpython3.13.so
# from /usr/local/lib, which a different distro does not have. uv's
# python-build-standalone ships its own libs, so the bundle only needs
# LD_LIBRARY_PATH pointing at its own lib dir (harbor_agent.py does that).
#
# Why no symlinks: `docker cp` on Windows cannot create them. Every symlink in
# the tree is dereferenced into a real file before the bundle leaves the image.
#
# Usage:
#   bash bench/build_bundle.sh            # build + export
#   DOCKER=podman bash bench/build_bundle.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT="${TMPDIR:-/tmp}/gradus-bundle-src"
IMAGE="${IMAGE:-gradus-agent:py313}"
DOCKER="${DOCKER:-docker}"

echo "==> staging sources from $REPO"
rm -rf "$CONTEXT"
mkdir -p "$CONTEXT"
# Only the package and the metadata are needed; .venv/web/node_modules stay out.
cp -r "$REPO/gradus" "$CONTEXT/gradus"
find "$CONTEXT" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$REPO/pyproject.toml" "$CONTEXT/pyproject.toml"

cat > "$CONTEXT/Dockerfile" <<'DOCKERFILE'
FROM python:3.13-slim
RUN pip install --no-cache-dir uv

# Standalone CPython 3.13 (ships its own .so files)
RUN uv python install 3.13 --install-dir /opt/gradus/py
RUN PY=$(ls -d /opt/gradus/py/cpython-3.13*/bin/python3.13 | head -1) && \
    $PY -m venv --copies /opt/gradus/venv && rm -f /opt/gradus/venv/lib64

# Gradus itself, installed from the staged source tree
COPY . /src
RUN uv pip install --python /opt/gradus/venv/bin/python /src

# docker cp on Windows cannot restore symlinks: materialise them as real files
RUN find /opt/gradus -type l | while read -r link; do \
      cp -a "$(readlink -f "$link")" "$link.real" && rm -f "$link" && mv "$link.real" "$link"; \
    done
RUN test -z "$(find /opt/gradus -type l)" && echo "no symlinks"

# Smoke check: the console script must start on a distro-agnostic loader path
RUN LD_LIBRARY_PATH=$(ls -d /opt/gradus/py/cpython-3.13*/lib | head -1) \
    /opt/gradus/venv/bin/gradus --help >/dev/null && echo "bundle ok"
DOCKERFILE

echo "==> building $IMAGE"
"$DOCKER" build -t "$IMAGE" "$CONTEXT"

echo "==> exporting to bench/.cache/gradus-bundle"
DEST="$REPO/bench/.cache/gradus-bundle"
rm -rf "$DEST"
mkdir -p "$DEST"
helper="gradus-bundle-export-$$"
"$DOCKER" create --name "$helper" "$IMAGE" >/dev/null
trap '"$DOCKER" rm -f "$helper" >/dev/null 2>&1 || true' EXIT
"$DOCKER" cp "$helper:/opt/gradus" "$DEST"

echo "==> done: $(du -sh "$DEST" | cut -f1) at $DEST"
echo "    run tasks with:  python -m pytest -q   # suite still green without it"
