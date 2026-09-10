#!/usr/bin/env python3
"""
A tiny always-on-top capsule widget that shows opencode's current state
(green = finished, yellow = working, red = needs permission).

Requires the opencode plugin (traffic-light.js) to be running, which
serves state at http://127.0.0.1:4390/status

Dependencies (usually already present on Mint's Cinnamon/MATE/XFCE):
    sudo apt install python3-gi python3-cairo python3-gi-cairo gir1.2-gtk-3.0

Run:
    python3 opencode-traffic-light.py

Quit:
    hover or right-click, then left-click the close control
    with --exit-on-disconnect, exits after five seconds without a valid status response
    otherwise keeps polling and displaying the last color while disconnected
"""
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

STATUS_URL = "http://127.0.0.1:4390/status"
POLL_MS = 400
DISCONNECT_GRACE_SECONDS = 5
H_WIDTH, H_HEIGHT, RADIUS = 61, 24, 12
V_WIDTH, V_HEIGHT = H_HEIGHT, H_WIDTH
EXPANDED_LENGTH = 84
MIN_SCALE, MAX_SCALE = 0.75, 2.0
MOTION_SECONDS, LAMP_SECONDS = 0.150, 0.120
EDGE_SNAP = 80  # px from screen edge that flips the capsule vertical

COLORS = {
    "green": (0x2D / 255, 0xC8 / 255, 0x4F / 255),
    "yellow": (0xC5 / 255, 0xCD / 255, 0x2D / 255),
    "red": (0xC7 / 255, 0x44 / 255, 0x30 / 255),
}
RING_COLORS = {
    "green": (0x1B / 255, 0x4F / 255, 0x2B / 255),
    "yellow": (0x50 / 255, 0x55 / 255, 0x1B / 255),
    "red": (0x51 / 255, 0x29 / 255, 0x1D / 255),
}
LABELS = {"green": "finished", "yellow": "working", "red": "permission"}


def default_preferences_path():
    root = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state"
    return Path(root) / "opencode-traffic-light/preferences.json"


def validated_scale(value):
    if (not isinstance(value, bool) and isinstance(value, (int, float))
            and MIN_SCALE <= value <= MAX_SCALE and math.isfinite(value)):
        return float(value)
    return 1.0


