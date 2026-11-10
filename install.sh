#!/bin/sh
# install.sh - studio-pulse for macOS (Apple Silicon or Intel) and Linux.
# No sudo and no system Python needed: uv installs Python and the tool for this user.
#
#   curl -LsSf https://raw.githubusercontent.com/emirhann-akca/studio-pulse/v0.2.0/install.sh | sh
#
# Options (environment variables):
#   PULSE_SOURCE   wheel path or URL to install (default: the v0.2.0 release wheel)
#   PULSE_EXTRAS   extras to install, default "gcp" (BigQuery / Cloud Storage); "none" for none
#   PULSE_PYTHON   Python version for the tool, default 3.13 (uv downloads it if missing)
#   PULSE_DEMO=1   build the demo dashboard afterwards (in PULSE_DEMO_DIR, default ~/studio-pulse-demo)
#   UV_NO_MODIFY_PATH=1   leave shell profiles alone
set -eu

VERSION="0.2.0"
UV_VERSION="0.13.7"   # pinned; Astral's install.sh verifies the SHA-256 of what it downloads
REPO="https://github.com/emirhann-akca/studio-pulse"
PYTHON="${PULSE_PYTHON:-3.13}"
EXTRAS="${PULSE_EXTRAS:-gcp}"
SOURCE="${PULSE_SOURCE:-$REPO/releases/download/v$VERSION/studio_pulse-$VERSION-py3-none-any.whl}"

say() { printf 'pulse-install: %s\n' "$*"; }
die() { printf 'pulse-install: error: %s\n' "$*" >&2; exit 1; }

main() {
    UV_DIR="${UV_INSTALL_DIR:-$HOME/.local/bin}"
    if command -v uv >/dev/null 2>&1; then
        UV=$(command -v uv)
    elif [ -x "$UV_DIR/uv" ]; then
        UV="$UV_DIR/uv"
    else
        command -v curl >/dev/null 2>&1 || die "curl is needed to download uv"
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
    say "installing $SPEC $VERSION with Python $PYTHON (uv downloads Python if it is missing)"
    "$UV" tool install --force --python "$PYTHON" "$SPEC @ $SOURCE" || die "uv tool install failed"

    BIN=$("$UV" tool dir --bin)
    if [ -z "${UV_NO_MODIFY_PATH:-}" ]; then
        "$UV" tool update-shell >/dev/null 2>&1 || true
    fi
    "$BIN/pulse" doctor || die "pulse doctor reported a problem (see above)"

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
    say "done. Next: pulse demo --open    (or: pulse serve --config <your pulse.local.yaml>)"
}

main "$@"
