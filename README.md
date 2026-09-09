# opencode traffic light

A tiny desktop widget that shows what your opencode agent is doing:

- 🟢 **green** — finished / idle
- 🟡 **yellow** — working
- 🔴 **red** — waiting on you (permission prompt or question)

Only the live lamp glows. Drag it anywhere: parked near the left/right
screen edge it rotates vertical (30×150) and welds flush to the edge;
drag it back and it goes horizontal again (150×30). Right-click quits.

demo.mp4

## How it works

Two parts, one channel:

```
opencode TUI ──events──▶ traffic-light.js (plugin, :4390) ──HTTP──▶ widget
```

1. **`traffic-light.js`** — an opencode plugin. It listens to opencode
   events (`tool.execute.before`, `permission.asked/replied`,
   `session.idle`, `message.updated`, `session.status`) and aggregates
   every session into one state served at
   `http://127.0.0.1:4390/status` → `{"state": "green"|"yellow"|"red"}`.
   Priority: any red wins, else any yellow, else green. The first
   opencode process owns the port; later ones forward events to it via
   `POST /event`, so multiple TUIs drive one light.
2. **`opencode-traffic-light.py`** — a GTK3/Cairo widget (stdlib +
   `python3-gi` only). Polls the endpoint every 400ms, redraws on change.

The `question` tool counts as input: asked → red, answered → yellow.

## Setup (fresh machine)

```sh
git clone <this-repo> ~/projects/opencode-traffic-light
cd ~/projects/opencode-traffic-light
./setup.sh
```

`setup.sh` is idempotent (safe to re-run). It:

1. Installs `python3-gi` + `gir1.2-gtk-3.0` via apt if missing (asks sudo).
2. Links the `traffic-light` CLI into `~/.local/bin` (must be on `PATH`).
3. Copies the plugin into `~/.config/opencode/plugins/traffic-light/`
   and registers it in `opencode.jsonc` — **not** `opencode.json`,
   because `opencode.jsonc` silently overrides it (see gotchas).
   Verifies with `opencode debug config`.
4. Installs + enables the systemd user service (autostart every login,
   restarts on crash).

Then:

```sh
# restart the TUI so it loads the plugin (plugins load once, at startup)
opencode            # quit + reopen any existing TUI

# verify the state server is up
curl http://127.0.0.1:4390/status   # {"state":"green"}
```

## Daily use

```sh
traffic-light start | stop | restart | status | logs
traffic-light disable   # stop + no autostart
traffic-light enable    # autostart + start now
```

- Left-drag the notch to move it, right-click to quit.
- Hover near a side edge and it rotates + snaps flush to the edge.

## Files

| File | What |
|---|---|
| `opencode-traffic-light.py` | the widget |
| `traffic-light.js` / `package.json` | the opencode plugin (source of truth; `setup.sh` copies it into `~/.config/opencode/…`) |
| `traffic-light` | CLI switch for the service |
| `opencode-traffic-light.service` | systemd user unit (source; `$HOME` substituted on install) |
| `setup.sh` | full installer |

## Gotchas (all hit during development)

1. **`opencode.jsonc` beats `opencode.json`** — a `plugin` array in the
   `.jsonc` *replaces* the `.json` one. Register the plugin where it
   takes effect and confirm with `opencode debug config`.
2. **Plugins load at TUI startup only** — `serve`/`web` modes never init
   them. After any plugin change: restart the TUI.
3. **A TUI started before the plugin existed serves nothing** — symptom:
   `curl :4390` refused, light stuck green. Restart the TUI.
4. **`set_default_size` is a minimum** — GTK3 won't shrink a window below
   it; size the window via the child requisition + explicit `resize()`
   instead (and never `resize()` from inside `configure-event` — defer
   to idle, then force after the requisition lands).
5. **No error lamp** — `session.error` maps to green (stopped, not
   waiting). A crash mid-approval can leave a stale red until the next
   event; restarting the TUI clears it.
6. **Widget alone shows green** — state lives inside the TUI process.
   No TUI, no signal.
7. **localhost HTTP, no auth** — any local process can read/spoof the
   light. Fine on a personal box; don't expose the port.

## Requirements

- Linux with GTK3 + Ayatana/GNOME introspection
  (`python3-gi`, `gir1.2-gtk-3.0` — preinstalled on Mint)
- Python 3 (stdlib only: `urllib`, `json`)
- opencode ≥ 1.15 (plugin `event` + `tool.execute.before/after` hooks)
- Optional: `wmctrl` (only used for testing position changes)
