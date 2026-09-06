#!/usr/bin/env bash
# Fat .deb assembly for the Termux hermes-agent bundle (Task 4 of
# .hermes/plans/2026-08-31_termux-deb.md). Runs AFTER termux_build.sh
# (wheelhouse) and build_cpython.sh / build_node.sh have populated the
# payload dir. No opt-out flags: a skipped step is a different artifact.
#
# Inputs (all required):
#   --repo <dir>          hermes-agent checkout (tag must exist; provenance)
#   --tag <tag>           immutable release tag (vX.Y.Z or vX.Y.Z-canary.<ts>)
#   --payload <dir>       dir containing python/, node/, app/ (git archive of
#                         the tag) and wheelhouse/ (from termux_build.sh)
#   --out <dir>           output dir; <out>/hermes-agent_<v>_aarch64.deb lands here
#
# No opt-out flags: the .deb is ALWAYS installed into a fresh run of the
# pinned termux-docker image (digest pinned in pm/lock.json) and smoke-tested.
# docker must be available. The channel is derived from the tag by
# deb_version.py (--channel), not passed in.
#
# Staged payload layout: python/ and node/ are pm-staged termux .deb
# trees ($PREFIX-shaped: data/data/com.termux/files/usr/...). The
# installed layout is $PREFIX/lib/hermes-agent/{python,node,app,venv,bin} with
# exactly one leak: $PREFIX/bin/hermes -> lib/hermes-agent/bin/hermes.

set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# The container digest is a pm pin (the termux-docker package); read it
# from the single lock beside every other third-party artifact pin.
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
DIGEST="$(cd "$REPO_ROOT" && python3 -c 'import sys; sys.path.insert(0, "."); from pm.lock import termux_docker_digest; print(termux_docker_digest())')"
[ -n "$DIGEST" ] || { printf 'termux-docker digest missing from pm/lock.json\n' >&2; exit 1; }
# The derived builder image (toolchain pre-baked) when CI provides it;
# the bare pinned base otherwise. Its tag IS this lock digest (short
# form), so a lock bump rolls the builder image with the base.
IMAGE="${TERMUX_BUILDER_IMAGE:-termux/termux-docker@$DIGEST}"

REPO=""
TAG=""
PAYLOAD=""
OUT=""

usage() { printf 'usage: build_deb.sh --repo <dir> --tag <tag> --payload <dir> --out <dir>\n' >&2; exit 2; }
log()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'build_deb: FAILED: %s\n' "$*" >&2; exit 1; }

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo) REPO="${2:?}"; shift 2 ;;
        --tag) TAG="${2:?}"; shift 2 ;;
        --payload) PAYLOAD="${2:?}"; shift 2 ;;
        --out) OUT="${2:?}"; shift 2 ;;
        *) usage ;;
    esac
done
[ -n "$REPO" ] && [ -n "$TAG" ] && [ -n "$PAYLOAD" ] && [ -n "$OUT" ] || usage

for tool in python3 docker dpkg-deb jq; do
    command -v "$tool" >/dev/null || fail "missing tool: $tool"
done

REPO_ABS="$(cd "$REPO" && pwd)"
PAYLOAD_ABS="$(cd "$PAYLOAD" && pwd)"
OUT_ABS="$(mkdir -p "$OUT" && cd "$OUT" && pwd)"

# [0] Provenance: the tag must be real in the checkout; the payload must be
# the built tree of that checkout, not some other directory. The commit is
# captured ONCE here and reused for the install stamp below.
COMMIT="$(git -C "$REPO_ABS" rev-parse --verify "refs/tags/$TAG^{commit}")" \
    || fail "tag $TAG not found in $REPO_ABS"
for d in python node uv npm ffmpeg ripgrep runtime-libs app wheelhouse; do
    [ -d "$PAYLOAD_ABS/$d" ] || fail "payload missing $d/ -- run termux_build.sh + build_cpython.sh + build_node.sh first"
done
PYBIN_REL="data/data/com.termux/files/usr/bin/python3.11"
[ -f "$PAYLOAD_ABS/python/$PYBIN_REL" ] || fail "payload python tree lacks $PYBIN_REL"
NODEBIN_REL="data/data/com.termux/files/usr/bin/node"
[ -f "$PAYLOAD_ABS/node/$NODEBIN_REL" ] || fail "payload node tree lacks $NODEBIN_REL"

PKG="hermes-agent"

# [1] Version derivation: pure function in deb_version.py, tested separately.
log "Deriving Debian version from tag $TAG"
DEB_VERSION="$(python3 "$HERE/deb_version.py" "$TAG")" || fail "version derivation failed for tag $TAG"
log "Package version: $DEB_VERSION"

