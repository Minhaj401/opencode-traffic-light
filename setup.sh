#!/bin/sh
# Setup for the opencode traffic-light widget.
# Idempotent: safe to re-run. Installs system deps (needs sudo),
# links the `traffic-light` CLI, registers the opencode plugin,
# and enables the autostart service.
set -e
cd "$(dirname "$0")"

echo "== 1. system deps =="
if python3 -c "import gi; gi.require_version('Gtk','3.0'); gi.require_version('Gdk','3.0'); from gi.repository import Gtk, Gdk" 2>/dev/null; then
  echo "  gtk3 introspection: present"
else
  echo "  installing python3-gi gir1.2-gtk-3.0 (sudo)…"
  sudo apt update && sudo apt install -y python3-gi gir1.2-gtk-3.0
fi
command -v wmctrl >/dev/null || echo "  note: wmctrl not found (optional, used only for testing)"

echo "== 2. CLI =="
chmod +x traffic-light opencode-traffic-light.py
mkdir -p ~/.local/bin
ln -sf "$PWD/traffic-light" ~/.local/bin/traffic-light
echo "  linked ~/.local/bin/traffic-light (ensure ~/.local/bin is on PATH)"

echo "== 3. opencode plugin =="
mkdir -p ~/.config/opencode/plugins/traffic-light
cp traffic-light.js package.json ~/.config/opencode/plugins/traffic-light/
# opencode.jsonc overrides opencode.json: register there
if grep -q "traffic-light" ~/.config/opencode/opencode.jsonc 2>/dev/null; then
  echo "  already registered in opencode.jsonc"
else
  python3 - "$HOME/.config/opencode/opencode.jsonc" <<'EOF'
import json, re, sys
p = sys.argv[1]
raw = open(p).read()
plug_re = re.compile(r'"plugin"\s*:\s*\[(.*?)\]', re.S)
entry = '"./plugins/traffic-light/traffic-light.js"'
if '"plugin"' not in raw:
    raw = raw.rstrip()
    assert raw.endswith('}'), "unexpected jsonc shape"
    raw = raw[: -1].rstrip() + ',\n  "plugin": [%s]\n}\n' % entry
elif 'traffic-light' not in raw:
    raw = plug_re.sub(lambda m: '"plugin": [%s, %s]' % (m.group(1).strip(), entry)
                      if m.group(1).strip() else '"plugin": [%s]' % entry, raw, count=1)
open(p, 'w').write(raw)
EOF
  echo "  registered in opencode.jsonc"
fi
timeout 30 opencode debug config 2>/dev/null | python3 -c "
import json, sys
plugs = json.load(sys.stdin).get('plugin') or []
print('  effective plugins:', 'OK (traffic-light present)' if any('traffic-light' in p for p in plugs) else 'MISSING — check config precedence')"

echo "== 4. autostart service =="
mkdir -p ~/.config/systemd/user
sed "s|/home/minhaj|$HOME|" opencode-traffic-light.service > ~/.config/systemd/user/opencode-traffic-light.service
systemctl --user daemon-reload
systemctl --user enable --now opencode-traffic-light.service
sleep 3
echo "  service: $(systemctl --user is-active opencode-traffic-light.service)"

echo ""
echo "Done. Next:"
echo "  1. (Re)start your opencode TUI so it loads the plugin."
echo "  2. curl http://127.0.0.1:4390/status  ->  {\"state\":\"green\"}"
echo "  3. traffic-light stop|start|restart|status|logs"
