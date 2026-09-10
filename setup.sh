#!/bin/sh
# Setup for the opencode traffic-light widget.
# Idempotent: safe to re-run. Builds/installs platform dependencies,
# links the CLI, installs the plugin, and configures on-demand widget services.
set -e
cd "$(dirname "$0")"
SOURCE_DIR=$PWD
OS=$(uname -s)
CONFIG_DIR=${OPENCODE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/opencode}
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/opencode-traffic-light"

case "$OS" in
  Darwin|Linux) ;;
  *) echo "Unsupported platform: $OS (expected macOS or Linux)" >&2; exit 1 ;;
esac

echo "== 1. system deps =="
if [ "$OS" = Darwin ]; then
  if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "Apple Command Line Tools required. Run: xcode-select --install" >&2
    exit 1
  fi
  APP_DIR="$HOME/Library/Application Support/opencode-traffic-light"
  mkdir -p "$APP_DIR"
  BUILD=$(mktemp "$APP_DIR/.widget.XXXXXX")
  trap 'rm -f "$BUILD"' EXIT
  echo "  building native macOS widget"
  MACOSX_DEPLOYMENT_TARGET=12.0 xcrun swiftc -O -parse-as-library \
    "$SOURCE_DIR/opencode-traffic-light.swift" -o "$BUILD"
  chmod +x "$BUILD"
  mv -f "$BUILD" "$APP_DIR/opencode-traffic-light"
  trap - EXIT
else
  if python3 -c "import gi; import cairo; gi.require_foreign('cairo'); gi.require_version('Gtk','3.0'); gi.require_version('Gdk','3.0'); from gi.repository import Gtk, Gdk" 2>/dev/null; then
    echo "  gtk3 and Cairo bindings: present"
  elif command -v apt >/dev/null 2>&1; then
    echo "  installing GTK3 and Cairo bindings (sudo)"
    sudo apt update && sudo apt install -y python3-gi python3-cairo python3-gi-cairo gir1.2-gtk-3.0
  else
    echo "Install Python 3, PyGObject, GTK3, PyCairo, and the GI/Cairo bridge with your distribution's package manager, then retry." >&2
    exit 1
  fi
fi

echo "== 2. CLI =="
chmod +x traffic-light
mkdir -p "$HOME/.local/bin"
ln -sf "$SOURCE_DIR/traffic-light" "$HOME/.local/bin/traffic-light"
echo "  linked ~/.local/bin/traffic-light (ensure ~/.local/bin is on PATH)"

echo "== 3. opencode plugin =="
# Keep the path registered by older installers; new installs use auto-discovery.
PLUGIN_DIR="$CONFIG_DIR/plugins"
for config in "$CONFIG_DIR/opencode.json" "$CONFIG_DIR/opencode.jsonc" "${OPENCODE_CONFIG:-}"; do
  if [ -f "$config" ] && grep -Fq 'plugins/traffic-light/traffic-light.js' "$config"; then
    PLUGIN_DIR="$CONFIG_DIR/plugins/traffic-light"
    break
  fi
done
mkdir -p "$PLUGIN_DIR"
cp traffic-light.js "$PLUGIN_DIR/traffic-light.js"
cp traffic-light-launcher.js "$CONFIG_DIR/plugins/traffic-light-launcher.js"
if [ "$PLUGIN_DIR" = "$CONFIG_DIR/plugins/traffic-light" ]; then
  cp package.json "$PLUGIN_DIR/package.json"
fi
echo "  installed in $PLUGIN_DIR (OpenCode configuration left unchanged)"

echo "== 4. OpenCode-linked service =="
if [ "$OS" = Darwin ]; then
  LABEL=com.opencode.traffic-light
  AGENT_DIR="$HOME/Library/LaunchAgents"
  LOG_DIR="$HOME/Library/Logs/opencode-traffic-light"
  mkdir -p "$AGENT_DIR" "$LOG_DIR"
  PLIST=$(mktemp "$AGENT_DIR/.$LABEL.XXXXXX")
  trap 'rm -f "$PLIST"' EXIT
  # plutil escapes paths correctly, including spaces and XML special characters.
  plutil -create xml1 "$PLIST"
  plutil -insert Label -string "$LABEL" "$PLIST"
  plutil -insert ProgramArguments -json '[]' "$PLIST"
  plutil -insert ProgramArguments.0 -string "$APP_DIR/opencode-traffic-light" "$PLIST"
  plutil -insert ProgramArguments.1 -string --exit-on-disconnect "$PLIST"
  # SuccessfulExit-based KeepAlive implies RunAtLoad, so leave it off entirely.
  plutil -insert RunAtLoad -bool false "$PLIST"
  plutil -insert LimitLoadToSessionType -string Aqua "$PLIST"
  plutil -insert StandardOutPath -string "$LOG_DIR/stdout.log" "$PLIST"
  plutil -insert StandardErrorPath -string "$LOG_DIR/stderr.log" "$PLIST"
  plutil -lint "$PLIST" >/dev/null
  mv -f "$PLIST" "$AGENT_DIR/$LABEL.plist"
  trap - EXIT
  "$SOURCE_DIR/traffic-light" stop
  if [ ! -e "$STATE_DIR/disabled" ]; then
    launchctl enable "gui/$(id -u)/$LABEL"
  fi
else
  SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  mkdir -p "$SERVICE_DIR"
  python3 - "$SOURCE_DIR" "$(command -v python3)" "$SERVICE_DIR/opencode-traffic-light.service" <<'EOF'
from pathlib import Path
import sys

source, python, destination = sys.argv[1:]
def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n') + '"'

unit = (Path(source) / 'opencode-traffic-light.service').read_text()
unit = unit.replace('@PYTHON@', quote(python)).replace('@WIDGET@', quote(str(Path(source) / 'opencode-traffic-light.py')))
Path(destination).write_text(unit)
EOF
  systemctl --user daemon-reload
  # Remove the login-started unit installed by older versions; the plugin starts it.
  systemctl --user disable --now opencode-traffic-light.service
fi

echo ""
echo "Done. Next:"
echo "  1. Restart all OpenCode processes. The plugin will open the widget automatically."
echo "  2. curl http://127.0.0.1:4390/status  ->  {\"state\":\"green\"}"
echo "  3. traffic-light stop|start|restart|status|logs"
if [ -e "$STATE_DIR/disabled" ]; then
  echo "  Automatic startup remains disabled. Run traffic-light enable to restore it."
fi
