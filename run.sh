#!/usr/bin/env bash
# ============================================================
# TikTok Riddle Automation — Daemon Launcher
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"
PID_FILE="$SCRIPT_DIR/.daemon.pid"
LOG_FILE="$SCRIPT_DIR/daemon.log"
PYTHON="$VENV_DIR/bin/python"
DAEMON_SCRIPT="$SCRIPT_DIR/daemon.py"
ENV_FILE="$SCRIPT_DIR/.env"

# ---- Load .env if present ----
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

# ---- Check ffmpeg ----
if ! command -v ffmpeg &>/dev/null; then
    echo "❌  FFmpeg not found. Install it with:"
    echo "    brew install ffmpeg"
    exit 1
fi

# ---- Setup virtualenv ----
if [ ! -d "$VENV_DIR" ]; then
    echo "🔧  Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# ---- Install / upgrade dependencies ----
echo "📦  Checking Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet 2>/dev/null || true
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" --quiet 2>/dev/null || echo "⚠️  Pip update skipped (offline or already satisfied)."

# ---- Check Ollama ----
if ! curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then
    echo "⚠️   Ollama not running. Starting it in background..."
    ollama serve &>/dev/null &
    sleep 2
    if curl -sf "http://localhost:11434/api/tags" >/dev/null 2>&1; then
        echo "✅  Ollama started."
    else
        echo "⚠️   Ollama could not be started automatically."
        echo "    Start it manually with: ollama serve"
        echo "    The daemon will use raw sheet explanations as fallback."
    fi
fi

# ============================================================
# COMMANDS
# ============================================================
ACTION="${1:-foreground}"

case "$ACTION" in

    foreground|"")
        echo ""
        echo "🚀  Starting TikTok Riddle Daemon (foreground — Ctrl+C to stop)..."
        echo ""
        exec "$PYTHON" "$DAEMON_SCRIPT"
        ;;

    background)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "⚠️   Daemon is already running (PID: $(cat "$PID_FILE"))."
            echo "    Use './run.sh stop' to stop it first."
            exit 1
        fi
        echo "🚀  Starting TikTok Riddle Daemon (background)..."
        nohup "$PYTHON" "$DAEMON_SCRIPT" >> "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "✅  Daemon started (PID: $(cat "$PID_FILE")). Log: $LOG_FILE"
        ;;

    stop)
        if [ -f "$PID_FILE" ]; then
            PID="$(cat "$PID_FILE")"
            if kill -0 "$PID" 2>/dev/null; then
                echo "🛑  Stopping daemon (PID: $PID)..."
                kill -TERM "$PID"
                sleep 2
                if kill -0 "$PID" 2>/dev/null; then
                    kill -KILL "$PID"
                fi
                echo "✅  Daemon stopped."
            else
                echo "⚠️   PID $PID not running."
            fi
            rm -f "$PID_FILE"
        else
            echo "⚠️   No PID file found — daemon may not be running."
        fi
        ;;

    status)
        if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
            echo "✅  Daemon is RUNNING (PID: $(cat "$PID_FILE"))"
        else
            echo "💤  Daemon is NOT running."
        fi
        ;;

    logs)
        if [ -f "$LOG_FILE" ]; then
            tail -f "$LOG_FILE"
        else
            echo "No log file found at $LOG_FILE."
        fi
        ;;

    restart)
        "$0" stop
        sleep 1
        "$0" background
        ;;

    *)
        echo "Usage: $0 [foreground|background|stop|status|logs|restart]"
        exit 1
        ;;
esac