# [2] Assemble the venv offline, INSIDE the pinned container: the staged
# interpreter is bionic/arm64 and cannot run on this host. Completeness is
# enforced by construction: --no-index means a missing wheel fails loudly.
# The container sees the payload at its real PREFIX path; the venv is built
# staged trees so the shipped venv's absolute shebangs point at the REAL
# $PREFIX path they will occupy on-device ($PREFIX is contractual).
log "Creating venv with the bundled CPython (inside the container)"
if [ -d "$PAYLOAD_ABS/venv" ]; then rm -rf "$PAYLOAD_ABS/venv"; fi
# The venv's dep list: the resolved graph with markers intact (the installer
# evaluates them on bionic) and documented android build misses skipped --
# uv pip check tolerates the app importing without them (its relay exporter
# is the only casualty). Generated host-side; consumed in-container.
python3 - "$PAYLOAD_ABS/.work/resolved.txt" "$PAYLOAD_ABS/.work/resolved-reqs.txt" <<'PYREQS' \
    || fail "deb-venv reqs generation failed"
import sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
MISSES = {"nemo-relay"}
out = []
for line in src.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    parts = line.split("\t")
    name, spec, marker = parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""
    if name in MISSES:
        continue
    req = f"{name}{spec.strip()}" if spec.strip() else name
    if marker:
        req += f" ; {marker}"
    out.append(req)
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(chr(10).join(out) + chr(10), encoding="utf-8")
PYREQS
# The bind mount is runner-owned: the container (any uid) can only write
# into a dir the HOST pre-created with open perms (same as the wheelhouse).
mkdir -p "$PAYLOAD_ABS/venv"
chmod 0777 "$PAYLOAD_ABS/venv"
# Mount the payload at its REAL on-device path: the venv records
# absolute paths (interpreter symlink, pyvenv.cfg) that must be correct
# on-device from birth -- a /payload alias would bake container paths in.
docker run --rm --platform linux/arm64 \
    --user root --network none \
    -v "$PAYLOAD_ABS/python:/data/data/com.termux/files/usr/lib/hermes-agent/tools/python" \
    -v "$PAYLOAD_ABS/node:/data/data/com.termux/files/usr/lib/hermes-agent/tools/node" \
    -v "$PAYLOAD_ABS/uv:/data/data/com.termux/files/usr/lib/hermes-agent/tools/uv" \
    -v "$PAYLOAD_ABS/runtime-libs:/data/data/com.termux/files/usr/lib/hermes-agent/runtime-libs" \
    -v "$PAYLOAD_ABS/wheelhouse:/data/data/com.termux/files/usr/lib/hermes-agent/wheelhouse" \
    -v "$PAYLOAD_ABS/.work:/data/data/com.termux/files/usr/lib/hermes-agent/.work" \
    -v "$PAYLOAD_ABS/venv:/data/data/com.termux/files/usr/lib/hermes-agent/venv" \
    "$IMAGE" bash -c '
        set -euo pipefail
        export PREFIX=/data/data/com.termux/files/usr
        export PATH="$PREFIX/bin:${PATH:-/usr/bin:/bin}"
        # The staged binary is dynamically linked against its OWN tree lib;
        # the container linker needs to be told where it lives (same fix as
        # the wheelhouse container half).
        export LD_LIBRARY_PATH="$PREFIX/lib/hermes-agent/tools/python$PREFIX/lib:$PREFIX/lib/hermes-agent/tools/node$PREFIX/lib:$PREFIX/lib/hermes-agent/runtime-libs/lib:$PREFIX/lib"
        # The staged tree is mounted at its REAL $PREFIX path so the venv
        # recorded absolute paths are correct on-device from birth.
        mkdir -p "$PREFIX" 2>/dev/null || true
        PY="$PREFIX/lib/hermes-agent/tools/python$PREFIX/bin/python3.11"
        UV="$PREFIX/lib/hermes-agent/tools/uv$PREFIX/bin/uv"
                # Desktop payload canon: the venv holds the DEPENDENCY tree only;
        # the app runs from its own directory via PYTHONPATH (the wheel
        # build is deliberately blocked in setup.py -- Hermes is not a
        # pip-installable package by design).
        # The payload is mounted at its ON-DEVICE path ($PREFIX/lib/
        # hermes-agent) so every absolute path the venv records --
        # interpreter symlink, pyvenv.cfg home -- is correct after
        # dpkg installs the tree to exactly that location.
        "$UV" venv --python "$PY" "$PREFIX/lib/hermes-agent/venv"
        # The dep graph with markers intact (the installer evaluates
        # them on bionic); documented android build misses skipped --
        # nemo-relay is the only casualty (the relay exporter).
        "$UV" pip install --python "$PREFIX/lib/hermes-agent/venv/bin/python" \
            --offline --no-index --only-binary :all: --find-links "$PREFIX/lib/hermes-agent/wheelhouse" \
            -r "$PREFIX/lib/hermes-agent/.work/resolved-reqs.txt"
        "$UV" pip check --python "$PREFIX/lib/hermes-agent/venv/bin/python"
    ' || fail "venv assembly failed inside the container (offline wheelhouse install)"

