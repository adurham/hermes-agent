#!/usr/bin/env bash
# hermes-update-rehearsal.sh — for an EXISTING Hermes install.
#
# Two steps:
#   pre   back up everything, then point the install's update source at a
#         custom repo + ref so `hermes update` pulls it. Prints what to do next.
#   post  undo all of it: remove the redirect, wipe, and restore the backup.
#
# Plus `status`, which only prints. This script never judges your install: it
# reports what it did and stops. Whether the update worked is for you to see.
#
# Read PLAN.md (next to this script) first.
#
#   ./hermes-update-rehearsal.sh pre  --source <git-url> --ref <branch-or-tag>
#   # ... run `hermes update`, use Hermes, test whatever you need ...
#   ./hermes-update-rehearsal.sh post
#
# Options:
#   --source URL   repo to pull the update from (default: the rehearsal fork)
#   --ref REV      branch or tag in that repo (default: main)
#   --backup-root DIR   where the backup lives (default ~/hermes-update-rehearsal)
#   --yes          post: skip the confirmation
#
# The backup is the ENTIRE HERMES_HOME plus the desktop app's Electron userData,
# the `hermes` shims on PATH, and your global git config. Expect it to be as
# large as your install and to take a few minutes.
#
# Requires: python3, tar, git. `pre` needs network access to --source.

set -euo pipefail

OFFICIAL_HTTPS="https://github.com/NousResearch/hermes-agent.git"
OFFICIAL_SSH="git@github.com:NousResearch/hermes-agent.git"
DEFAULT_SOURCE="https://github.com/ethernet8023/hermes-agent.git"
DEFAULT_REF="main"

SUBCMD=""
BACKUP_ROOT="${HOME}/hermes-update-rehearsal"
SOURCE="$DEFAULT_SOURCE"
REF="$DEFAULT_REF"
ASSUME_YES=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK %s\n' "$*"; }
warn() { printf '  WARN %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n=== %s ===\n' "$*"; }

usage() {
  if [ -n "$SELF" ] && [ -r "$SELF" ]; then sed -n '2,26p' "$SELF"; exit 0; fi
  cat <<'EOF'
hermes-update-rehearsal.sh -- run against an EXISTING Hermes install.

  pre     back up everything, then point the update source at a custom repo+ref
  post    undo all of it: remove the redirect, wipe, restore the backup
  status  print what is prepared (read-only; nothing is touched)

Options:
  --source URL        repo to pull the update from (default: the rehearsal fork)
  --ref REV           branch or tag in that repo (default: main)
  --backup-root DIR   where the backup lives (default ~/hermes-update-rehearsal)
  --yes               post: skip the confirmation

`pre` reports what it did and prints the next commands. It never judges your
install; whether the update worked is for you to see.
EOF
  exit 0
}

