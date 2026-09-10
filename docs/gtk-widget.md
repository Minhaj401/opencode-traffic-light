# Linux widget controls

The GTK3/Cairo frontend consumes the existing
`http://127.0.0.1:4390/status` response. No new transport protocol is required.

![Compact horizontal and vertical layouts](compact-widget-preview.png)

## Controls

- Drag the lamp area to move the widget. Side-edge placement switches between
  horizontal and vertical layouts.
- Hover or right-click to reveal Close and the grey resize arc.
- Drag the arc along the widget's length to resize from 75% to 200%.
- Double-click the arc to reset to 100%. Size is saved for the next launch.
- Click the white circle to close. A neutral gap separates Close and Resize;
  dragging between the targets does not accidentally activate the other control.

At 100%, collapsed size is 61 by 24 points and expanded size is 84 by 24 points.
Vertical layouts transpose those dimensions. Lamps always stay red/yellow/green,
and inactive cores use 15% opacity. Hover/reset transitions take approximately
150 ms and brightness fades take 120 ms. GTK's animation setting is respected.

## Run independently

Install Python 3.8+, GTK3, PyGObject, PyCairo, and the GI/Cairo bridge, then run:

```sh
python3 opencode-traffic-light.py
```

Standalone mode stays open and retains the last color through a backend outage.
It continues polling and recovers when the existing status endpoint returns.

Integrations can explicitly request shutdown after a five-second outage:

```sh
python3 opencode-traffic-light.py --exit-on-disconnect
```

Size is saved in
`${XDG_STATE_HOME:-$HOME/.local/state}/opencode-traffic-light/preferences.json`.
Missing or invalid preferences use 100%; failed saves do not stop the widget.
Service startup/restart policy is controlled by the installed service definition.

## Tests

```sh
python3 -B -m unittest discover -s tests -p test_widget.py -v
```

Tests use GTK/Cairo doubles and temporary preference paths. They cover drawing,
hit targets, move/resize separation, saved scale, transitions, and both outage
policies. Actual desktop rendering and window-manager interactions need a Linux
desktop check.
