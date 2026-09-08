#!/usr/bin/env python3
"""
A tiny always-on-top "notch" widget that shows opencode's current state
(green = finished, yellow = working, red = needs permission).

Requires the opencode plugin (traffic-light.ts) to be running, which
serves state at http://localhost:4390/status

Dependencies (usually already present on Mint's Cinnamon/MATE/XFCE):
    sudo apt install python3-gi gir1.2-gtk-3.0

Run:
    python3 opencode-traffic-light.py

Quit:
    right-click the widget
"""
import json
import math
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk

STATUS_URL = "http://localhost:4390/status"
POLL_MS = 400
H_WIDTH, H_HEIGHT, RADIUS = 150, 30, 14
V_WIDTH, V_HEIGHT = 30, 150  # exact 90° rotation of the horizontal 150x30
EDGE_SNAP = 80  # px from screen edge that flips the notch vertical

COLORS = {
    "green": (0.204, 0.780, 0.349),
    "yellow": (1.0, 0.800, 0.0),
    "red": (1.0, 0.231, 0.188),
}
LABELS = {"green": "finished", "yellow": "working", "red": "permission"}


class Notch(Gtk.Window):
    def __init__(self):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.state = "green"
        self.orient = "top"  # top | left | right
        self._resize_pending = False

        self.set_decorated(False)
        self.set_resizable(False)
        # note: no set_default_size — it doubles as a minimum size and would
        # pin the width at 150 forever. The DrawingArea request sizes us.
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
        area.set_size_request(H_WIDTH, H_HEIGHT)
        area.connect("draw", self.on_draw)
        self.add(area)
        self.area = area

        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.STRUCTURE_MASK)
        self.connect("button-press-event", self.on_click)
        self.connect("configure-event", self.on_configure)
        self.connect("realize", lambda *_: self.center_on_top())

        GLib.timeout_add(POLL_MS, self.poll_status)

    def center_on_top(self):
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        x = geo.x + (geo.width - H_WIDTH) // 2
        y = geo.y  # flush against the top edge, like a real notch
        self.move(x, y)

    def on_configure(self, _widget, _event):
        # flip vertical when parked against a side edge
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        x, _y = self.get_position()
        cx = x + self.get_allocated_width() / 2
        if cx - geo.x < EDGE_SNAP:
            new = "left"
        elif geo.x + geo.width - cx < EDGE_SNAP:
            new = "right"
        else:
            new = "top"
        if new != self.orient:
            self.orient = new
            # resize() inside configure-event is ignored: defer to idle
            if not self._resize_pending:
                self._resize_pending = True
                GLib.idle_add(self.apply_orient_size)
        return False

    def apply_orient_size(self):
        self._resize_pending = False
        if self.orient == "top":
            target = (H_WIDTH, H_HEIGHT)
        else:
            target = (V_WIDTH, V_HEIGHT)
        # requisition applies async: request now, force the size after it lands
        self.area.set_size_request(*target)
        self.queue_resize()
        GLib.timeout_add(50, self.force_size, target)
        return False

    def force_size(self, target):
        self.resize(*target)
        # snap flush to the edge like the top notch: y stays where dropped
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        x, y = self.get_position()
        if self.orient == "left":
            self.move(geo.x, max(geo.y, min(y, geo.y + geo.height - V_HEIGHT)))
        elif self.orient == "right":
            self.move(geo.x + geo.width - V_WIDTH, max(geo.y, min(y, geo.y + geo.height - V_HEIGHT)))
        else:
            self.move(max(geo.x, min(x, geo.x + geo.width - H_WIDTH)), geo.y)
        return False

    def on_click(self, _widget, event):
        if event.button == 3:  # right click = quit
            Gtk.main_quit()
        elif event.button == 1:  # left drag = move
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    def on_draw(self, widget, cr):
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        # lamp order follows the rotation: the red end of the horizontal bar
        # swings up on the right edge, down on the left edge
        order = ("red", "yellow", "green") if self.orient != "left" else ("green", "yellow", "red")

        if self.orient != "top":
            # vertical pill, three lamps top to bottom
            rad = 12
            cr.new_path()
            cr.move_to(rad, 0)
            cr.line_to(w - rad, 0)
            cr.arc(w - rad, rad, rad, -math.pi / 2, 0)
            cr.line_to(w, h - rad)
            cr.arc(w - rad, h - rad, rad, 0, math.pi / 2)
            cr.line_to(rad, h)
            cr.arc(rad, h - rad, rad, math.pi / 2, math.pi)
            cr.line_to(0, rad)
            cr.arc(rad, rad, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(0.06, 0.06, 0.07, 0.95)
            cr.fill()

            dot_r = 8
            ys = (30, h / 2, h - 30)
            for i, name in enumerate(order):
                r, g, b = COLORS[name]
                cx, cy = w / 2, ys[i]
                if name == self.state:
                    cr.set_source_rgba(r, g, b, 0.35)
                    cr.arc(cx, cy, dot_r + 5, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgb(r, g, b)
                else:
                    cr.set_source_rgba(r, g, b, 0.15)
                cr.arc(cx, cy, dot_r, 0, 2 * math.pi)
                cr.fill()
            return False

        # horizontal notch: flat top, rounded bottom corners, three lamps
        cr.new_path()
        cr.move_to(0, 0)
        cr.line_to(w, 0)
        cr.line_to(w, h - RADIUS)
        cr.arc(w - RADIUS, h - RADIUS, RADIUS, 0, math.pi / 2)
        cr.line_to(RADIUS, h)
        cr.arc(RADIUS, h - RADIUS, RADIUS, math.pi / 2, math.pi)
        cr.close_path()
        cr.set_source_rgba(0.06, 0.06, 0.07, 0.95)
        cr.fill()

        dot_r = 8
        xs = (w / 2 - 34, w / 2, w / 2 + 34)
        for i, name in enumerate(order):
            r, g, b = COLORS[name]
            cx, cy = xs[i], h / 2
            if name == self.state:
                cr.set_source_rgba(r, g, b, 0.35)
                cr.arc(cx, cy, dot_r + 5, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(r, g, b)
            else:
                cr.set_source_rgba(r, g, b, 0.15)
            cr.arc(cx, cy, dot_r, 0, 2 * math.pi)
            cr.fill()
        return False

    def poll_status(self):
        try:
            with urllib.request.urlopen(STATUS_URL, timeout=1) as resp:
                data = json.loads(resp.read())
                new_state = data.get("state", "green")
                if new_state != self.state:
                    self.state = new_state
                    self.area.queue_draw()
        except Exception:
            pass  # plugin/server not running yet - just keep polling
        return True


def main():
    win = Notch()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