# How to name ourselves in the instructions we print. Piped into a shell
# (`curl ... | bash -s -- pre`) there is no path to point at and "$0" is just
# "bash", which would print a command that does not exist.
SELF="$0"
case "$(basename "$SELF")" in bash|sh|dash|zsh|-bash|-sh|'') SELF="" ;; esac
case "$SELF" in /dev/fd/*|/dev/stdin|/proc/self/fd/*) SELF="" ;; esac
SELF_PIPED=""
if [ -n "$SELF" ]; then SELF_CMD="$SELF"; else SELF_CMD="hermes-update-rehearsal.sh"; SELF_PIPED=1; fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    pre|post|status) SUBCMD="$1"; shift ;;
    --source)      [ "$#" -ge 2 ] || die "--source needs a value"; SOURCE="$2"; shift 2 ;;
    --ref)         [ "$#" -ge 2 ] || die "--ref needs a value";    REF="$2";    shift 2 ;;
    --backup-root) [ "$#" -ge 2 ] || die "--backup-root needs a value"; BACKUP_ROOT="$2"; shift 2 ;;
    --yes|-y)      ASSUME_YES=1; shift ;;
    -h|--help)     usage ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$SUBCMD" ] || usage

# Probe, don't assume: on Windows `python3` is often the Microsoft Store alias
# stub, which exits non-zero for every real invocation.
PYTHON=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'pass' >/dev/null 2>&1; then
    PYTHON="$cand"; break
  fi
done
[ -n "$PYTHON" ] || die "python3 is required (fingerprints)"
command -v git >/dev/null 2>&1 || die "git is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

SNAP=""
ARMED=""

# The durable state `post` reports on after restoring. Regenerable trees
# (caches, logs, dependency dirs) are deliberately absent, and `skills/` is
# reported but never judged: the product syncs the bundled library into it on
# startup and after every update, so it changes legitimately.
durable_tops() {
  printf '%s\n' config.yaml .env auth.json state.db gateway_state.json \
    memories skills cron plugins photon desktop-plugins tui-widgets skins pets \
    sessions profiles
}

# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

# Under git-bash/cygwin, POSIX paths are not translated for native tools
# (git.exe, python.exe), so normalise them. On macOS cygpath is absent: no-op.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1" 2>/dev/null || printf '%s' "$1"
  else
    printf '%s' "$1"
  fi
}

resolve_paths() {
  local suffix="${HERMES_DATA_DIR_SUFFIX:-}"
  if [ -n "${HERMES_HOME:-}" ]; then
    HERMES_HOME="$(cd "$HERMES_HOME" 2>/dev/null && pwd || printf '%s' "$HERMES_HOME")"
  else
    HERMES_HOME="$HOME/.hermes${suffix}"
  fi
  # A profile home sits inside the shared root; both are protected.
  if [ "$(basename "$(dirname "$HERMES_HOME")")" = "profiles" ]; then
    HERMES_ROOT="$(dirname "$(dirname "$HERMES_HOME")")"
  else
    HERMES_ROOT="$HERMES_HOME"
  fi
  INSTALL_DIR="$HERMES_HOME/hermes-agent"
  if [ -n "${HERMES_DESKTOP_USER_DATA_DIR:-}" ]; then
    USERDATA_DIR="$HERMES_DESKTOP_USER_DATA_DIR"; USERDATA_SOURCE="env"
  else
    USERDATA_DIR="$HOME/Library/Application Support/Hermes${suffix}"; USERDATA_SOURCE="default"
  fi
  SHIM_CANDIDATES=(
    "$INSTALL_DIR/.hermes/bin/hermes" "$INSTALL_DIR/.hermes/bin/hermes-acp"
    "$INSTALL_DIR/.hermes/bin/hermes-agent"
    "$HOME/.local/bin/hermes" "$HOME/.local/bin/hermes-acp" "$HOME/.local/bin/hermes-agent"
    "$HERMES_ROOT/bin/hermes" "$HERMES_ROOT/bin/hermes-acp" "$HERMES_ROOT/bin/hermes-agent"
    "/usr/local/bin/hermes" "/usr/local/bin/hermes-acp"
  )
  BACKUP_ROOT="$(native_path "$BACKUP_ROOT")"
  HERMES_HOME="$(native_path "$HERMES_HOME")"
  HERMES_ROOT="$(native_path "$HERMES_ROOT")"
  INSTALL_DIR="$(native_path "$INSTALL_DIR")"
  USERDATA_DIR="$(native_path "$USERDATA_DIR")"
  local i
  for i in "${!SHIM_CANDIDATES[@]}"; do
    SHIM_CANDIDATES[i]="$(native_path "${SHIM_CANDIDATES[$i]}")"
  done
}

global_git_config_paths() {
  printf '%s\n' "$HOME/.gitconfig"
  # git also reads the XDG path, and XDG_CONFIG_HOME DEFAULTS to ~/.config --
  # checking only when the var is exported misses every XDG-style setup, so the
  # backup silently captured nothing for those users.
  printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/git/config"
  return 0
}

first_existing_global_config() {
  local cfg
  while IFS= read -r cfg; do
    [ -f "$cfg" ] && { printf '%s' "$cfg"; return 0; }
  done < <(global_git_config_paths)
  return 1
}

latest_backup_root() {
  [ -d "$BACKUP_ROOT" ] || return 1
  local newest
  newest="$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | sort | tail -1 || true)"
  [ -n "$newest" ] || return 1
  printf '%s' "${newest%/}"
}

load_snapshot() {
  SNAP="$(latest_backup_root)" || die "no backup found under $BACKUP_ROOT — run 'pre' first"
  ARMED="$SNAP/armed"
  [ -f "$SNAP/manifest.json" ] || die "$SNAP is not a rehearsal backup (no manifest.json)"
  local recorded
  recorded="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["hermes_home"])' "$SNAP/manifest.json")"
  [ "$recorded" = "$HERMES_HOME" ] \
    || die "that backup belongs to HERMES_HOME=$recorded, not $HERMES_HOME; pass --backup-root to pick the right one"
}

# ---------------------------------------------------------------------------
# fingerprints (used only to report how exact a restore was)
# ---------------------------------------------------------------------------

fingerprint_home() {
  local out="$1"
  REHEARSAL_TOPS="$(durable_tops | tr '\n' ' ')" "$PYTHON" - "$HERMES_HOME" "$out" <<'PY'
import hashlib, os, sqlite3, sys

root, out = sys.argv[1], sys.argv[2]
SKIP_DIRS = {"node_modules", ".venv", "venv", "site-packages", "__pycache__", ".git",
             ".cache", ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             "backups", "state-snapshots", "checkpoints", "hermes-agent",
             "browser-profiles", "browser-profile", "models", "runtimes", "node"}
SKIP_SUF = (".pyc", ".pyo", ".db-wal", ".db-shm", ".db-journal")
TOP = os.environ.get("REHEARSAL_TOPS", "").split()

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def db_counts(path):
    """Bytes are meaningless for a live SQLite db; count rows instead."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            parts = [f"{t}={con.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}"
                     for t in sorted(tables & {"sessions", "messages", "usage", "cron_jobs"})]
            return "db\t" + ";".join(parts) if parts else None
        finally:
            con.close()
    except Exception:
        return None

def render(path, rel):
    if os.path.islink(path):
        return f"link\t{rel}\t{os.readlink(path)}"
    if os.path.isdir(path):
        return f"dir\t{rel}"
    if os.path.basename(path) == "state.db":
        counts = db_counts(path)
        if counts:
            return f"{counts}\t{rel}"
    return f"file\t{rel}\t{sha(path)}"

def walk(base):
    for cur, dirs, files in os.walk(base):
        keep = []
        for name in sorted(dirs):
            if name in SKIP_DIRS:
                continue
            full = os.path.join(cur, name)
            # os.walk lists a symlink-to-dir in `dirs` and refuses to descend,
            # so record it here or it vanishes from the fingerprint.
            if os.path.islink(full):
                yield full
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            if not name.endswith(SKIP_SUF):
                yield os.path.join(cur, name)
        if not files and not dirs:
            yield cur

lines = []
for entry in TOP:
    top = os.path.join(root, entry)
    if not os.path.lexists(top):
        continue
    lines.append(render(top, entry))
    if os.path.isdir(top) and not os.path.islink(top):
        for path in walk(top):
            lines.append(render(path, os.path.relpath(path, root).replace(os.sep, "/")))

with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(sorted(lines)) + ("\n" if lines else ""))
print(f"{len(lines)} state entries fingerprinted")
PY
}

fingerprint_userdata() {
  local out="$1"
  [ -d "$USERDATA_DIR" ] || { : > "$out"; return 0; }
  "$PYTHON" - "$USERDATA_DIR" "$out" <<'PY'
import hashlib, os, sys

root, out = sys.argv[1], sys.argv[2]
SKIP_DIRS = {"Cache", "Code Cache", "GPUCache", "DawnGraphiteCache", "DawnWebGPUCache",
             "ShaderCache", "Crashpad", "CachedData", "blob_storage"}

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

lines = []
for cur, dirs, files in os.walk(root):
    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
    for name in sorted(files):
        path = os.path.join(cur, name)
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        lines.append(f"link\t{rel}\t{os.readlink(path)}" if os.path.islink(path)
                     else f"file\t{rel}\t{sha(path)}")

with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(sorted(lines)) + ("\n" if lines else ""))
print(f"{len(lines)} userData entries fingerprinted")
PY
}

# ---------------------------------------------------------------------------
# pre: back up, then point the install at the rehearsal source
# ---------------------------------------------------------------------------

cmd_pre() {
  resolve_paths
  step "your install"
  say "HERMES_HOME   $HERMES_HOME"
  say "install       $INSTALL_DIR"
  say "desktop data  $USERDATA_DIR ($USERDATA_SOURCE)"
  say "backup to     $BACKUP_ROOT"
  [ -d "$HERMES_HOME" ] || die "no HERMES_HOME at $HERMES_HOME"
  [ -d "$INSTALL_DIR/.git" ] || die "no git checkout at $INSTALL_DIR — this tool covers source installs"

  step "before we start (nothing here is a pass/fail, just read it)"
  if command -v pgrep >/dev/null 2>&1; then
    local procs
    procs="$(pgrep -fl hermes 2>/dev/null | grep -v 'hermes-update-rehearsal' || true)"
    if [ -n "$procs" ]; then
      warn "Hermes looks like it is running — close the desktop app and the gateway"
      warn "before you run 'hermes update', or the dependency sync may fail:"
      printf '    %s\n' "$procs"
    else
      ok "no Hermes processes running"
    fi
  fi
  local n
  n="$( { git config --global --get-regexp '^url\.' 2>/dev/null || true; git -C "$INSTALL_DIR" config --local --get-regexp '^url\.' 2>/dev/null || true; } | wc -l | tr -d ' ')"
  if [ "$n" = 0 ]; then ok "global git config has no URL rewrites"
  else warn "$n existing url.* insteadOf entr(y/ies) in your git config; we add more and remove only ours"; fi

  SNAP="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
  [ ! -e "$SNAP" ] || die "backup dir already exists: $SNAP"
  mkdir -p "$SNAP/shims" "$SNAP/armed"
  ARMED="$SNAP/armed"

  step "copying your entire HERMES_HOME"
  local started elapsed
  started="$SECONDS"
  # No excludes: checkout, venv, PM store and node_modules come too, so `post`
  # is a true rollback rather than a re-download. SQLite sidecars travel WITH
  # their db on purpose (a raw copy of db+wal+shm is consistent).
  tar -czf "$SNAP/hermes-home.tgz" -C "$HERMES_HOME" . || die "backup failed (tar)"
  elapsed=$((SECONDS - started))
  ok "hermes-home.tgz ($(du -h "$SNAP/hermes-home.tgz" | cut -f1), ${elapsed}s)"

  step "recording git facts"
  {
    printf 'head\t%s\n'    "$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    printf 'branch\t%s\n'  "$(git -C "$INSTALL_DIR" branch --show-current || true)"
    printf 'stashes\t%s\n' "$(git -C "$INSTALL_DIR" stash list | wc -l | tr -d ' ')"
  } > "$SNAP/checkout.txt"
  : > "$SNAP/remotes.txt"
  local remote
  while IFS= read -r remote; do
    [ -n "$remote" ] || continue
    printf '%s\t%s\n' "$remote" "$(git -C "$INSTALL_DIR" config --get "remote.$remote.url")" \
      >> "$SNAP/remotes.txt"
  done < <(git -C "$INSTALL_DIR" remote)
  ok "checkout at $(awk -F'\t' '$1=="head"{print $2}' "$SNAP/checkout.txt")"

  step "copying the desktop app's data"
  if [ -d "$USERDATA_DIR" ]; then
    tar -czf "$SNAP/electron-userdata.tgz" -C "$USERDATA_DIR" . || die "userData backup failed"
    ok "electron-userdata.tgz ($(du -h "$SNAP/electron-userdata.tgz" | cut -f1))"
  else
    warn "no Electron userData at $USERDATA_DIR (desktop app not installed?)"
  fi

  step "copying the hermes shims on your PATH"
  : > "$SNAP/shims.txt"; : > "$SNAP/shims-links.txt"
  local shim
  for shim in "${SHIM_CANDIDATES[@]}"; do
    [ -e "$shim" ] || [ -L "$shim" ] || continue
    printf '%s\n' "$shim" >> "$SNAP/shims.txt"
    if [ -L "$shim" ]; then
      printf '%s\t%s\n' "$shim" "$(readlink "$shim")" >> "$SNAP/shims-links.txt"
    else
      cp -p "$shim" "$SNAP/shims/$(printf '%s' "$shim" | tr '/' '_')" 2>/dev/null || warn "could not copy $shim"
    fi
  done
  if [ -s "$SNAP/shims.txt" ]; then ok "$(wc -l < "$SNAP/shims.txt" | tr -d ' ') shim path(s) recorded"
  else warn "no shims found on PATH"; fi

  step "copying your global git config"
  local cfg
  if cfg="$(first_existing_global_config)"; then
    cp -p "$cfg" "$SNAP/gitconfig.bak"; ok "saved $cfg"
  else
    warn "no global git config yet; we will create one and remove it again in post"
  fi

  step "fingerprinting (so post can tell you how exact the restore was)"
  fingerprint_home "$SNAP/fingerprint-before.txt"
  fingerprint_userdata "$SNAP/userdata-before.txt"

  "$PYTHON" - "$SNAP" "$HERMES_HOME" "$HERMES_ROOT" "$INSTALL_DIR" \
    "$USERDATA_DIR" "$USERDATA_SOURCE" "$SOURCE" "$REF" <<'PY'
import datetime, json, os, subprocess, sys
snap, home, root, install, userdata, src, source, ref = sys.argv[1:9]
def git(*args):
    try:
        return subprocess.run(["git", "-C", install, *args], capture_output=True,
                              text=True, check=False).stdout.strip()
    except Exception:
        return ""
json.dump({
    "schema": 2,
    "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "hermes_home": home,
    "hermes_root": root,
    "install_dir": install,
    "userdata_dir": userdata,
    "userdata_dir_source": src,
    "rehearsal_source": source,
    "rehearsal_ref": ref,
    "env": {k: os.environ.get(k) for k in
            ("HERMES_HOME", "HERMES_DESKTOP_USER_DATA_DIR", "HERMES_DATA_DIR_SUFFIX",
             "PHOTON_SIDECAR_DIR")},
    "checkout": {"head": git("rev-parse", "HEAD"),
                 "branch": git("branch", "--show-current"),
                 "origin": git("config", "--get", "remote.origin.url"),
                 "upstream": git("config", "--get", "remote.upstream.url")},
}, open(os.path.join(snap, "manifest.json"), "w", encoding="utf-8"), indent=2, sort_keys=True)
PY
  ok "manifest.json"

  # --- point the install at the rehearsal source ---------------------------
  step "fetching the rehearsal source"
  say "source        $SOURCE"
  say "ref           $REF"
  local serve="$SNAP/serve.git" target_sha=""
  rm -rf "$serve"
  # Prefer a single-branch clone (fast); fall back to a full bare clone so a
  # raw commit in --ref still resolves.
  if git clone --quiet --bare --branch "$REF" --single-branch "$SOURCE" "$serve" 2>/dev/null; then
    ok "cloned $REF"
  else
    rm -rf "$serve"
    git clone --quiet --bare "$SOURCE" "$serve" \
      || die "could not clone $SOURCE (network? permissions? bad --source?)"
    ok "cloned the whole repo (--ref '$REF' is not a branch/tag name)"
  fi
  target_sha="$(git -C "$serve" rev-parse --verify "${REF}^{commit}" 2>/dev/null || true)"
  [ -n "$target_sha" ] || die "--ref '$REF' was not found in $SOURCE"
  git -C "$serve" update-ref refs/heads/main "$target_sha"
  git -C "$serve" symbolic-ref HEAD refs/heads/main
  # The updater may ask for an exact SHA instead of a branch tip.
  git -C "$serve" config uploadpack.allowAnySHA1InWant true
  ok "the update will land on $target_sha"

  step "pointing your install at it"
  # insteadOf is a TRANSPORT rewrite. Your checkout's origin keeps the official
  # URL, which matters: `hermes update` resolves its channel from the archive
  # and validates it against `git config --get remote.origin.url`. Repointing
  # origin at a fork would make the update fail before any git work.
  case "$serve" in
    [A-Za-z]:*) REDIRECT="file:///$serve" ;;
    *)          REDIRECT="file://$serve" ;;
  esac
  : > "$ARMED/armed.txt"
  local url
  # The redirect belongs in the REPO-LOCAL config: it lives inside the checkout
  # this kit already owns and backs up, it cannot be root-owned or read-only
  # (the install could not have written its own repo otherwise), and `post`'s
  # wipe+restore removes it for free. A writable GLOBAL git config is a
  # dependency we do not need -- requiring it aborts on any machine whose
  # ~/.config/git/config is read-only or ACL-denied.
  for url in "$OFFICIAL_HTTPS" "$OFFICIAL_SSH"; do
    # --add: the key is multi-valued; a plain write would drop the first URL.
    git -C "$INSTALL_DIR" config --local --add "url.$REDIRECT.insteadOf" "$url" \
      || die "could not write the URL redirect into $INSTALL_DIR/.git/config"
    printf '%s\t%s\t%s\n' "$REDIRECT" "$url" local >> "$ARMED/armed.txt"
  done
  mkdir -p "$HERMES_HOME"
  touch "$HERMES_HOME/.skip_upstream_prompt"
  printf '%s\n' "$target_sha" > "$ARMED/target-sha"
  ok "official repo URL now resolves to the rehearsal copy"
  ok "created $HERMES_HOME/.skip_upstream_prompt (stops the 'add upstream remote?' prompt)"

  step "ready"
  say "your install is unchanged so far. nothing has been updated yet."
  say ""
  say "continue with the instructions provided!"
  say "your backup is at $SNAP. keep it until post has run."
}

# ---------------------------------------------------------------------------
# status (read-only)
# ---------------------------------------------------------------------------

cmd_status() {
  resolve_paths
  step "your install"
  say "HERMES_HOME   $HERMES_HOME"
  say "install       $INSTALL_DIR"
  say "desktop data  $USERDATA_DIR ($USERDATA_SOURCE)"
  say "backup root   $BACKUP_ROOT"
  step "backup"
  if ! SNAP="$(latest_backup_root)"; then
    say "none — nothing has been set up yet (run 'pre')"
    return 0
  fi
  ARMED="$SNAP/armed"
  say "latest        $SNAP"
  if [ -f "$ARMED/target-sha" ]; then
    say "prepared for  $(cat "$ARMED/target-sha")"
    say "source        $(cat "$SNAP/manifest.json" 2>/dev/null | "$PYTHON" -c 'import json,sys;d=json.load(sys.stdin);print(d.get("rehearsal_source","?"),"@",d.get("rehearsal_ref","?"))' 2>/dev/null || echo '?')"
  else
    say "prepared      no"
  fi
  say "marker        $([ -f "$HERMES_HOME/.skip_upstream_prompt" ] && echo present || echo absent)"
  local n=0
  while IFS= read -r _; do n=$((n + 1)); done \
    < <( { git config --global --get-regexp '^url\.' 2>/dev/null || true; git -C "$INSTALL_DIR" config --local --get-regexp '^url\.' 2>/dev/null || true; } )
  say "git rewrites  $n insteadOf entr(y/ies)"
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "checkout now  $(git -C "$INSTALL_DIR" rev-parse --short HEAD) ($(git -C "$INSTALL_DIR" branch --show-current))"
  fi
}

# ---------------------------------------------------------------------------
# post: undo everything
# ---------------------------------------------------------------------------

confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  local prompt="$1 [y/N] " reply=""
  # Piped into a shell (`curl ... | bash -s -- post`) stdin is THIS SCRIPT, so a
  # plain `read` hits EOF and aborts every time. Ask the terminal instead.
  # /dev/tty is still a device node with no controlling terminal, so probe by
  # OPENING it -- `[ -r /dev/tty ]` passes there and the open then fails.
  local from_tty=""
  if from_tty="$( { printf '%s' "$prompt" >/dev/tty && read -r a </dev/tty && printf '%s' "$a"; } 2>/dev/null )"; then
    reply="$from_tty"
  else
    printf '%s' "$prompt"
    read -r reply || reply=""
  fi
  case "$reply" in y|Y|yes|YES) return 0 ;; *) die "aborted — nothing was changed (re-run with --yes to skip this prompt)" ;; esac
}

unarm() {
  step "removing the URL redirect"
  if [ -f "$ARMED/armed.txt" ]; then
    local scope target
    while IFS=$'\t' read -r target _url scope; do
      [ -n "$target" ] || continue
      case "${scope:-global}" in
        local) git -C "$INSTALL_DIR" config --local --unset-all "url.$target.insteadOf" 2>/dev/null || true ;;
        *)     git config --global --unset-all "url.$target.insteadOf" 2>/dev/null || true ;;
      esac
    done < "$ARMED/armed.txt"
    # An earlier version of this kit wrote the redirect GLOBALLY; clear that too
    # so a machine that ran it is not left with a stale redirect.
    while IFS= read -r target; do
      [ -n "$target" ] || continue
      git config --global --unset-all "url.$target.insteadOf" 2>/dev/null || true
    done < <(cut -f1 "$ARMED/armed.txt" | sort -u)
    ok "removed our insteadOf entries"
  fi
  if [ -f "$SNAP/gitconfig.bak" ]; then
    local cfg
    if cfg="$(first_existing_global_config)"; then
      cp -p "$SNAP/gitconfig.bak" "$cfg" && ok "restored your global git config"
    fi
  fi
  if [ -f "$HERMES_HOME/.skip_upstream_prompt" ]; then
    rm -f "$HERMES_HOME/.skip_upstream_prompt" && ok "removed the upstream-prompt marker"
  fi
}

cmd_post() {
  resolve_paths
  load_snapshot
  step "this will delete and restore:"
  say "  $HERMES_HOME  (all of it, including the checkout)"
  say "  $USERDATA_DIR"
  say "  the shim files recorded in $SNAP/shims.txt"
  confirm "Put everything back from $SNAP?"

  unarm
  step "stopping Hermes"
  pkill -f "Hermes.app/Contents/MacOS" 2>/dev/null || true
  pkill -f "hermes gateway" 2>/dev/null || true
  ok "asked Hermes to stop (if anything was running)"

  step "clearing what the rehearsal touched"
  rm -rf "$HERMES_HOME";  ok "removed $HERMES_HOME"
  rm -rf "$USERDATA_DIR"; ok "removed $USERDATA_DIR"
  local shim
  if [ -s "$SNAP/shims.txt" ]; then
    while IFS= read -r shim; do rm -f "$shim" 2>/dev/null || true; done < "$SNAP/shims.txt"
    ok "removed the recorded shim files"
  fi

  step "restoring your HERMES_HOME"
  mkdir -p "$HERMES_HOME"
  tar -xzf "$SNAP/hermes-home.tgz" -C "$HERMES_HOME" || die "restore failed — your backup is intact at $SNAP"
  ok "restored"

  step "restoring the desktop app's data"
  if [ -f "$SNAP/electron-userdata.tgz" ]; then
    mkdir -p "$USERDATA_DIR"
    tar -xzf "$SNAP/electron-userdata.tgz" -C "$USERDATA_DIR" || die "userData restore failed (backup intact at $SNAP)"
    ok "restored"
  else
    warn "there was no desktop app data to restore"
  fi

  step "restoring the shims"
  if [ -s "$SNAP/shims.txt" ]; then
    while IFS= read -r shim; do
      local saved="$SNAP/shims/$(printf '%s' "$shim" | tr '/' '_')"
      if [ -f "$saved" ]; then
        mkdir -p "$(dirname "$shim")"; cp -p "$saved" "$shim"; ok "restored $shim"
      elif grep -qF "$shim" "$SNAP/shims-links.txt" 2>/dev/null; then
        local target; target="$(awk -v s="$shim" -F'\t' '$1==s{print $2}' "$SNAP/shims-links.txt")"
        mkdir -p "$(dirname "$shim")"; rm -f "$shim"; ln -s "$target" "$shim"
        ok "restored symlink $shim"
      fi
    done < "$SNAP/shims.txt"
  fi

  step "how exact was the restore"
  fingerprint_home "$SNAP/fingerprint-restored.txt" >/dev/null
  fingerprint_userdata "$SNAP/userdata-restored.txt" >/dev/null
  local diffs=0
  if diff -q "$SNAP/fingerprint-before.txt" "$SNAP/fingerprint-restored.txt" >/dev/null 2>&1; then
    ok "all $(wc -l < "$SNAP/fingerprint-before.txt" | tr -d ' ') state entries match your backup"
  else
    diffs=$((diffs + 1))
    diff -u "$SNAP/fingerprint-before.txt" "$SNAP/fingerprint-restored.txt" \
      > "$SNAP/fingerprint-restore.diff" 2>/dev/null || true
    warn "some state differs from the backup — details in $SNAP/fingerprint-restore.diff"
  fi
  if [ -f "$SNAP/userdata-before.txt" ] \
     && ! diff -q "$SNAP/userdata-before.txt" "$SNAP/userdata-restored.txt" >/dev/null 2>&1; then
    diffs=$((diffs + 1))
    diff -u "$SNAP/userdata-before.txt" "$SNAP/userdata-restored.txt" \
      > "$SNAP/userdata-restore.diff" 2>/dev/null || true
    warn "some desktop app data differs — details in $SNAP/userdata-restore.diff"
  else
    ok "desktop app data matches"
  fi

  step "done"
  say "Your install, your data and your git config are back as they were."
  say "Open the desktop app once and run 'hermes doctor' to confirm."
  say "Nothing was judged or changed by this script; the backup at $SNAP is"
  say "yours to keep or delete."
  [ "$diffs" = 0 ] || say "(The differences reported above are informational — see the diff files.)"
}

case "$SUBCMD" in
  pre)    cmd_pre ;;
  post)   cmd_post ;;
  status) cmd_status ;;
esac