# The install-method stamp (code-scoped, next to hermes_cli/): the deb IS
# the Termux apt distribution, and detect_install_method reads this marker
# to route hermes update -> pkg upgrade remediation.
printf 'apt\n' > "$PAYLOAD_ABS/app/.install_method"

# [3] Entry functions come from the archived project's script declarations.
log "Writing trampolines"
python3 "$HERE/launchers.py" --payload "$PAYLOAD_ABS"

# [4] Install stamp: provenance for the steward contract (distribution
# apt-termux -> update/uninstall refuse with pkg remediation). Written by the
# canonical writer (same one docker/nix/desktop use) so the schema stays
# identical across packagers; the tag rides in via HERMES_PAYLOAD_TAG.
log "Writing app/install-stamp.json"
HERMES_PAYLOAD_TAG="$TAG" \
HERMES_DESKTOP_VARIANT=bundled \
python3 "$REPO_ABS/scripts/write_install_stamp.py" \
    --output "$PAYLOAD_ABS/app/install-stamp.json" \
    --commit "$COMMIT" \
    --distribution apt-termux \
    --update-mechanism external \
    --source bundle \
    || fail "stamp write failed"

# [5]+[6] Staging dir: DEBIAN/ control + payload under lib/hermes-agent/.
log "Staging the package tree"
STAGE="$OUT_ABS/.stage-$DEB_VERSION"
rm -rf "$STAGE"
# Termux debs store their payload at the ANDROID-fs-rooted on-device
# path (data/data/com.termux/files/usr/...): on-device dpkg extracts
# at / and only /data/data is writable -- a ./lib staging would make
# dpkg try to create /lib and fail on the read-only root.
ROOT_IN_DEB=data/data/com.termux/files/usr
DEST="$STAGE/$ROOT_IN_DEB/lib/hermes-agent"
mkdir -p "$STAGE/DEBIAN" "$DEST/tools"
for tool in python node uv npm ffmpeg ripgrep; do
    cp -a "$PAYLOAD_ABS/$tool" "$DEST/tools/"
done
cp -a "$PAYLOAD_ABS/runtime-libs" "$PAYLOAD_ABS/app" "$PAYLOAD_ABS/venv" "$PAYLOAD_ABS/bin" "$DEST/"
python3 "$HERE/payload_facts.py" "$DEST" "$PAYLOAD_ABS/.work/build_set.txt"

python3 "$HERE/launchers.py" --payload "$DEST" --control "$STAGE/DEBIAN"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $DEB_VERSION
Architecture: aarch64
Maintainer: Nous Research
Description: Hermes Agent CLI for Termux (self-contained bundled python/node/venv)
Installed-Size: $(du -sk "$STAGE/$ROOT_IN_DEB" | cut -f1)
EOF
# Self-contained: no Depends line at all. Our python, node and venv ship inside.

# [7] Validation hook: install into a FRESH container of the pinned image and
# smoke-test the exact binaries the phone will run. No opt-out.
log "Validating in a fresh pinned termux-docker container"
DEB="$OUT_ABS/${PKG}_${DEB_VERSION}_aarch64.deb"
rm -f "$DEB"
# Use xz for both archive members so Termux dpkg can extract the package.
dpkg-deb --build -Zxz --root-owner-group "$STAGE" "$DEB" || fail "dpkg-deb --build failed"
rm -rf "$STAGE"
[ -f "$DEB" ] || fail "dpkg-deb did not produce $DEB"

# The bare rootfs has no build toolchain to hide a missing payload library.
docker run --rm --platform linux/arm64 \
    --user 1000:1000 --network none \
    -v "$DEB:/tmp/pkg.deb:ro" \
    -v "$HERE/check_deb.sh:/tmp/check.sh:ro" \
    -v "$HERE/validate_installed.py:/tmp/validate_installed.py:ro" \
    "termux/termux-docker@$DIGEST" bash /tmp/check.sh \
    || fail "container validation failed"

log "Built $DEB (validated)"
