# Native macOS widget

A standalone Swift/AppKit frontend for the existing localhost status endpoint.
It requires no GTK or Python runtime and makes no transport protocol changes.

## Build and run

Requirements: macOS 12+, a desktop login, and recent Apple Command Line Tools.
Swift 6 is recommended.

```sh
xcode-select --install  # only if the compiler tools are missing
MACOSX_DEPLOYMENT_TARGET=12.0 xcrun swiftc -O -parse-as-library \
  opencode-traffic-light.swift -o /tmp/traffic-light-widget
/tmp/traffic-light-widget
```

The floating panel does not take focus or add a Dock icon. It polls
`http://127.0.0.1:4390/status` for `{"state":"green"}`, `{"state":"yellow"}`, or
`{"state":"red"}`. Standalone mode keeps polling and retains the last state during
an outage; it does not exit solely because the endpoint is unavailable.

Use the optional flag when a launcher should tie the widget to backend lifetime:

```sh
/tmp/traffic-light-widget --exit-on-disconnect
```

That mode exits after approximately five seconds without a valid response.

## Controls

- Drag the lamps to move; side edges switch to a vertical layout.
- Hover/right-click to reveal a white Close button and a grey resize arc.
- Drag the arc to resize proportionally from 75% to 200%; double-click to reset.
- Click Close to quit. Its hit area is separated from Resize by a neutral gap.

The default capsule is 61 by 24 points, expanding to 84 by 24. Lamp order stays
red/yellow/green, with dim inactive cores. Size is saved atomically in
`${XDG_STATE_HOME:-$HOME/.local/state}/opencode-traffic-light/preferences.json`.
Subtle hover/reset and brightness transitions respect Reduce Motion. Direct
moving/resizing stays immediate, and controls expose accessibility actions.

## Tests

```sh
(
  set -eu
  BUILD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/traffic-light.XXXXXX")
  trap 'rm -rf "$BUILD_DIR"' EXIT
  xcrun swiftc -parse-as-library -swift-version 6 -strict-concurrency=complete \
    -warnings-as-errors -D WIDGET_TESTING opencode-traffic-light.swift \
    tests/macos-widget.swift -o "$BUILD_DIR/widget-tests"
  "$BUILD_DIR/widget-tests"
)
```

Append `--gui-smoke` to the test invocation for temporary-panel interaction tests.
Use `--render-previews "$BUILD_DIR"` instead to export PNGs without opening a window.
Tests use isolated clocks, networking, and preference paths.

Apple Silicon execution and native GUI checks have been tested. Intel/macOS 12
compatibility is compile-checked; physical input, VoiceOver, and fullscreen/Spaces
behavior still need platform-specific verification.