def load_scale(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return validated_scale(data.get("scale")) if isinstance(data, dict) else 1.0
    except (OSError, ValueError, RecursionError):
        return 1.0


def save_scale(path, scale):
    temporary = None
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".preferences-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"scale": validated_scale(scale)}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class Notch(Gtk.Window):
    def __init__(self, preferences_path=None, clock=None, exit_on_disconnect=False):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self._preferences_path = (Path(preferences_path) if preferences_path is not None
                                  else default_preferences_path())
        self._clock = clock or time.monotonic
        self.scale = load_scale(self._preferences_path)
        self._committed_scale = self.scale
        self.state = "green"
        self.orient = "top"  # top | left | right
        self._presentation = {"scale": self.scale, "reveal": 0.0,
                              "red": 0.15, "yellow": 0.15, "green": 1.0}
        self._animations = {}
        self._tick_id = None
        self._size_anchor = None
        self._resize_gesture = None
        self._size_generation = 0
        self._resize_pending = False
        self._snap_pending = False
        self._dragging = False
        self._drag_position = None
        self._move_position = None
        self._drag_check_source = None
        self.close_visible = False
        self._close_press = None
        self._exit_on_disconnect = exit_on_disconnect
        self._last_valid_response = time.monotonic()

        self.set_decorated(False)
        self.set_resizable(False)
        # The DrawingArea request controls size, including contraction.
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.stick()
        self.set_app_paintable(True)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)

        area = Gtk.DrawingArea()
        area.set_size_request(*self.widget_size())
        area.set_tooltip_text("Resize arc: drag to resize; double-click to reset to 100%")
        area.connect("draw", self.on_draw)
        self.add(area)
        self.area = area

        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
            | Gdk.EventMask.STRUCTURE_MASK | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event", self.on_click)
        self.connect("button-release-event", self.on_release)
        self.connect("motion-notify-event", self.on_motion)
        self.connect("enter-notify-event", self.on_enter)
        self.connect("leave-notify-event", self.on_leave)
        self.connect("configure-event", self.on_configure)
        self.connect("realize", lambda *_: self.center_on_top())
        self.connect("destroy", self.on_destroy)

        GLib.timeout_add(POLL_MS, self.poll_status)

    def center_on_top(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_workarea()
        x = geo.x + (geo.width - self.widget_size()[0]) // 2
        y = geo.y
        self.move(x, y)

    def widget_size(self):
        scale = self._presentation["scale"]
        length = math.ceil((H_WIDTH + (EXPANDED_LENGTH - H_WIDTH)
                            * self._presentation["reveal"]) * scale)
        short = math.ceil(H_HEIGHT * scale)
        return (length, short) if self.orient == "top" else (short, length)

    def workarea(self):
        display = Gdk.Display.get_default()
        x, y = self.get_position()
        scale = self._presentation["scale"]
        w, h = (H_WIDTH, H_HEIGHT) if self.orient == "top" else (V_WIDTH, V_HEIGHT)
        w, h = math.ceil(w * scale), math.ceil(h * scale)
        monitor = display.get_monitor_at_point(x + w // 2, y + h // 2)
        return monitor.get_workarea()

    def animations_enabled(self):
        settings = Gtk.Settings.get_default()
        return settings is not None and bool(settings.get_property("gtk-enable-animations"))

    def capture_anchor(self):
        x, y = self.get_position()
        geo = self.workarea()
        right_docked = (self.orient == "right"
                        and x + self.widget_size()[0] >= geo.x + geo.width - 1)
        return x, y, geo, right_docked

    def advance_animation(self, now):
        geometry_changed = False
        enabled = self.animations_enabled()
        for name, (start, target, began, duration) in tuple(self._animations.items()):
            t = max(0.0, min(1.0, (now - began) / duration)) if enabled else 1.0
            eased = t * t * (3 - 2 * t)
            value = target if t >= 1 else start + (target - start) * eased
            geometry_changed |= name in ("scale", "reveal") and value != self._presentation[name]
            self._presentation[name] = value
            if t >= 1:
                del self._animations[name]
        return geometry_changed

    def animate(self, targets, duration=MOTION_SECONDS, *, immediate=False, defer_size=False):
        now = self._clock()
        geometry_changed = self.advance_animation(now)
        enabled = self.animations_enabled() and not immediate
        geometry = any(name in targets for name in ("scale", "reveal"))
        if geometry and self._size_anchor is None and (enabled or not defer_size):
            self._size_anchor = self.capture_anchor()
        for name, target in targets.items():
            self._animations.pop(name, None)
            start = self._presentation[name]
            if enabled and start != target:
                self._animations[name] = (start, target, now, duration)
            else:
                self._presentation[name] = target
        if geometry or geometry_changed:
            if defer_size and not enabled and self._size_anchor is None and not geometry_changed:
                self.request_size()
            else:
                self.force_size()
        self.area.queue_draw()
        self.sync_animation_tick()

    def sync_animation_tick(self):
        if self._animations and self._tick_id is None:
            self._tick_id = self.add_tick_callback(self.on_tick)
        elif not self._animations and self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None
        if not self._resize_gesture and not any(k in self._animations for k in ("scale", "reveal")):
            self._size_anchor = None

    def on_tick(self, _widget, _frame_clock):
        if self._animations:
            if self.advance_animation(self._clock()):
                self.force_size()
            self.area.queue_draw()
        if not self._resize_gesture and not any(k in self._animations for k in ("scale", "reveal")):
            self._size_anchor = None
        if not self._animations:
            self._tick_id = None
            return False
        return True

    def on_destroy(self, *_args):
        self._animations.clear()
        self.sync_animation_tick()

    def on_configure(self, _widget, _event):
        position = self.get_position()
        if position == self._move_position:
            self._move_position = None
            self._drag_position = position
            return False
        # Only drag movement can change orientation, never a hover requisition
        # or our own clamp/snap. Use the collapsed frame at the snap threshold.
        if not self._dragging or position == self._drag_position:
            return False
        self._drag_position = position
        geo = self.workarea()
        x, _y = position
        cx = x + (H_WIDTH if self.orient == "top" else V_WIDTH) * self._presentation["scale"] / 2
        if cx - geo.x < EDGE_SNAP:
            new = "left"
        elif geo.x + geo.width - cx < EDGE_SNAP:
            new = "right"
        else:
            new = "top"
        if new != self.orient:
            self.orient = new
            self._size_anchor = None
            self._close_press = None
            self._snap_pending = True
            self.request_size()
        return False

    def request_size(self):
        # resize() inside configure-event is ignored: defer to idle.
        self._size_generation += 1
        if not self._resize_pending:
            self._resize_pending = True
            GLib.idle_add(self.apply_orient_size)

    def apply_orient_size(self):
        self._resize_pending = False
        # requisition applies async: request now, force the size after it lands
        self.area.set_size_request(*self.widget_size())
        self.queue_resize()
        GLib.timeout_add(50, self.force_size, self._size_generation)
        return False

    def force_size(self, generation=None):
        # Requisitions may arrive after a tick or pointer resize. Never replay them.
        if generation is not None and generation != self._size_generation:
            return False
        self._size_generation += 1
        w, h = self.widget_size()
        self.area.set_size_request(w, h)
        self.resize(w, h)
        if self._size_anchor is not None:
            x, y, geo, right_docked = self._size_anchor
            if right_docked:
                x = geo.x + geo.width - w
        else:
            geo = self.workarea()
            x, y = self.get_position()
        if self._snap_pending:
            if self.orient == "left":
                x = geo.x
            elif self.orient == "right":
                x = geo.x + geo.width - w
            else:
                y = geo.y
            self._snap_pending = False
        position = (max(geo.x, min(x, geo.x + geo.width - w)),
                    max(geo.y, min(y, geo.y + geo.height - h)))
        if position != self.get_position():
            self._close_press = None
            # Keep the last observed drag position until move() is acknowledged.
            self._move_position = position
            self.move(*position)
        return False

    def close_rect(self):
        return self.control_rect(61, 19)

    def resize_rect(self):
        return self.control_rect(75, 9)

    def control_rect(self, start, length, scale=None):
        scale = self._presentation["scale"] if scale is None else scale
        rect = (start, 0, length, H_HEIGHT) if self.orient == "top" else (0, start, V_WIDTH, length)
        return tuple(value * scale for value in rect)

    def close_contains(self, event):
        if not self.control_contains(self.close_rect(), event):
            return False
        long, short = self.control_point(event)
        return math.hypot(long - 70.5, short - 12) <= 6

    def resize_contains(self, event):
        if not self.control_contains(self.resize_rect(), event):
            return False
        long, short = self.control_point(event)
        return (long >= 75 and math.hypot(long - 72, short - 12) <= 12
                and math.hypot(long - 70.5, short - 12) >= 8)

    def control_point(self, event):
        scale = self._presentation["scale"]
        x, y = event.x / scale, event.y / scale
        return (x, y) if self.orient == "top" else (y, x)

    def control_contains(self, rect, event):
        x, y, w, h = rect
        return (self.close_visible and self._presentation["reveal"] == 1
                and x <= event.x < x + w and y <= event.y < y + h
                and x + w <= self.area.get_allocated_width()
                and y + h <= self.area.get_allocated_height())

    def set_close_visible(self, visible):
        if not visible and (self._dragging or self._resize_gesture):
            return
        if self.close_visible == visible:
            return
        self.close_visible = visible
        self._close_press = None
        self.animate({"reveal": float(visible)}, immediate=self._dragging,
                     defer_size=not self.animations_enabled())

    def reconcile_hover(self):
        if self._dragging or self._resize_gesture:
            return
        window = self.get_window()
        if window is not None:
            _child, x, y, _mask = window.get_pointer()
            w, h = self.widget_size()
            self.set_close_visible(0 <= x < w and 0 <= y < h)

    def on_enter(self, _widget, _event):
        # Crossing events can be queued by resize or a window-manager grab.
        self.reconcile_hover()
        return False

    def on_leave(self, _widget, _event):
        self.reconcile_hover()
        return False

    def check_drag(self):
        if self._dragging:
            window = self.get_window()
            if window is not None:
                _child, _x, _y, mask = window.get_pointer()
                if mask & Gdk.ModifierType.BUTTON1_MASK:
                    return True
            # The window manager may consume the release without another configure.
            self.on_configure(None, None)
            self._dragging = False
            self.reconcile_hover()
        self._drag_check_source = None
        return False

    def on_click(self, _widget, event):
        self._close_press = None
        if event.button == 3:
            self.set_close_visible(True)
            return True
        if event.button == 1:
            if self.resize_contains(event):
                if event.type == Gdk.EventType._2BUTTON_PRESS:
                    self._resize_gesture = None
                    self.reset_scale()
                else:
                    self._size_anchor = self.capture_anchor()
                    self._resize_gesture = (event.x_root if self.orient == "top" else event.y_root,
                                            self._presentation["scale"])
                    self._animations.pop("scale", None)
                    self.scale = self._presentation["scale"]
                    self.sync_animation_tick()
            elif self._resize_gesture:
                return True
            elif self.close_contains(event):
                self._close_press = self.close_rect()
            elif (self._presentation["reveal"] > 0
                  and (event.x if self.orient == "top" else event.y)
                  >= H_WIDTH * self._presentation["scale"]):
                return True  # Inactive or neutral end-cap space must not start a move.
            else:
                self._dragging = True
                if any(k in self._animations for k in ("scale", "reveal")):
                    self._size_anchor = None
                    self.close_visible |= self._presentation["reveal"] > 0
                    self.animate({"scale": self.scale, "reveal": float(self.close_visible)},
                                 immediate=True)
                self._drag_position = self.get_position()
                self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
                if self._drag_check_source is None:
                    self._drag_check_source = GLib.timeout_add(50, self.check_drag)
            return True
        return False

    def on_motion(self, _widget, event):
        if self._resize_gesture is None:
            return False
        origin, scale = self._resize_gesture
        pointer = event.x_root if self.orient == "top" else event.y_root
        self.scale = max(MIN_SCALE, min(MAX_SCALE, scale + (pointer - origin) / EXPANDED_LENGTH))
        self._presentation["scale"] = self.scale
        self.force_size()
        self.area.queue_draw()
        return True

    def commit_scale(self):
        if self.scale != self._committed_scale:
            save_scale(self._preferences_path, self.scale)
            self._committed_scale = self.scale

    def reset_scale(self):
        self._close_press = None
        self.scale = 1.0
        self.animate({"scale": 1.0})
        self.commit_scale()

    def on_release(self, _widget, event):
        if event.button != 1:
            return False
        if self._resize_gesture is not None:
            self.on_motion(None, event)
            self._resize_gesture = None
            self._size_anchor = None
            self.commit_scale()
            self.reconcile_hover()
            return True
        pressed = self._close_press
        self._close_press = None
        if self._dragging:
            self.on_configure(None, None)
            self._dragging = False
            self.reconcile_hover()
        # Resizing or leaving during the gesture must not activate a new target.
        if (pressed == self.close_rect() and self.close_visible
                and self.close_contains(event)):
            Gtk.main_quit()
        return pressed is not None

    def draw_close(self, cr):
        opacity = self._presentation["reveal"]
        if opacity <= 0:
            return
        x, y, w, h = self.control_rect(61, 19, scale=1)
        cx, cy = x + w / 2, y + h / 2
        cr.save()
        cr.set_source_rgba(1, 1, 1, opacity)
        cr.arc(cx, cy, 5.25, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, opacity)
        cr.set_line_width(0.65)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.move_to(cx - 1.8, cy - 1.8)
        cr.line_to(cx + 1.8, cy + 1.8)
        cr.move_to(cx + 1.8, cy - 1.8)
        cr.line_to(cx - 1.8, cy + 1.8)
        cr.stroke()
        cr.set_source_rgba(0.55, 0.55, 0.57, opacity)
        cr.set_line_width(1.5)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        if self.orient == "top":
            cr.arc(73, 12, 9, math.radians(-70), math.radians(70))
        else:
            cr.arc(12, 73, 9, math.radians(20), math.radians(160))
        cr.stroke()
        cr.restore()

    def on_draw(self, _widget, cr):
        length = H_WIDTH + (EXPANDED_LENGTH - H_WIDTH) * self._presentation["reveal"]
        w, h = (length, H_HEIGHT) if self.orient == "top" else (V_WIDTH, length)
        order = ("red", "yellow", "green")
        cr.save()
        # Clear old corners and the close slot before painting the opaque capsule.
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.scale(self._presentation["scale"], self._presentation["scale"])
        cr.new_path()
        cr.move_to(RADIUS, 0)
        cr.line_to(w - RADIUS, 0)
        cr.arc(w - RADIUS, RADIUS, RADIUS, -math.pi / 2, 0)
        cr.line_to(w, h - RADIUS)
        cr.arc(w - RADIUS, h - RADIUS, RADIUS, 0, math.pi / 2)
        cr.line_to(RADIUS, h)
        cr.arc(RADIUS, h - RADIUS, RADIUS, math.pi / 2, math.pi)
        cr.line_to(0, RADIUS)
        cr.arc(RADIUS, RADIUS, RADIUS, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgb(0x11 / 255, 0x11 / 255, 0x13 / 255)
        cr.fill_preserve()
        cr.clip()

        for position, name in zip((12, 30.5, 49), order):
            cx, cy = (position, 12) if self.orient == "top" else (12, position)
            cr.set_source_rgb(*RING_COLORS[name])
            cr.arc(cx, cy, 7.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*COLORS[name], self._presentation[name])
            cr.arc(cx, cy, 5.25, 0, 2 * math.pi)
            cr.fill()
        self.draw_close(cr)
        cr.restore()
        return False

    def poll_status(self):
        try:
            with urllib.request.urlopen(STATUS_URL, timeout=1) as resp:
                data = json.loads(resp.read())
                new_state = data.get("state") if isinstance(data, dict) else None
                if (200 <= resp.status < 300 and isinstance(new_state, str)
                        and new_state in COLORS):
                    self._last_valid_response = time.monotonic()
                    if new_state != self.state:
                        self.state = new_state
                        self.animate({name: 1.0 if name == new_state else 0.15 for name in COLORS},
                                     LAMP_SECONDS)
        except Exception:
            pass  # Keep the last color during backend outages.
        if (self._exit_on_disconnect
                and time.monotonic() - self._last_valid_response >= DISCONNECT_GRACE_SECONDS):
            Gtk.main_quit()
            return False
        return True


def main():
    win = Notch(exit_on_disconnect="--exit-on-disconnect" in sys.argv[1:])
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
