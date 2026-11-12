#!/bin/sh
# install.sh - studio-pulse for macOS (Apple Silicon or Intel) and Linux.
# No sudo and no system Python needed: uv installs Python and the tool for this user.
#
#   curl -LsSf https://raw.githubusercontent.com/emirhann-akca/studio-pulse/main/install.sh | sh
#
# Source: the wheel of the v0.2.0 GitHub release when it exists; when that
# download answers 404 (no release published), the main-branch archive, built
# locally by uv. The installer says which one it used.
#
# Options (environment variables):
#   PULSE_SOURCE       wheel or source archive (path or URL) to install; skips the release lookup
#   PULSE_ARCHIVE_URL  archive used when there is no release (default: the main branch zip)
#   PULSE_RELEASE_BASE where release wheels are looked up (default: this repository's releases)
#   PULSE_EXTRAS   extras to install, default "gcp,ua" (BigQuery / Cloud Storage, ad-network
#                  APIs); add "forecast" or "stores"; "none" for none
#   PULSE_PYTHON   Python version for the tool, default 3.13 (uv downloads it if missing)
#   PULSE_DEMO=1   build the demo dashboard afterwards (in PULSE_DEMO_DIR, default ~/studio-pulse-demo)
#   PULSE_NO_SHORTCUT=1   do not add ~/Applications/Studio Pulse.app (macOS) or the app-menu entry (Linux)
#   UV_NO_MODIFY_PATH=1   leave shell profiles alone
set -eu

VERSION="0.2.0"
UV_VERSION="0.13.7"   # pinned; Astral's install.sh verifies the SHA-256 of what it downloads
REPO="https://github.com/emirhann-akca/studio-pulse"
PYTHON="${PULSE_PYTHON:-3.13}"
EXTRAS="${PULSE_EXTRAS:-gcp,ua}"
RELEASE_BASE="${PULSE_RELEASE_BASE:-$REPO/releases/download}"
ARCHIVE="${PULSE_ARCHIVE_URL:-$REPO/archive/refs/heads/main.zip}"

say() { printf 'pulse-install: %s\n' "$*"; }
die() { printf 'pulse-install: error: %s\n' "$*" >&2; exit 1; }

choose_source() {
    if [ -n "${PULSE_SOURCE:-}" ]; then
        SOURCE="$PULSE_SOURCE"
        say "source: PULSE_SOURCE ($SOURCE)"
        return
    fi
    WHEEL="$RELEASE_BASE/v$VERSION/studio_pulse-$VERSION-py3-none-any.whl"
    # One byte of the wheel is enough to tell a published release from a 404.
    CODE=$(curl -sL -r 0-0 -o /dev/null -w '%{http_code}' "$WHEEL" 2>/dev/null || true)
    case "$CODE" in
        200|206)
            SOURCE="$WHEEL"
            say "source: release wheel v$VERSION ($WHEEL)" ;;
        404)
            SOURCE="$ARCHIVE"
            say "no release wheel for v$VERSION (HTTP 404); source: branch archive ($ARCHIVE)" ;;
        *)
            die "cannot reach $WHEEL (HTTP ${CODE:-none}); check the connection or set PULSE_SOURCE" ;;
    esac
}

main() {
    command -v curl >/dev/null 2>&1 || die "curl is needed"
    choose_source
    UV_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"
    if command -v uv >/dev/null 2>&1; then
        UV=$(command -v uv)
    elif [ -x "$UV_DIR/uv" ]; then
        UV="$UV_DIR/uv"
    else
        say "installing uv $UV_VERSION into $UV_DIR"
        curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh || die "uv installation failed"
        UV="$UV_DIR/uv"
        [ -x "$UV" ] || die "uv not found at $UV after installation"
    fi
    say "using $("$UV" --version)"

    case "$SOURCE" in
        /*) SOURCE="file://$(printf '%s' "$SOURCE" | sed 's/ /%20/g')" ;;
    esac
    SPEC="studio-pulse"
    if [ -n "$EXTRAS" ] && [ "$EXTRAS" != "none" ]; then SPEC="studio-pulse[$EXTRAS]"; fi
    say "installing $SPEC with Python $PYTHON (uv downloads Python if it is missing)"
    # --refresh-package: a branch archive keeps its URL while its contents change
    "$UV" tool install --force --refresh-package studio-pulse --python "$PYTHON" "$SPEC @ $SOURCE" \
        || die "uv tool install failed"

    BIN=$("$UV" tool dir --bin)
    if [ -z "${UV_NO_MODIFY_PATH:-}" ]; then
        "$UV" tool update-shell >/dev/null 2>&1 || true
    fi
    "$BIN/pulse" doctor || die "pulse doctor reported a problem (see above)"

    LAUNCHER=""
    if [ "${PULSE_NO_SHORTCUT:-0}" != "1" ]; then
        if "$BIN/pulse" app --install-shortcut; then
            LAUNCHER=1
        else
            say "could not add the app launcher; 'pulse app' opens the same window"
        fi
    fi

    if [ "${PULSE_DEMO:-0}" = "1" ]; then
        DEMO="${PULSE_DEMO_DIR:-$HOME/studio-pulse-demo}"
        mkdir -p "$DEMO"
        (cd "$DEMO" && "$BIN/pulse" demo --scale 0.3) || die "pulse demo failed"
        say "demo dashboard: $DEMO/demo-site/index.html"
    fi
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) say "open a new terminal so that 'pulse' is on your PATH ($BIN)" ;;
    esac
    if [ -n "$LAUNCHER" ] && [ "$(uname -s)" = "Darwin" ]; then
        say "done. Open 'Studio Pulse' from Launchpad, Spotlight or ~/Applications (or run: pulse app)"
    elif [ -n "$LAUNCHER" ]; then
        say "done. Open 'Studio Pulse' from your applications menu (or run: pulse app)"
    else
        say "done. Next: pulse app    (or: pulse serve --config <your pulse.local.yaml>)"
    fi
}

main "$@"
