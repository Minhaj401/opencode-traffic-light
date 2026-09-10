"""Exercise the Linux widget without GTK, a window, or network access."""
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]


class WindowDouble:
    def __init__(self, **_kwargs):
        for name in (
            "set_decorated", "set_resizable", "set_type_hint", "set_keep_above",
            "set_skip_taskbar_hint", "set_skip_pager_hint", "stick",
            "set_app_paintable", "set_visual", "add", "add_events", "connect",
            "begin_move_drag", "move", "resize", "queue_resize", "show_all",
            "add_tick_callback", "remove_tick_callback",
        ):
            setattr(self, name, mock.Mock())
        self.get_screen = mock.Mock(return_value=SimpleNamespace(
            get_rgba_visual=mock.Mock(return_value=None),
        ))
        self.get_position = mock.Mock(return_value=(800, 50))
        self.get_allocated_width = mock.Mock(return_value=61)
        self.get_allocated_height = mock.Mock(return_value=24)
        self.pointer = mock.Mock(return_value=(None, 12, 12, 0))
        self.get_window = mock.Mock(return_value=SimpleNamespace(get_pointer=self.pointer))
        self.move.side_effect = lambda x, y: setattr(self.get_position, "return_value", (x, y))


class WidgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.preferences = Path(temporary.name) / "state/preferences.json"
        self.area = mock.Mock(spec=[
            "set_size_request", "set_tooltip_text", "connect", "queue_draw",
            "get_allocated_width", "get_allocated_height",
        ])
        self.settings = SimpleNamespace(get_property=mock.Mock(return_value=False))
        self.gtk = SimpleNamespace(
            Window=WindowDouble,
            WindowType=SimpleNamespace(TOPLEVEL=0),
            DrawingArea=mock.Mock(return_value=self.area),
            Settings=SimpleNamespace(get_default=mock.Mock(return_value=self.settings)),
            main_quit=mock.Mock(),
            main=mock.Mock(),
        )
        self.monitor = SimpleNamespace(get_workarea=mock.Mock(return_value=SimpleNamespace(
            x=100, y=50, width=1600, height=900,
        )))
        self.display = SimpleNamespace(
            get_primary_monitor=mock.Mock(return_value=self.monitor),
            get_monitor=mock.Mock(return_value=self.monitor),
            get_monitor_at_point=mock.Mock(return_value=self.monitor),
        )
        self.gdk = SimpleNamespace(
            WindowTypeHint=SimpleNamespace(UTILITY=0),
            EventMask=SimpleNamespace(
                BUTTON_PRESS_MASK=1, BUTTON_RELEASE_MASK=2, ENTER_NOTIFY_MASK=4,
                LEAVE_NOTIFY_MASK=8, STRUCTURE_MASK=16, POINTER_MOTION_MASK=32,
            ),
            EventType=SimpleNamespace(BUTTON_PRESS=4, _2BUTTON_PRESS=5),
            ModifierType=SimpleNamespace(BUTTON1_MASK=256),
            Display=SimpleNamespace(get_default=mock.Mock(return_value=self.display)),
        )
        self.idles = []
        self.timeouts = []
        self.glib = SimpleNamespace(
            timeout_add=mock.Mock(side_effect=self.add_timeout),
            idle_add=mock.Mock(side_effect=lambda callback: self.idles.append(callback)),
        )
        self.cairo = SimpleNamespace(OPERATOR_SOURCE=1, OPERATOR_OVER=2, LINE_CAP_BUTT=0)
        gi = ModuleType("gi")
        gi.require_version = mock.Mock()
        repository = ModuleType("gi.repository")
        repository.Gtk, repository.Gdk, repository.GLib = self.gtk, self.gdk, self.glib
        gi.repository = repository
        spec = importlib.util.spec_from_file_location("widget_under_test", ROOT / "opencode-traffic-light.py")
        self.widget = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {
            "gi": gi, "gi.repository": repository, "cairo": self.cairo,
        }):
            spec.loader.exec_module(self.widget)
        clock_patch = mock.patch.object(self.widget.time, "monotonic", return_value=100.0)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        network_patch = mock.patch.object(
            self.widget.urllib.request, "urlopen", side_effect=URLError("backend absent"),
        )
        self.urlopen = network_patch.start()
        self.addCleanup(network_patch.stop)
        # Any uninjected construction also stays inside this test's temporary state.
        self.resolve_preferences_path = self.widget.default_preferences_path
        path_patch = mock.patch.object(self.widget, "default_preferences_path", return_value=self.preferences)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        self.win = self.widget.Notch(preferences_path=self.preferences, clock=self.clock,
                                     exit_on_disconnect=True)
        self.win.resize.side_effect = self.allocate
        self.ticks = {}
        self.win.add_tick_callback.side_effect = self.add_tick
        self.win.remove_tick_callback.side_effect = self.ticks.pop
        self.set_orientation("top")

    def add_tick(self, callback):
        identifier = self.win.add_tick_callback.call_count
        self.ticks[identifier] = callback
        return identifier

    def enable_motion(self):
        self.settings.get_property.return_value = True
        self.animation_clock = mock.Mock(return_value=0.0)
        self.win._clock = self.animation_clock

    def frame(self, now):
        self.animation_clock.return_value = now
        for identifier, callback in tuple(self.ticks.items()):
            if not callback(self.win, None):
                self.ticks.pop(identifier)
        self.assertLessEqual(len(self.ticks), 1)

    def set_scale(self, scale):
        self.win.scale = scale
        self.win._presentation["scale"] = scale
        self.allocate(*self.win.widget_size())

    def resize_event(self, delta=0, cross=0, double=False, button=1):
        top = self.win.orient == "top"
        return self.cap_event(82, 12, button=button,
                              x_root=100 + (delta if top else cross),
                              y_root=200 + (cross if top else delta), double=double)

    def cap_event(self, long, short, **kwargs):
        scale = self.win._presentation["scale"]
        x, y = (long, short) if self.win.orient == "top" else (short, long)
        return self.event(x=x * scale, y=y * scale, **kwargs)

    def add_timeout(self, delay, callback, *args):
        self.timeouts.append((delay, callback, args))
        return len(self.timeouts)

    def run_idles(self):
        while self.idles:
            self.assertFalse(self.idles.pop(0)())

    def flush_resizes(self, reverse=False):
        self.run_idles()
        callbacks = [entry for entry in self.timeouts if entry[1] == self.win.force_size]
        self.timeouts = [entry for entry in self.timeouts if entry not in callbacks]
        for _delay, callback, args in reversed(callbacks) if reverse else callbacks:
            self.assertFalse(callback(*args))

    def allocate(self, width, height):
        self.area.get_allocated_width.return_value = width
        self.area.get_allocated_height.return_value = height
        self.win.get_allocated_width.return_value = width
        self.win.get_allocated_height.return_value = height

    def set_orientation(self, orient, expanded=False):
        self.win.orient = orient
        self.win.close_visible = expanded
        self.win._presentation["reveal"] = float(expanded)
        self.allocate(*self.win.widget_size())

    def event(self, button=1, x=12, y=12, x_root=123.9, y_root=456.9, double=False):
        return SimpleNamespace(button=button, x=x, y=y, x_root=x_root, y_root=y_root,
                               time=42, type=5 if double else 4)

    def enter(self):
        self.win.pointer.return_value = (None, 12, 12, 0)
        return self.win.on_enter(None, self.event())

    def leave(self):
        self.win.pointer.return_value = (None, -1, -1, 0)
        return self.win.on_leave(None, self.event(x=-1, y=-1))

    def close_event(self, button=1):
        x, y, width, height = self.win.close_rect()
        return self.event(button, x + width / 2, y + height / 2)

    def respond(self, body=b'{"state":"green"}', status=200):
        response = mock.MagicMock()
        response.status = status
        response.read.return_value = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        self.urlopen.side_effect = None
        self.urlopen.return_value = response
        return response

    def test_initialization_registers_events_and_polling(self):
        self.assertFalse(self.win.close_visible)
        self.assertEqual(self.win._last_valid_response, 100)
        self.area.set_size_request.assert_called_once_with(61, 24)
        self.area.connect.assert_called_once_with("draw", self.win.on_draw)
        self.win.add_events.assert_called_once_with(63)
        self.win.connect.assert_has_calls([
            mock.call("button-press-event", self.win.on_click),
            mock.call("button-release-event", self.win.on_release),
            mock.call("motion-notify-event", self.win.on_motion),
            mock.call("enter-notify-event", self.win.on_enter),
            mock.call("leave-notify-event", self.win.on_leave),
            mock.call("configure-event", self.win.on_configure),
        ])
        self.glib.timeout_add.assert_called_once_with(400, self.win.poll_status)
        self.urlopen.assert_not_called()
        self.gtk.main.assert_not_called()

    def test_hover_and_right_click_expand_and_leave_contracts(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                expanded = (84, 24) if orient == "top" else (24, 84)
                collapsed = (61, 24) if orient == "top" else (24, 61)
                self.enter()
                self.assertTrue(self.win.close_visible)
                self.flush_resizes()
                self.area.set_size_request.assert_called_with(*expanded)
                self.win.resize.assert_called_with(*expanded)
                self.leave()
                self.assertFalse(self.win.close_visible)
                self.flush_resizes()
                self.area.set_size_request.assert_called_with(*collapsed)
                self.win.resize.assert_called_with(*collapsed)
                for event in (self.event(3), self.close_event(3)):
                    self.assertTrue(self.win.on_click(None, event))
                    self.assertTrue(self.win.close_visible)
                    self.flush_resizes()
                    self.win.resize.assert_called_with(*expanded)
                    self.assertFalse(self.win.on_release(None, event))
                    self.leave()
                    self.assertFalse(self.win.close_visible)
                    self.flush_resizes()
                    self.win.resize.assert_called_with(*collapsed)
        self.assertGreater(self.area.queue_draw.call_count, 0)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_close_geometry_is_inside_trailing_end_and_clear_of_lamps(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient, expanded=True)
                width, height = (84, 24) if orient == "top" else (24, 84)
                x, y, w, h = self.win.close_rect()
                self.assertEqual((x, y, w, h), (61, 0, 19, 24) if orient == "top" else (0, 61, 24, 19))
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + w, width)
                self.assertLessEqual(y + h, height)
                if orient == "top":
                    self.assertEqual(x + w, width - 4)
                    centers = ((12, 12), (30.5, 12), (49, 12))
                else:
                    self.assertEqual(y + h, height - 4)
                    centers = ((12, 12), (12, 30.5), (12, 49))
                for cx, cy in centers:
                    dx = max(x - cx, 0, cx - (x + w))
                    dy = max(y - cy, 0, cy - (y + h))
                    self.assertGreater(math.hypot(dx, dy), 7.5)

    def test_close_hit_test_matches_circle_boundaries(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient, expanded=True)
                for long, short in ((70.5, 12), (64.5, 12), (76.5, 12), (70.5, 6), (70.5, 18)):
                    self.assertTrue(self.win.close_contains(self.cap_event(long, short)))
                for long, short in ((64.49, 12), (76.51, 12), (70.5, 5.99), (70.5, 18.01),
                                    (61, 0), (79, 23), (76, 17.5)):
                    self.assertFalse(self.win.close_contains(self.cap_event(long, short)))

    def test_scaled_grey_arc_endpoints_and_padded_cap_exclude_close(self):
        for scale in (0.75, 1, 1.375, 2):
            for orient in ("top", "left", "right"):
                with self.subTest(scale=scale, orient=orient):
                    self.set_scale(scale)
                    self.set_orientation(orient, expanded=True)
                    for long, short in ((82, 12), (76.0781812899, 3.5427664129),
                                        (76.0781812899, 20.4572335871),
                                        (75, 2), (75, 22), (78.5, 12), (83, 12)):
                        event = self.cap_event(long, short)
                        self.assertTrue(self.win.resize_contains(event), (long, short))
                        self.assertFalse(self.win.close_contains(event), (long, short))
                    for long, short in ((74.99, 2), (83, 2), (77.5, 12), (84, 12)):
                        self.assertFalse(self.win.resize_contains(self.cap_event(long, short)))
                    x, y, w, h = self.win.resize_rect()
                    bounds_center = self.event(x=x + w / 2, y=y + h / 2)
                    self.assertFalse(self.win.close_contains(bounds_center))
                    self.assertTrue(self.win.resize_contains(bounds_center))

                    cr = mock.Mock()
                    self.win.on_draw(self.area, cr)
                    cr.scale.assert_called_once_with(scale, scale)
                    cx, cy, radius, start, end = cr.arc.call_args_list[-1].args
                    cr.assert_has_calls([
                        mock.call.set_source_rgba(0.55, 0.55, 0.57, 1),
                        mock.call.set_line_width(1.5), mock.call.set_line_cap(0),
                        mock.call.arc(cx, cy, radius, start, end), mock.call.stroke(),
                    ])
                    white_center = (70.5, 12) if orient == "top" else (12, 70.5)
                    cr.assert_has_calls([
                        mock.call.set_source_rgba(1, 1, 1, 1),
                        mock.call.arc(*white_center, 5.25, 0, 2 * math.pi), mock.call.fill(),
                    ])
                    self.assertEqual(radius, 9)
                    self.assertAlmostEqual(math.degrees(end - start), 140)
                    self.assertAlmostEqual(math.degrees(start), -70 if orient == "top" else 20)
                    self.assertAlmostEqual(math.degrees(end), 70 if orient == "top" else 160)
                    arc_center = cx if orient == "top" else cy
                    self.assertAlmostEqual((arc_center + radius - 1.5 / 2 - (70.5 + 5.25)) * scale,
                                           5.5 * scale)
                    points = ((76.0781812899, 3.5427664129), (82, 12),
                              (76.0781812899, 20.4572335871)) if orient == "top" else (
                        (20.4572335871, 76.0781812899), (12, 82), (3.5427664129, 76.0781812899),
                    )
                    for angle, (px, py) in zip((start, (start + end) / 2, end), points):
                        self.assertAlmostEqual((cx + radius * math.cos(angle)) * scale, px * scale)
                        self.assertAlmostEqual((cy + radius * math.sin(angle)) * scale, py * scale)

    def test_explicit_left_press_and_release_closes_in_all_orientations(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                self.gtk.main_quit.reset_mock()
                self.enter()
                self.flush_resizes()
                event = self.close_event()
                self.assertTrue(self.win.on_click(None, event))
                self.gtk.main_quit.assert_not_called()
                self.assertTrue(self.win.on_release(None, event))
                self.gtk.main_quit.assert_called_once_with()
                self.assertFalse(self.win.on_release(None, event))
                self.gtk.main_quit.assert_called_once_with()
        self.win.begin_move_drag.assert_not_called()

    def test_hidden_control_does_not_close_or_block_drag(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                event = self.close_event()
                self.win.on_click(None, event)
                self.win.begin_move_drag.assert_called_with(1, 123, 456, 42)
                self.win.pointer.return_value = (None, event.x, event.y, 0)
                self.assertFalse(self.win.on_release(None, event))
        self.gtk.main_quit.assert_not_called()

    def test_drag_elsewhere_is_preserved_even_when_released_on_close(self):
        for orient in ("top", "left", "right"):
            for visible in (False, True):
                with self.subTest(orient=orient, visible=visible):
                    self.set_orientation(orient, expanded=visible)
                    self.win.begin_move_drag.reset_mock()
                    x, y = (30.5, 12) if orient == "top" else (12, 30.5)
                    self.assertTrue(self.win.on_click(None, self.event(x=x, y=y)))
                    self.win.begin_move_drag.assert_called_once_with(1, 123, 456, 42)
                    self.win.on_release(None, self.close_event())
        self.gtk.main_quit.assert_not_called()

    def test_release_without_press_never_closes(self):
        self.enter()
        self.flush_resizes()
        for button in (1, 2, 3):
            self.assertFalse(self.win.on_release(None, self.close_event(button)))
        self.gtk.main_quit.assert_not_called()

    def test_release_outside_cancels_close(self):
        self.enter()
        self.flush_resizes()
        self.win.on_click(None, self.close_event())
        self.assertTrue(self.win.on_release(None, self.event()))
        self.assertFalse(self.win.on_release(None, self.close_event()))
        self.gtk.main_quit.assert_not_called()
        self.win.begin_move_drag.assert_not_called()

    def test_close_press_release_on_arc_cancels_without_starting_resize(self):
        for scale in (0.75, 1, 1.375, 2):
            for orient in ("top", "left", "right"):
                with self.subTest(scale=scale, orient=orient):
                    self.set_scale(scale)
                    self.set_orientation(orient, expanded=True)
                    for long, short in ((82, 12), (76.0781812899, 3.5427664129),
                                        (76.0781812899, 20.4572335871)):
                        release = self.cap_event(long, short)
                        self.assertTrue(self.win.resize_contains(release))
                        self.win.on_click(None, self.close_event())
                        self.assertIsNotNone(self.win._close_press)
                        self.assertFalse(self.win.on_motion(None, release))
                        self.assertTrue(self.win.on_release(None, release))
                        self.assertIsNone(self.win._close_press)
                        self.assertIsNone(self.win._resize_gesture)
                        self.assertEqual(self.win.scale, scale)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_neutral_cap_space_never_starts_move_close_resize_or_reset(self):
        for scale in (0.75, 1, 1.375, 2):
            for orient in ("top", "left", "right"):
                with self.subTest(scale=scale, orient=orient):
                    self.set_scale(scale)
                    self.set_orientation(orient, expanded=True)
                    for long, short in ((61, 12), (64, 12), (65, 2), (68.49, 2), (79, 2)):
                        for double in (False, True):
                            event = self.cap_event(long, short, double=double)
                            self.assertFalse(self.win.close_contains(event))
                            self.assertFalse(self.win.resize_contains(event))
                            self.assertTrue(self.win.on_click(None, event))
                            self.assertFalse(self.win.on_motion(None, event))
                            self.assertFalse(self.win.on_release(None, event))
                            self.assertIsNone(self.win._close_press)
                            self.assertIsNone(self.win._resize_gesture)
                            self.assertFalse(self.win._dragging)
                            self.assertEqual(self.win.scale, scale)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_two_point_close_resize_gap_stays_inactive_at_every_scale_and_orientation(self):
        for scale in (0.75, 1, 2):
            for orient in ("top", "left", "right"):
                with self.subTest(scale=scale, orient=orient):
                    self.set_scale(scale)
                    self.set_orientation(orient, expanded=True)
                    self.assertTrue(self.win.close_contains(self.cap_event(76.5, 12)))
                    self.assertFalse(self.win.resize_contains(self.cap_event(76.5, 12)))
                    self.assertFalse(self.win.close_contains(self.cap_event(78.5, 12)))
                    self.assertTrue(self.win.resize_contains(self.cap_event(78.5, 12)))
                    for long, short in ((76.5001, 12), (77, 12), (78, 12), (78.4999, 12),
                                        (75, 6), (75, 18)):
                        for double in (False, True):
                            event = self.cap_event(long, short, double=double)
                            self.assertFalse(self.win.close_contains(event))
                            self.assertFalse(self.win.resize_contains(event))
                            self.assertTrue(self.win.on_click(None, event))
                            self.assertFalse(self.win.on_motion(None, event))
                            self.assertFalse(self.win.on_release(None, event))
                            self.assertIsNone(self.win._close_press)
                            self.assertIsNone(self.win._resize_gesture)
                            self.assertFalse(self.win._dragging)
                            self.assertEqual(self.win.scale, scale)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_leaving_cancels_close_even_after_reentry(self):
        self.enter()
        self.flush_resizes()
        self.win.on_click(None, self.close_event())
        self.leave()
        self.enter()
        self.assertFalse(self.win.on_release(None, self.close_event()))
        self.gtk.main_quit.assert_not_called()

    def test_other_buttons_never_close_and_cancel_pending_left_press(self):
        self.enter()
        self.flush_resizes()
        for button in (2, 3):
            with self.subTest(button=button):
                self.win.on_click(None, self.close_event())
                self.win.on_click(None, self.close_event(button))
                self.assertFalse(self.win.on_release(None, self.close_event(button)))
                self.assertFalse(self.win.on_release(None, self.close_event()))
        self.gtk.main_quit.assert_not_called()
        self.win.begin_move_drag.assert_not_called()

    def test_orientation_change_during_press_does_not_close_new_target(self):
        self.enter()
        self.flush_resizes()
        self.win.on_click(None, self.close_event())
        self.set_orientation("left", expanded=True)
        self.win.on_release(None, self.close_event())
        self.gtk.main_quit.assert_not_called()

    def test_rendering_matches_shared_capsule_palette_lamps_and_close(self):
        cairo_methods = [
            "new_path", "move_to", "line_to", "arc", "close_path", "fill",
            "set_source_rgb", "set_source_rgba", "save", "restore", "rectangle",
            "set_line_width", "set_line_cap", "stroke", "set_operator", "paint",
            "scale", "fill_preserve", "clip",
        ]
        cores = {
            "red": (199 / 255, 68 / 255, 48 / 255),
            "yellow": (197 / 255, 205 / 255, 45 / 255),
            "green": (45 / 255, 200 / 255, 79 / 255),
        }
        rings = {
            "red": (81 / 255, 41 / 255, 29 / 255),
            "yellow": (80 / 255, 85 / 255, 27 / 255),
            "green": (27 / 255, 79 / 255, 43 / 255),
        }
        self.assertEqual(self.widget.COLORS, cores)
        self.assertEqual(self.widget.RING_COLORS, rings)
        for orient in ("top", "left", "right"):
            for state in ("red", "yellow", "green"):
                for expanded in (False, True):
                    with self.subTest(orient=orient, state=state, expanded=expanded):
                        self.set_orientation(orient, expanded)
                        self.win.state = state
                        self.win._presentation.update({name: 1 if name == state else 0.15 for name in cores})
                        cr = mock.Mock(spec=cairo_methods)
                        self.assertFalse(self.win.on_draw(self.area, cr))
                        w, h = ((84 if expanded else 61), 24) if orient == "top" else (
                            24, (84 if expanded else 61),
                        )
                        expected = [
                            mock.call.save(), mock.call.set_operator(1),
                            mock.call.set_source_rgba(0, 0, 0, 0), mock.call.paint(),
                            mock.call.set_operator(2), mock.call.scale(1, 1), mock.call.new_path(),
                            mock.call.move_to(12, 0), mock.call.line_to(w - 12, 0),
                            mock.call.arc(w - 12, 12, 12, -math.pi / 2, 0),
                            mock.call.line_to(w, h - 12),
                            mock.call.arc(w - 12, h - 12, 12, 0, math.pi / 2),
                            mock.call.line_to(12, h),
                            mock.call.arc(12, h - 12, 12, math.pi / 2, math.pi),
                            mock.call.line_to(0, 12),
                            mock.call.arc(12, 12, 12, math.pi, 3 * math.pi / 2),
                            mock.call.close_path(),
                            mock.call.set_source_rgb(17 / 255, 17 / 255, 19 / 255),
                            mock.call.fill_preserve(), mock.call.clip(),
                        ]
                        centers = ((12, 12), (30.5, 12), (49, 12)) if orient == "top" else (
                            (12, 12), (12, 30.5), (12, 49),
                        )
                        order = ("red", "yellow", "green")
                        for name, (cx, cy) in zip(order, centers):
                            expected.extend([
                                mock.call.set_source_rgb(*rings[name]),
                                mock.call.arc(cx, cy, 7.5, 0, 2 * math.pi), mock.call.fill(),
                                mock.call.set_source_rgba(*cores[name], 1 if name == state else 0.15),
                                mock.call.arc(cx, cy, 5.25, 0, 2 * math.pi), mock.call.fill(),
                            ])
                        if expanded:
                            cx, cy = (70.5, 12) if orient == "top" else (12, 70.5)
                            expected.extend([
                                mock.call.save(), mock.call.set_source_rgba(1, 1, 1, 1),
                                mock.call.arc(cx, cy, 5.25, 0, 2 * math.pi), mock.call.fill(),
                                mock.call.set_source_rgba(0, 0, 0, 1), mock.call.set_line_width(0.65),
                                mock.call.set_line_cap(0),
                                mock.call.move_to(cx - 1.8, cy - 1.8),
                                mock.call.line_to(cx + 1.8, cy + 1.8),
                                mock.call.move_to(cx + 1.8, cy - 1.8),
                                mock.call.line_to(cx - 1.8, cy + 1.8),
                                mock.call.stroke(), mock.call.set_source_rgba(0.55, 0.55, 0.57, 1),
                                mock.call.set_line_width(1.5), mock.call.set_line_cap(0),
                            ])
                            expected.append(mock.call.arc(73, 12, 9, math.radians(-70), math.radians(70))
                                            if orient == "top" else
                                            mock.call.arc(12, 73, 9, math.radians(20), math.radians(160)))
                            expected.extend([mock.call.stroke(), mock.call.restore()])
                        expected.append(mock.call.restore())
                        self.assertEqual(cr.mock_calls, expected)
                        cr.rectangle.assert_not_called()

    def test_configure_during_drag_preserves_orientation_sizes_and_edge_snap(self):
        for initial, position, orient, snapped in (
            ("top", (100, 400), "left", (100, 400)),
            ("top", (1680, 1000), "right", (1676, 889)),
            ("left", (800, 200), "top", (800, 50)),
        ):
            with self.subTest(orient=orient):
                self.set_orientation(initial)
                self.win.on_click(None, self.event())
                self.win.get_position.return_value = position
                self.assertFalse(self.win.on_configure(None, None))
                self.assertEqual(self.win.orient, orient)
                self.glib.idle_add.assert_called_with(self.win.apply_orient_size)
                self.run_idles()
                target = (61, 24) if orient == "top" else (24, 61)
                self.area.set_size_request.assert_called_with(*target)
                self.glib.timeout_add.assert_called_with(50, self.win.force_size, self.win._size_generation)
                self.flush_resizes()
                self.win.resize.assert_called_with(*target)
                self.assertEqual(self.win.get_position(), snapped)
                self.win.on_release(None, self.event())

    def test_shared_sizes_in_both_expansion_states_and_orientations(self):
        self.assertEqual((self.widget.H_WIDTH, self.widget.H_HEIGHT, self.widget.RADIUS), (61, 24, 12))
        self.assertEqual((self.widget.V_WIDTH, self.widget.V_HEIGHT), (24, 61))
        self.assertEqual(self.widget.EXPANDED_LENGTH, 84)
        for orient in ("top", "left", "right"):
            for expanded in (False, True):
                with self.subTest(orient=orient, expanded=expanded):
                    self.set_orientation(orient, expanded)
                    expected = (84 if expanded else 61, 24) if orient == "top" else (
                        24, 84 if expanded else 61,
                    )
                    self.assertEqual(self.win.widget_size(), expected)

    def test_initial_position_uses_compact_width_and_primary_workarea(self):
        self.win.center_on_top()
        self.win.move.assert_called_once_with(869, 50)
        self.display.get_primary_monitor.assert_called_once_with()
        self.display.get_monitor.assert_not_called()
        self.monitor.get_workarea.assert_called_once_with()

    def test_initial_position_falls_back_to_first_monitor(self):
        self.display.get_primary_monitor.return_value = None
        self.win.center_on_top()
        self.display.get_monitor.assert_called_once_with(0)
        self.win.move.assert_called_once_with(869, 50)

    def test_hover_preserves_leading_lamp_anchor_and_horizontal_y(self):
        for orient, position in (("top", (800, 350)), ("left", (100, 350)), ("right", (1676, 350))):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                self.win.get_position.return_value = position
                self.win.move.reset_mock()
                self.enter()
                self.flush_resizes()
                self.win.on_configure(None, None)
                self.assertEqual(self.win.orient, orient)
                self.assertEqual(self.win.get_position(), position)
                self.assertEqual(tuple(value + 12 for value in self.win.get_position()),
                                 tuple(value + 12 for value in position))
                self.leave()
                self.flush_resizes()
                self.win.on_configure(None, None)
                self.assertEqual(self.win.orient, orient)
                self.assertEqual(self.win.get_position(), position)
                self.win.move.assert_not_called()

    def test_expansion_clamps_both_axes_without_snapping_or_changing_orientation(self):
        for orient, position, clamped in (
            ("top", (1639, 350), (1616, 350)),
            ("top", (95, 40), (100, 50)),
            ("top", (1650, 940), (1616, 926)),
            ("left", (100, 889), (100, 866)),
            ("right", (1676, 889), (1676, 866)),
            ("left", (95, 40), (100, 50)),
            ("right", (1685, 940), (1676, 866)),
        ):
            with self.subTest(orient=orient, position=position):
                self.set_orientation(orient)
                self.win.get_position.return_value = position
                self.enter()
                self.flush_resizes()
                self.win.on_configure(None, None)
                self.assertEqual(self.win.get_position(), clamped)
                self.assertEqual(self.win.orient, orient)
                self.leave()
                self.flush_resizes()
                self.assertEqual(self.win.get_position(), clamped)
                self.assertEqual(self.win.orient, orient)

    def test_hover_uses_current_monitor_workarea_and_stable_collapsed_center(self):
        secondary = SimpleNamespace(get_workarea=mock.Mock(return_value=SimpleNamespace(
            x=-1600, y=-850, width=1600, height=850,
        )))
        self.display.get_monitor_at_point.return_value = secondary
        self.win.get_position.return_value = (-61, -300)
        self.enter()
        self.flush_resizes()
        self.display.get_monitor_at_point.assert_called_with(-31, -288)
        self.assertEqual(self.win.get_position(), (-84, -300))
        self.monitor.get_workarea.assert_not_called()
        self.assertEqual(self.win.orient, "top")

    def test_hover_configure_cannot_flip_orientation_near_snap_threshold(self):
        for x in (145, 1585, 1639):
            with self.subTest(x=x):
                self.set_orientation("top")
                self.win.get_position.return_value = (x, 400)
                self.enter()
                self.flush_resizes()
                self.win.on_configure(None, None)
                self.assertEqual(self.win.orient, "top")
                self.assertEqual(self.win.get_position()[1], 400)
                self.leave()
                self.flush_resizes()
                self.win.on_configure(None, None)
                self.assertEqual(self.win.orient, "top")

    def test_drag_snap_threshold_uses_collapsed_not_allocated_width(self):
        for expanded in (False, True):
            for x, expected in ((145, "left"), (1585, "top"), (1595, "right")):
                with self.subTest(expanded=expanded, x=x):
                    self.set_orientation("top", expanded)
                    self.win.get_position.return_value = (800, 400)
                    self.win.on_click(None, self.event())
                    self.win.get_position.return_value = (x, 400)
                    self.win.on_configure(None, None)
                    self.assertEqual(self.win.orient, expected)
                    self.flush_resizes()
                    self.win.on_configure(None, None)
                    self.assertEqual(self.win.orient, expected)
                    self.win.on_release(None, self.event())
                    self.flush_resizes()

    def test_resize_only_configure_during_drag_does_not_snap(self):
        self.win.get_position.return_value = (145, 400)
        self.win.on_click(None, self.event())
        self.win.set_close_visible(True)
        self.flush_resizes()
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "top")
        self.assertEqual(self.win.get_position(), (145, 400))
        self.assertTrue(self.win._dragging)

    def test_async_snap_acknowledgment_does_not_reverse_drag_orientation(self):
        self.set_orientation("top", expanded=True)
        self.win.on_click(None, self.event())
        self.win.get_position.return_value = (1595, 400)
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "right")
        self.win.move.side_effect = None
        self.flush_resizes()
        self.win.move.assert_called_with(1676, 400)
        self.win.on_configure(None, None)  # Size arrived, position is still old.
        self.assertEqual(self.win.orient, "right")
        self.win.get_position.return_value = (1676, 400)
        self.win.on_configure(None, None)  # Our own move, not another drag.
        self.assertEqual(self.win.orient, "right")
        self.assertIsNone(self.win._move_position)
        self.win.get_position.return_value = (1500, 400)
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "top")

    def test_async_hover_clamp_during_drag_cannot_trigger_orientation_change(self):
        self.win.get_position.return_value = (1639, 400)
        self.win.on_click(None, self.event())
        self.win.set_close_visible(True)
        self.win.move.side_effect = None
        self.flush_resizes()
        self.win.move.assert_called_with(1616, 400)
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "top")
        self.win.get_position.return_value = (1616, 400)
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "top")
        self.assertEqual(self.win.get_position()[1], 400)

    def test_rapid_hover_idles_and_timeouts_always_use_latest_size(self):
        for orient in ("top", "left", "right"):
            for expanded in (False, True):
                for reverse in (False, True):
                    with self.subTest(orient=orient, expanded=expanded, reverse=reverse):
                        self.set_orientation(orient)
                        self.win.get_position.return_value = (800, 350)
                        self.enter()
                        self.run_idles()
                        self.leave()
                        self.run_idles()
                        self.enter()
                        if not expanded:
                            self.leave()
                        # Execute old force callbacks before the newest idle requisition.
                        callbacks = [entry for entry in self.timeouts if entry[1] == self.win.force_size]
                        expected = (84 if expanded else 61, 24) if orient == "top" else (
                            24, 84 if expanded else 61,
                        )
                        for _delay, callback, args in reversed(callbacks) if reverse else callbacks:
                            self.assertLess(args[0], self.win._size_generation)
                            self.win.resize.reset_mock()
                            self.assertFalse(callback(*args))
                            self.win.resize.assert_not_called()
                        self.flush_resizes(reverse)
                        self.area.set_size_request.assert_called_with(*expected)
                        self.win.resize.assert_called_with(*expected)
                        self.assertEqual(self.win.orient, orient)
                        self.assertEqual(self.win.get_position(), (800, 350))

    def test_queued_resize_uses_latest_drag_orientation_and_expansion(self):
        self.enter()
        self.run_idles()
        self.win.on_click(None, self.event())
        self.win.get_position.return_value = (1680, 400)
        self.win.on_configure(None, None)
        self.win.pointer.return_value = (None, -1, -1, 0)
        self.win.on_release(None, self.event(x=-1, y=-1))
        self.flush_resizes(reverse=True)
        self.assertEqual(self.win.orient, "right")
        self.win.resize.assert_called_with(24, 61)
        self.assertEqual(self.win.get_position(), (1676, 400))
        self.assertFalse(self.win.close_visible)

    def test_repeated_enter_does_not_queue_duplicate_resizes(self):
        for _ in range(4):
            self.enter()
        self.glib.idle_add.assert_called_once_with(self.win.apply_orient_size)
        self.flush_resizes()
        self.enter()
        self.glib.idle_add.assert_called_once_with(self.win.apply_orient_size)

    def test_stale_crossing_events_use_current_pointer_not_event_coordinates(self):
        self.enter()
        self.flush_resizes()
        self.win.on_click(None, self.close_event())
        self.win.pointer.return_value = (None, 70.5, 12, 0)
        self.win.on_leave(None, self.event(x=-1, y=-1))
        self.assertTrue(self.win.close_visible)
        self.assertIsNotNone(self.win._close_press)
        self.leave()
        self.win.on_enter(None, self.event())
        self.assertFalse(self.win.close_visible)
        self.win.on_release(None, self.close_event())
        self.gtk.main_quit.assert_not_called()

    def test_stale_expanded_tail_cannot_reopen_after_collapse(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                self.enter()
                self.flush_resizes()
                self.leave()
                event = self.close_event()
                self.win.pointer.return_value = (None, event.x, event.y, 0)
                self.win.on_enter(None, event)
                self.assertFalse(self.win.close_visible)
                self.flush_resizes()
                self.win.resize.assert_called_with(*((61, 24) if orient == "top" else (24, 61)))

    def test_expanded_drag_defers_collapse_and_reconciles_release_position(self):
        for orient in ("top", "left", "right"):
            for inside in (False, True):
                with self.subTest(orient=orient, inside=inside):
                    self.set_orientation(orient, expanded=True)
                    self.win.on_click(None, self.event())
                    self.leave()
                    self.assertTrue(self.win._dragging)
                    self.assertTrue(self.win.close_visible)
                    self.assertEqual(self.idles, [])
                    event = self.close_event() if inside else self.event(x=-1, y=-1)
                    self.win.pointer.return_value = (None, event.x, event.y, 0)
                    self.win.on_release(None, event)
                    self.assertFalse(self.win._dragging)
                    self.assertEqual(self.win.close_visible, inside)
                    self.flush_resizes()
        self.gtk.main_quit.assert_not_called()

    def test_window_manager_swallowed_release_is_detected_without_configure(self):
        for inside in (False, True):
            with self.subTest(inside=inside):
                self.set_orientation("top", expanded=True)
                self.win.on_click(None, self.event())
                self.glib.timeout_add.assert_called_with(50, self.win.check_drag)
                self.win.pointer.return_value = (None, -1, -1, 256)
                self.win.on_leave(None, self.event(x=-1, y=-1))
                self.assertTrue(self.win.check_drag())
                self.assertTrue(self.win._dragging)
                self.assertTrue(self.win.close_visible)
                x, y = (70.5, 12) if inside else (-1, -1)
                self.win.pointer.return_value = (None, x, y, 0)
                self.assertFalse(self.win.check_drag())
                self.assertFalse(self.win._dragging)
                self.assertIsNone(self.win._drag_check_source)
                self.assertEqual(self.win.close_visible, inside)
                self.flush_resizes()
                self.win.on_release(None, self.close_event())
        self.gtk.main_quit.assert_not_called()

    def test_drag_poll_does_not_duplicate_after_quick_release_and_new_press(self):
        self.win.on_click(None, self.event())
        self.win.on_release(None, self.event())
        self.win.on_click(None, self.event())
        self.assertEqual([entry[1] for entry in self.timeouts].count(self.win.check_drag), 1)
        self.win.pointer.return_value = (None, 12, 12, 256)
        self.assertTrue(self.win.check_drag())
        self.win.on_release(None, self.event())
        self.assertFalse(self.win.check_drag())
        self.assertIsNone(self.win._drag_check_source)

    def test_hover_and_drag_cleanup_tolerate_unrealized_window(self):
        self.win.get_window.return_value = None
        self.assertFalse(self.enter())
        self.assertFalse(self.leave())
        self.win.on_click(None, self.event())
        self.assertFalse(self.win.check_drag())
        self.assertFalse(self.win._dragging)
        self.assertIsNone(self.win._drag_check_source)
        self.gtk.main_quit.assert_not_called()

    def test_close_hit_padding_is_clickable_but_slot_margins_are_not(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient, expanded=True)
                x, y, _w, _h = self.win.close_rect()
                event = self.event(x=x, y=y)
                self.gtk.main_quit.reset_mock()
                self.win.on_click(None, event)
                self.assertFalse(self.win.on_release(None, event))
                self.gtk.main_quit.assert_not_called()
                event = self.cap_event(76.25, 12)
                self.win.on_click(None, event)
                self.assertTrue(self.win.on_release(None, event))
                self.gtk.main_quit.assert_called_once_with()
        self.win.begin_move_drag.assert_not_called()

    def test_pending_expansion_cannot_activate_unpainted_close_target(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_orientation(orient)
                self.enter()
                event = self.close_event()
                self.assertFalse(self.win.close_contains(event))
                self.win.on_click(None, event)
                self.assertIsNone(self.win._close_press)
                self.flush_resizes()
                self.win.on_release(None, event)
        self.gtk.main_quit.assert_not_called()

    def test_hidden_tail_and_collapsed_lamps_never_activate_close(self):
        for orient in ("top", "left", "right"):
            for expanded in (False, True):
                with self.subTest(orient=orient, expanded=expanded):
                    self.set_orientation(orient, expanded)
                    if not expanded:
                        self.assertFalse(self.win.close_contains(self.close_event()))
                    for position in (12, 30.5, 49):
                        x, y = (position, 12) if orient == "top" else (12, position)
                        self.assertFalse(self.win.close_contains(self.event(x=x, y=y)))
        self.gtk.main_quit.assert_not_called()

    def test_clamp_moving_close_target_cancels_pending_activation(self):
        self.set_orientation("top", expanded=True)
        self.win.get_position.return_value = (1639, 350)
        self.win.on_click(None, self.close_event())
        self.win.force_size()
        self.assertEqual(self.win.get_position(), (1616, 350))
        self.win.on_release(None, self.close_event())
        self.gtk.main_quit.assert_not_called()

    def test_preferences_path_uses_xdg_or_home_without_accessing_real_state(self):
        root = self.preferences.parent.parent
        for xdg in (str(root / "xdg"), "", None):
            with self.subTest(xdg=xdg):
                env = {"HOME": str(root)}
                if xdg is not None:
                    env["XDG_STATE_HOME"] = xdg
                with mock.patch.dict(self.widget.os.environ, env, clear=True):
                    expected = Path(xdg) if xdg else root / ".local/state"
                    self.assertEqual(self.resolve_preferences_path(),
                                     expected / "opencode-traffic-light/preferences.json")
        self.assertFalse(self.preferences.parent.exists())

    def test_missing_preferences_default_without_creating_state(self):
        self.assertEqual(self.win.scale, 1.0)
        self.assertEqual(self.widget.load_scale(self.preferences), 1.0)
        self.assertFalse(self.preferences.parent.exists())
        self.win.add_tick_callback.assert_not_called()

    def test_scale_validation_rejects_bool_nonfinite_and_out_of_range(self):
        for value in (None, True, False, "1.5", [], {}, 0, -1, 0.74999, 2.00001,
                      float("nan"), float("inf"), -float("inf"), 10 ** 1000):
            with self.subTest(value=value):
                self.assertEqual(self.widget.validated_scale(value), 1.0)
        for value in (0.75, 1, 1.375, 2):
            with self.subTest(value=value):
                self.assertEqual(self.widget.validated_scale(value), value)
                self.assertIsInstance(self.widget.validated_scale(value), float)

    def test_malformed_preferences_and_read_errors_fall_back_to_default(self):
        self.preferences.parent.mkdir()
        bodies = [b"", b"{", b"\xff", b'{"scale":1.5} trailing', b"[" * 2000 + b"]" * 2000]
        bodies.extend(json.dumps(value).encode() for value in (
            None, True, 1.5, "1.5", [], {}, {"scale": True}, {"scale": False},
            {"scale": "1.5"}, {"scale": None}, {"scale": []}, {"scale": {}},
            {"scale": float("nan")}, {"scale": float("inf")}, {"scale": -float("inf")},
            {"scale": 0.74}, {"scale": 2.01},
        ))
        for body in bodies:
            with self.subTest(body=body[:80]):
                self.preferences.write_bytes(body)
                self.assertEqual(self.widget.load_scale(self.preferences), 1.0)
                self.assertEqual(self.preferences.read_bytes(), body)
        self.assertEqual(self.widget.load_scale(self.preferences.parent), 1.0)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertEqual(self.widget.load_scale(self.preferences), 1.0)

    def test_saved_scale_is_loaded_before_initial_geometry_and_centering(self):
        for scale in (0.75, 1.25, 2.0):
            with self.subTest(scale=scale):
                self.assertTrue(self.widget.save_scale(self.preferences, scale))
                win = self.widget.Notch(preferences_path=self.preferences, clock=self.clock)
                expected = math.ceil(61 * scale), math.ceil(24 * scale)
                self.assertEqual(win.scale, scale)
                self.assertEqual(win.widget_size(), expected)
                self.area.set_size_request.assert_called_with(*expected)
                win.center_on_top()
                win.move.assert_called_once_with(100 + (1600 - expected[0]) // 2, 50)
                win.add_tick_callback.assert_not_called()

    def test_save_is_atomic_and_preserves_other_state_files(self):
        self.preferences.parent.mkdir()
        self.preferences.write_text('{"scale":1.25}', encoding="utf-8")
        disabled = self.preferences.parent / "disabled"
        disabled.write_bytes(b"leave this marker alone")
        replace = self.widget.os.replace

        def check_replace(source, target):
            self.assertEqual(source.parent, self.preferences.parent)
            self.assertNotEqual(source, self.preferences)
            self.assertEqual(target, self.preferences)
            self.assertEqual(json.loads(source.read_text()), {"scale": 1.75})
            self.assertEqual(json.loads(target.read_text()), {"scale": 1.25})
            replace(source, target)

        with mock.patch.object(self.widget.os, "replace", side_effect=check_replace) as atomic:
            self.assertTrue(self.widget.save_scale(self.preferences, 1.75))
        atomic.assert_called_once()
        self.assertEqual(self.widget.load_scale(self.preferences), 1.75)
        self.assertEqual(disabled.read_bytes(), b"leave this marker alone")
        self.assertEqual(set(self.preferences.parent.iterdir()), {self.preferences, disabled})

    def test_save_failures_are_quiet_and_clean_up_temporary_files(self):
        self.assertTrue(self.widget.save_scale(self.preferences, 1.25))
        for owner, name in ((Path, "mkdir"), (self.widget.tempfile, "NamedTemporaryFile"),
                            (self.widget.json, "dump"), (self.widget.os, "fsync"),
                            (self.widget.os, "replace")):
            with self.subTest(operation=name):
                with mock.patch.object(owner, name, side_effect=OSError("denied")), \
                        mock.patch("builtins.print") as output:
                    self.assertFalse(self.widget.save_scale(self.preferences, 1.75))
                    output.assert_not_called()
                self.assertEqual(self.widget.load_scale(self.preferences), 1.25)
                self.assertEqual(list(self.preferences.parent.iterdir()), [self.preferences])

    def test_scaled_sizes_and_disjoint_control_targets_in_all_orientations(self):
        for scale in (0.75, 1.0, 1.375, 2.0):
            for orient in ("top", "left", "right"):
                for expanded in (False, True):
                    with self.subTest(scale=scale, orient=orient, expanded=expanded):
                        self.set_scale(scale)
                        self.set_orientation(orient, expanded)
                        size = math.ceil((84 if expanded else 61) * scale), math.ceil(24 * scale)
                        self.assertEqual(self.win.widget_size(), size if orient == "top" else size[::-1])
                        for start, length, rect, contains in (
                            (61, 19, self.win.close_rect(), self.win.close_contains),
                            (75, 9, self.win.resize_rect(), self.win.resize_contains),
                        ):
                            base = (start, 0, length, 24) if orient == "top" else (0, start, 24, length)
                            self.assertEqual(rect, tuple(value * scale for value in base))
                            x, y, w, h = rect
                            for px, py in ((x, y), (x + w - 0.001, y + h - 0.001)):
                                self.assertFalse(contains(self.event(x=px, y=py)))
                            for px, py in ((x - 0.001, y), (x, y - 0.001), (x + w, y), (x, y + h)):
                                self.assertFalse(contains(self.event(x=px, y=py)))
                        self.assertFalse(self.win.close_contains(self.resize_event()))
                        self.assertFalse(self.win.resize_contains(self.close_event()))
                        # Bounds overlap, but the actual circular/end-cap targets never do.
                        for long in (61, 64.5, 70.5, 72, 74.99, 75, 76.5, 77, 78.49, 78.5, 82, 84):
                            for short in (0, 2, 6, 10, 12, 18, 22, 24):
                                event = self.cap_event(long, short)
                                close = expanded and math.hypot(long - 70.5, short - 12) <= 6
                                resize = (expanded and 75 <= long < 84 and 0 <= short < 24
                                          and math.hypot(long - 72, short - 12) <= 12
                                          and math.hypot(long - 70.5, short - 12) >= 8)
                                self.assertEqual(self.win.close_contains(event), close, (long, short))
                                self.assertEqual(self.win.resize_contains(event), resize, (long, short))

    def test_scaled_intermediate_drawing_keeps_round_capsule_and_fixed_lamps(self):
        for scale in (0.75, 1.375, 2):
            for orient in ("top", "left", "right"):
                for reveal in (0, 0.25, 0.5, 1):
                    with self.subTest(scale=scale, orient=orient, reveal=reveal):
                        self.set_scale(scale)
                        self.set_orientation(orient)
                        self.win._presentation["reveal"] = reveal
                        cr = mock.Mock()
                        self.win.on_draw(self.area, cr)
                        cr.scale.assert_called_once_with(scale, scale)
                        length = 61 + 23 * reveal
                        w, h = (length, 24) if orient == "top" else (24, length)
                        self.assertEqual(cr.arc.call_args_list[:4], [
                            mock.call(w - 12, 12, 12, -math.pi / 2, 0),
                            mock.call(w - 12, h - 12, 12, 0, math.pi / 2),
                            mock.call(12, h - 12, 12, math.pi / 2, math.pi),
                            mock.call(12, 12, 12, math.pi, 3 * math.pi / 2),
                        ])
                        cr.fill_preserve.assert_called_once_with()
                        cr.clip.assert_called_once_with()
                        self.assertEqual(cr.set_source_rgb.call_args_list[1:], [
                            mock.call(*self.widget.RING_COLORS[name]) for name in ("red", "yellow", "green")
                        ])
                        expected = []
                        for position in (12, 30.5, 49):
                            cx, cy = (position, 12) if orient == "top" else (12, position)
                            expected.extend([mock.call(cx, cy, radius, 0, 2 * math.pi) for radius in (7.5, 5.25)])
                        self.assertEqual(cr.arc.call_args_list[4:10], expected)
                        if reveal:
                            cr.set_source_rgba.assert_any_call(1, 1, 1, reveal)
                            cr.set_source_rgba.assert_any_call(0.55, 0.55, 0.57, reveal)
                            self.assertEqual(cr.set_line_width.call_args_list, [mock.call(0.65), mock.call(1.5)])
                            arc = ((73, 12, 9, math.radians(-70), math.radians(70)) if orient == "top"
                                   else (12, 73, 9, math.radians(20), math.radians(160)))
                            cr.assert_has_calls([
                                mock.call.set_source_rgba(0.55, 0.55, 0.57, reveal),
                                mock.call.set_line_width(1.5), mock.call.set_line_cap(0),
                                mock.call.arc(*arc), mock.call.stroke(),
                            ])
                            self.assertEqual(cr.arc.call_args_list[-1], mock.call(*arc))
                        else:
                            cr.stroke.assert_not_called()

    def test_resize_drag_is_immediate_long_axis_only_and_persists_on_release(self):
        self.enable_motion()
        for orient, position in (("top", (800, 350)), ("left", (100, 350)), ("right", (1676, 350))):
            with self.subTest(orient=orient):
                self.set_scale(1)
                self.win._committed_scale = 1
                self.set_orientation(orient, expanded=True)
                self.win.get_position.return_value = position
                with mock.patch.object(self.widget, "save_scale", wraps=self.widget.save_scale) as save:
                    self.win.on_click(None, self.resize_event())
                    self.assertIsNotNone(self.win._resize_gesture)
                    self.assertFalse(self.win._dragging)
                    self.win.on_motion(None, self.resize_event(cross=500))
                    self.assertEqual(self.win.scale, 1)
                    self.win.on_motion(None, self.resize_event(delta=42, cross=-500))
                    self.assertEqual(self.win.scale, 1.5)
                    expected_size = (126, 36) if orient == "top" else (36, 126)
                    self.win.resize.assert_called_with(*expected_size)
                    expected_position = (1664, 350) if orient == "right" else position
                    self.assertEqual(self.win.get_position(), expected_position)
                    self.win.on_configure(None, None)
                    self.assertEqual(self.win.orient, orient)
                    self.leave()
                    self.win.set_close_visible(False)
                    self.assertTrue(self.win.close_visible)
                    save.assert_not_called()
                    self.win.pointer.return_value = (None, 12, 12, 0)
                    self.assertTrue(self.win.on_release(None, self.resize_event(delta=42)))
                    save.assert_called_once_with(self.preferences, 1.5)
                    self.assertEqual(self.widget.load_scale(self.preferences), 1.5)
                    self.assertIsNone(self.win._resize_gesture)
                    self.assertIsNone(self.win._size_anchor)
        self.win.begin_move_drag.assert_not_called()
        self.win.add_tick_callback.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_resize_clamps_scale_and_screen_but_restores_initial_anchor_on_return(self):
        for orient, position, clamped in (("top", (1639, 920), (1532, 902)),
                                          ("left", (100, 889), (100, 782)),
                                          ("right", (1676, 889), (1652, 782))):
            with self.subTest(orient=orient):
                self.set_scale(1)
                self.set_orientation(orient, expanded=True)
                self.win.get_position.return_value = position
                self.win.on_click(None, self.resize_event())
                self.win.on_motion(None, self.resize_event(delta=10000))
                self.assertEqual(self.win.scale, 2)
                self.assertEqual(self.win.get_position(), clamped)
                self.win.on_motion(None, self.resize_event(delta=-10000))
                self.assertEqual(self.win.scale, 0.75)
                self.assertEqual(self.win.orient, orient)
                self.win.on_motion(None, self.resize_event(delta=0))
                geo = self.monitor.get_workarea()
                w, h = self.win.widget_size()
                self.assertEqual(self.win.get_position(),
                                 (min(position[0], geo.x + geo.width - w),
                                  min(position[1], geo.y + geo.height - h)))
                self.win.on_release(None, self.resize_event())
        self.assertFalse(self.preferences.exists())
        self.win.begin_move_drag.assert_not_called()

    def test_resize_capture_accepts_outside_motion_and_defers_collapse_until_release(self):
        self.enable_motion()
        self.set_orientation("top", expanded=True)
        self.win.on_click(None, self.resize_event())
        self.leave()
        outside = self.event(x=-100, y=-100, x_root=121, y_root=-100)
        self.assertTrue(self.win.on_motion(None, outside))
        self.assertEqual(self.win.scale, 1.25)
        self.assertTrue(self.win.close_visible)
        self.assertFalse(self.win.on_release(None, self.event(button=3)))
        self.assertIsNotNone(self.win._resize_gesture)
        self.assertTrue(self.win.on_release(None, outside))
        self.assertFalse(self.win.close_visible)
        self.assertEqual(self.win._presentation["reveal"], 1)
        self.frame(0.075)
        self.assertAlmostEqual(self.win._presentation["reveal"], 0.5)
        self.frame(0.15)
        self.assertEqual(self.win.widget_size(), (77, 30))
        self.assertEqual(self.widget.load_scale(self.preferences), 1.25)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_unchanged_resize_hover_move_and_frames_never_save(self):
        with mock.patch.object(self.widget, "save_scale") as save:
            self.enter()
            self.flush_resizes()
            self.win.on_click(None, self.resize_event())
            self.win.on_motion(None, self.resize_event(delta=21))
            self.win.on_motion(None, self.resize_event())
            self.win.on_release(None, self.resize_event())
            self.win.on_click(None, self.event())
            self.win.on_release(None, self.event())
            self.leave()
            self.flush_resizes()
            self.enable_motion()
            self.enter()
            self.frame(0.075)
            self.frame(0.15)
            self.win.reset_scale()
            save.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_resize_release_over_close_commits_final_pointer_without_closing(self):
        for orient in ("top", "left", "right"):
            for scale in (0.75, 1, 1.5):
                with self.subTest(orient=orient, scale=scale):
                    self.set_scale(scale)
                    self.win._committed_scale = scale
                    self.set_orientation(orient, expanded=True)
                    self.win.on_click(None, self.resize_event())
                    release = self.close_event()
                    release.x_root, release.y_root = (121, 200) if orient == "top" else (100, 221)
                    self.assertTrue(self.win.on_release(None, release))
                    self.assertEqual(self.win.scale, scale + 0.25)
                    self.assertEqual(self.widget.load_scale(self.preferences), scale + 0.25)
                    self.assertIsNone(self.win._resize_gesture)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_failed_commit_does_not_break_resize_or_reset(self):
        self.set_orientation("top", expanded=True)
        with mock.patch.object(self.widget.os, "replace", side_effect=PermissionError("denied")):
            self.win.on_click(None, self.resize_event())
            self.win.on_motion(None, self.resize_event(delta=42))
            self.win.on_release(None, self.resize_event(delta=42))
            self.assertEqual(self.win.scale, 1.5)
            self.assertEqual(self.win.widget_size(), (126, 36))
            self.assertIsNone(self.win._resize_gesture)
            self.win.on_click(None, self.resize_event(double=True))
            self.assertEqual(self.win.scale, 1)
            self.assertEqual(self.win.widget_size(), (84, 24))
        self.assertFalse(self.preferences.exists())
        self.assertEqual(list(self.preferences.parent.iterdir()), [])
        self.gtk.main_quit.assert_not_called()

    def test_double_click_sequence_resets_and_saves_once_in_every_orientation(self):
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.set_scale(1.5)
                self.win._committed_scale = 1.5
                self.set_orientation(orient, expanded=True)
                with mock.patch.object(self.widget, "save_scale", wraps=self.widget.save_scale) as save:
                    self.win.on_click(None, self.resize_event())
                    self.win.on_release(None, self.resize_event())
                    self.win.on_click(None, self.resize_event())
                    self.win.on_click(None, self.resize_event(double=True))
                    self.assertFalse(self.win.on_release(None, self.resize_event()))
                    self.assertEqual(self.win.scale, 1)
                    self.assertIsNone(self.win._resize_gesture)
                    self.assertEqual(self.win.widget_size(), (84, 24) if orient == "top" else (24, 84))
                    save.assert_called_once_with(self.preferences, 1.0)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_right_click_arc_reveals_without_resize_close_or_menu(self):
        for orient in ("top", "left", "right"):
            self.set_orientation(orient)
            event = self.resize_event(button=3)
            self.assertTrue(self.win.on_click(None, event))
            self.flush_resizes()
            self.assertTrue(self.win.close_visible)
            self.assertIsNone(self.win._resize_gesture)
            self.assertFalse(self.win.on_release(None, event))
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_hover_animation_start_mid_end_opacity_and_idle_cleanup(self):
        self.enable_motion()
        for orient in ("top", "left", "right"):
            with self.subTest(orient=orient):
                self.animation_clock.return_value = 0
                self.set_orientation(orient)
                self.enter()
                self.assertEqual(self.win._presentation["reveal"], 0)
                self.assertEqual(self.win.widget_size(), (61, 24) if orient == "top" else (24, 61))
                self.assertEqual(len(self.ticks), 1)
                self.frame(0.0375)
                self.assertAlmostEqual(self.win._presentation["reveal"], 0.15625)
                self.frame(0.075)
                self.assertAlmostEqual(self.win._presentation["reveal"], 0.5)
                self.assertEqual(self.win.widget_size(), (73, 24) if orient == "top" else (24, 73))
                cr = mock.Mock()
                self.win.on_draw(self.area, cr)
                cr.set_source_rgba.assert_any_call(1, 1, 1, 0.5)
                cr.set_source_rgba.assert_any_call(0.55, 0.55, 0.57, 0.5)
                self.frame(0.15)
                self.assertEqual(self.win._presentation["reveal"], 1)
                self.assertTrue(self.win.close_contains(self.close_event()))
                self.assertTrue(self.win.resize_contains(self.resize_event()))
                self.assertEqual(self.ticks, {})
                self.assertIsNone(self.win._tick_id)
                self.win.resize.reset_mock()
                self.area.queue_draw.reset_mock()
                self.frame(1)
                self.win.resize.assert_not_called()
                self.area.queue_draw.assert_not_called()
                self.leave()
                self.assertFalse(self.win.close_contains(self.close_event()))
                self.frame(1.075)
                self.assertAlmostEqual(self.win._presentation["reveal"], 0.5)
                self.frame(1.151)
                self.assertEqual(self.win._presentation["reveal"], 0)
                self.assertEqual(self.ticks, {})
        self.assertEqual(self.idles, [])
        self.assertEqual([delay for delay, _callback, _args in self.timeouts], [400])

    def test_hover_reversal_retargets_current_frame_without_overshoot(self):
        self.enable_motion()
        self.enter()
        self.frame(0.06)
        self.assertAlmostEqual(self.win._presentation["reveal"], 0.352)
        self.leave()
        self.assertAlmostEqual(self.win._presentation["reveal"], 0.352)
        self.frame(0.135)
        current = self.win._presentation["reveal"]
        self.assertAlmostEqual(current, 0.176)
        self.enter()
        self.assertEqual(self.win._presentation["reveal"], current)
        for now in (0.15, 0.18, 0.21, 0.25, 0.286):
            self.frame(now)
            self.assertGreaterEqual(self.win._presentation["reveal"], current)
            self.assertLessEqual(self.win._presentation["reveal"], 1)
            current = self.win._presentation["reveal"]
        self.assertEqual(current, 1)
        self.assertEqual(self.ticks, {})
        self.win.add_tick_callback.assert_called_once_with(self.win.on_tick)

    def test_retarget_samples_elapsed_time_between_frame_callbacks(self):
        self.enable_motion()
        self.enter()
        self.animation_clock.return_value = 0.05
        self.leave()
        expected = (1 / 3) ** 2 * (3 - 2 / 3)
        self.assertAlmostEqual(self.win._presentation["reveal"], expected)
        self.assertAlmostEqual(self.win._animations["reveal"][0], expected)
        self.frame(0.125)
        self.assertAlmostEqual(self.win._presentation["reveal"], expected / 2)

    def test_partial_reveal_and_hide_cannot_activate_either_control_or_move(self):
        self.enable_motion()
        self.enter()
        self.frame(0.075)
        for event in (self.close_event(), self.resize_event(), self.resize_event(double=True)):
            self.assertFalse(self.win.close_contains(event))
            self.assertFalse(self.win.resize_contains(event))
            self.win.on_click(None, event)
            self.assertFalse(self.win.on_release(None, event))
            self.assertIsNone(self.win._resize_gesture)
        self.frame(0.15)
        self.win.on_click(None, self.close_event())
        self.leave()
        for event in (self.close_event(), self.resize_event(), self.resize_event(double=True)):
            self.win.on_click(None, event)
            self.assertFalse(self.win.on_release(None, event))
            self.assertIsNone(self.win._resize_gesture)
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_allocation_must_contain_whole_control_before_activation(self):
        for orient in ("top", "left", "right"):
            self.set_orientation(orient, expanded=True)
            self.assertTrue(self.win.close_contains(self.close_event()))
            self.assertTrue(self.win.resize_contains(self.resize_event()))
            for length in (80, 83):
                self.allocate(*((length, 24) if orient == "top" else (24, length)))
                self.assertTrue(self.win.close_contains(self.close_event()))
                self.assertFalse(self.win.resize_contains(self.cap_event(75, 2)))
                self.assertFalse(self.win.resize_contains(self.resize_event()))
            for length, short in ((79, 24), (80, 23)):
                self.allocate(*((length, short) if orient == "top" else (short, length)))
                self.assertFalse(self.win.close_contains(self.close_event()))
                self.assertFalse(self.win.resize_contains(self.resize_event()))

    def test_animated_reset_preserves_side_docking_and_saves_endpoint_not_frames(self):
        self.enable_motion()
        self.set_scale(2)
        self.win._committed_scale = 2
        self.set_orientation("right", expanded=True)
        self.win.get_position.return_value = (1652, 350)
        with mock.patch.object(self.widget, "save_scale", wraps=self.widget.save_scale) as save:
            self.win.on_click(None, self.resize_event(double=True))
            self.assertEqual(self.win.scale, 1)
            self.assertEqual(self.win._presentation["scale"], 2)
            self.assertEqual(self.widget.load_scale(self.preferences), 1)
            self.frame(0.075)
            self.assertAlmostEqual(self.win._presentation["scale"], 1.5)
            self.assertEqual(self.win.widget_size(), (36, 126))
            self.assertEqual(self.win.get_position(), (1664, 350))
            self.frame(0.15)
            self.assertEqual(self.win.widget_size(), (24, 84))
            self.assertEqual(self.win.get_position(), (1676, 350))
            save.assert_called_once_with(self.preferences, 1.0)
        self.assertEqual(self.ticks, {})
        self.win.begin_move_drag.assert_not_called()
        self.gtk.main_quit.assert_not_called()

    def test_reset_can_be_interrupted_by_immediate_resize_from_presentation_scale(self):
        self.enable_motion()
        self.set_scale(2)
        self.set_orientation("top", expanded=True)
        self.win.reset_scale()
        self.frame(0.075)
        self.win.on_click(None, self.resize_event())
        self.assertEqual(self.win.scale, 1.5)
        self.assertEqual(self.ticks, {})
        self.win.on_motion(None, self.resize_event(delta=21))
        self.assertEqual(self.win.scale, 1.75)
        self.frame(0.2)
        self.assertEqual(self.win._presentation["scale"], 1.75)
        self.win.on_release(None, self.resize_event(delta=21))
        self.assertEqual(self.widget.load_scale(self.preferences), 1.75)

    def test_lamps_crossfade_and_retarget_without_moving_or_resizing(self):
        self.enable_motion()
        self.respond(b'{"state":"red"}')
        self.assertTrue(self.win.poll_status())
        self.assertEqual(self.win.state, "red")
        self.assertEqual(self.win._presentation["red"], 0.15)
        self.assertEqual(self.win._presentation["green"], 1)
        self.frame(0.03)
        self.assertAlmostEqual(self.win._presentation["red"], 0.15 + 0.85 * 0.15625)
        self.frame(0.06)
        self.assertAlmostEqual(self.win._presentation["red"], 0.575)
        self.assertAlmostEqual(self.win._presentation["green"], 0.575)
        self.respond(b'{"state":"yellow"}')
        self.win.poll_status()
        self.assertAlmostEqual(self.win._presentation["red"], 0.575)
        self.frame(0.12)
        self.assertAlmostEqual(self.win._presentation["red"], 0.3625)
        self.assertAlmostEqual(self.win._presentation["yellow"], 0.575)
        self.frame(0.181)
        for name in ("red", "yellow", "green"):
            self.assertEqual(self.win._presentation[name], 1 if name == "yellow" else 0.15)
        self.win.resize.assert_not_called()
        self.win.move.assert_not_called()
        self.assertEqual(self.ticks, {})
        self.win.add_tick_callback.assert_called_once_with(self.win.on_tick)

    def test_simultaneous_hover_reset_and_lamp_fade_share_one_short_lived_tick(self):
        self.enable_motion()
        self.set_scale(1.75)
        self.enter()
        self.win.reset_scale()
        self.respond(b'{"state":"red"}')
        self.win.poll_status()
        self.frame(0.12)
        self.assertEqual(self.win._presentation["red"], 1)
        self.assertNotIn("red", self.win._animations)
        self.assertIn("scale", self.win._animations)
        self.assertIn("reveal", self.win._animations)
        self.assertEqual(len(self.ticks), 1)
        self.frame(0.15)
        self.assertEqual(self.win.widget_size(), (84, 24))
        self.assertEqual(self.ticks, {})
        self.win.add_tick_callback.assert_called_once_with(self.win.on_tick)

    def test_moving_finishes_geometry_immediately_and_defers_collapse(self):
        self.enable_motion()
        self.enter()
        self.frame(0.075)
        self.win.on_click(None, self.event())
        self.win.begin_move_drag.assert_called_once_with(1, 123, 456, 42)
        self.assertEqual(self.win._presentation["reveal"], 1)
        self.assertEqual(self.win.widget_size(), (84, 24))
        self.assertEqual(self.ticks, {})
        self.leave()
        self.assertTrue(self.win.close_visible)
        self.assertEqual(self.ticks, {})
        self.win.on_release(None, self.event(x=-1, y=-1))
        self.assertFalse(self.win.close_visible)
        self.assertEqual(self.win._presentation["reveal"], 1)
        self.frame(0.226)
        self.assertEqual(self.win.widget_size(), (61, 24))
        self.assertEqual(self.ticks, {})

    def test_animation_anchors_clamp_each_frame_without_orientation_changes(self):
        self.enable_motion()
        self.win.get_position.return_value = (1639, 350)
        self.enter()
        self.frame(0.075)
        self.assertEqual(self.win.get_position(), (1627, 350))
        self.win.on_configure(None, None)
        self.assertEqual(self.win.orient, "top")
        self.leave()
        self.frame(0.226)
        self.assertEqual(self.win.get_position(), (1639, 350))
        self.assertEqual(self.win.orient, "top")
        self.assertIsNone(self.win._size_anchor)

    def test_stale_requisition_callbacks_cannot_override_animation_or_direct_resize(self):
        self.enter()
        self.run_idles()
        callback = self.timeouts[-1]
        self.enable_motion()
        self.leave()
        self.frame(0.075)
        expected = self.win.widget_size()
        self.win.resize.reset_mock()
        self.assertFalse(callback[1](*callback[2]))
        self.assertEqual(self.win.widget_size(), expected)
        self.win.resize.assert_not_called()
        self.frame(0.15)
        self.settings.get_property.return_value = False
        self.enter()
        self.run_idles()
        callback = self.timeouts[-1]
        self.win.force_size()
        self.win.on_click(None, self.resize_event())
        self.win.on_motion(None, self.resize_event(delta=42))
        self.win.resize.reset_mock()
        self.assertFalse(callback[1](*callback[2]))
        self.win.resize.assert_not_called()
        self.assertEqual(self.win.widget_size(), (126, 36))
        self.win.on_release(None, self.resize_event(delta=42))

    def test_stale_idle_reads_presentation_not_animation_endpoint(self):
        self.enter()
        self.enable_motion()
        self.leave()
        self.frame(0.075)
        self.run_idles()
        self.area.set_size_request.assert_called_with(73, 24)
        callback = self.timeouts[-1]
        self.frame(0.12)
        size = self.win.widget_size()
        self.assertFalse(callback[1](*callback[2]))
        self.win.resize.assert_called_with(*size)
        self.frame(0.15)
        self.assertEqual(self.win.widget_size(), (61, 24))

    def test_disabling_animations_midflight_applies_all_endpoints_and_stops_tick(self):
        self.enable_motion()
        self.set_scale(2)
        self.enter()
        self.win.reset_scale()
        self.respond(b'{"state":"red"}')
        self.win.poll_status()
        self.frame(0.04)
        self.settings.get_property.return_value = False
        self.frame(0.05)
        self.assertEqual(self.win.widget_size(), (84, 24))
        self.assertEqual(self.win._presentation["scale"], 1)
        self.assertEqual(self.win._presentation["red"], 1)
        self.assertEqual(self.win._presentation["green"], 0.15)
        self.assertEqual(self.ticks, {})
        self.assertIsNone(self.win._tick_id)
        self.settings.get_property.assert_called_with("gtk-enable-animations")

    def test_disabled_or_unavailable_settings_use_endpoints_without_ticks(self):
        for settings in (self.settings, None):
            with self.subTest(settings=settings):
                self.gtk.Settings.get_default.return_value = settings
                self.set_orientation("top")
                self.set_scale(2)
                self.enter()
                self.flush_resizes()
                self.assertEqual(self.win.widget_size(), (168, 48))
                self.win.reset_scale()
                self.assertEqual(self.win.widget_size(), (84, 24))
                self.respond(b'{"state":"red"}')
                self.win.poll_status()
                self.assertEqual(self.win._presentation["red"], 1)
        self.win.add_tick_callback.assert_not_called()

    def test_reduced_motion_retarget_finishes_scale_with_original_side_anchor(self):
        self.enable_motion()
        self.set_scale(2)
        self.set_orientation("right", expanded=True)
        self.win.get_position.return_value = (1652, 350)
        self.win.reset_scale()
        self.frame(0.075)
        self.assertEqual(self.win.get_position(), (1664, 350))
        self.settings.get_property.return_value = False
        self.leave()
        self.assertEqual(self.win.widget_size(), (24, 61))
        self.assertEqual(self.win.get_position(), (1676, 350))
        self.assertEqual(self.ticks, {})
        self.assertIsNone(self.win._size_anchor)
        self.assertEqual(self.idles, [])

    def test_reversal_before_first_frame_removes_inactive_tick(self):
        self.enable_motion()
        self.enter()
        identifier = self.win._tick_id
        self.leave()
        self.assertEqual(self.win.widget_size(), (61, 24))
        self.assertEqual(self.ticks, {})
        self.assertIsNone(self.win._tick_id)
        self.win.remove_tick_callback.assert_called_once_with(identifier)
        self.area.queue_draw.reset_mock()
        self.win.resize.reset_mock()
        self.assertFalse(self.win.on_tick(None, None))
        self.area.queue_draw.assert_not_called()
        self.win.resize.assert_not_called()

    def test_destroy_removes_active_animation_callback(self):
        self.enable_motion()
        self.enter()
        identifier = self.win._tick_id
        self.win.on_destroy()
        self.win.remove_tick_callback.assert_called_once_with(identifier)
        self.assertEqual(self.win._animations, {})
        self.assertEqual(self.ticks, {})
        self.assertIsNone(self.win._tick_id)

    def test_default_standalone_keeps_polling_retains_color_and_recovers(self):
        win = self.widget.Notch(preferences_path=self.preferences, clock=self.clock)
        self.glib.timeout_add.assert_called_with(400, win.poll_status)
        _delay, poll, _args = self.timeouts[-1]
        for now in (100, 105, 160):
            self.clock.return_value = now
            self.assertTrue(poll())
            self.assertEqual(win.state, "green")
        self.area.queue_draw.assert_not_called()

        self.clock.return_value = 161
        self.respond(b'{"state":"red"}')
        self.assertTrue(poll())
        self.assertEqual(win.state, "red")
        self.assertEqual(win._presentation["red"], 1)
        self.area.queue_draw.reset_mock()

        self.urlopen.side_effect = URLError("backend stopped")
        for now in (166, 3600):
            self.clock.return_value = now
            self.assertTrue(poll())
            self.assertEqual(win.state, "red")
            self.assertEqual(win._presentation["red"], 1)
        self.clock.return_value = 3601
        self.respond(b'{"state":"blue"}')
        self.assertTrue(poll())
        self.assertEqual(win.state, "red")
        self.area.queue_draw.assert_not_called()

        self.clock.return_value = 3602
        self.respond(b'{"state":"yellow"}')
        self.assertTrue(poll())
        self.assertEqual(win.state, "yellow")
        self.assertEqual(win._presentation["yellow"], 1)
        self.assertEqual(win._presentation["red"], 0.15)
        self.area.queue_draw.assert_called_once_with()
        self.gtk.main_quit.assert_not_called()
        self.assertFalse(self.preferences.exists())

    def test_main_disconnect_flag_is_opt_in(self):
        for args, exit_on_disconnect in (([], False), (["--exit-on-disconnect"], True)):
            with self.subTest(args=args):
                self.clock.return_value = 100
                self.gtk.main.reset_mock()
                self.gtk.main_quit.reset_mock()
                with mock.patch.object(sys, "argv", ["opencode-traffic-light.py", *args]), \
                        mock.patch.object(self.widget, "Notch", wraps=self.widget.Notch) as constructor:
                    self.widget.main()
                constructor.assert_called_once_with(exit_on_disconnect=exit_on_disconnect)
                _delay, poll, _args = self.timeouts[-1]
                poll.__self__.show_all.assert_called_once_with()
                self.gtk.main.assert_called_once_with()
                self.clock.return_value = 105
                self.assertEqual(poll(), not exit_on_disconnect)
                if exit_on_disconnect:
                    self.gtk.main_quit.assert_called_once_with()
                else:
                    self.gtk.main_quit.assert_not_called()
                self.assertFalse(self.preferences.exists())

    def test_startup_without_backend_exits_after_five_seconds(self):
        for now in (100, 102, 104.999):
            self.clock.return_value = now
            self.assertTrue(self.win.poll_status())
            self.gtk.main_quit.assert_not_called()
        self.clock.return_value = 105
        self.assertFalse(self.win.poll_status())
        self.gtk.main_quit.assert_called_once_with()
        self.assertEqual(self.win.state, "green")
        self.area.queue_draw.assert_not_called()

    def test_successful_states_use_loopback_and_one_second_timeout(self):
        for now, state, status in ((101, "red", 200), (102, "yellow", 201), (103, "green", 299)):
            with self.subTest(state=state, status=status):
                self.respond(json.dumps({"state": state}).encode(), status)
                self.clock.return_value = now
                self.area.queue_draw.reset_mock()
                self.assertTrue(self.win.poll_status())
                self.assertEqual(self.win.state, state)
                self.assertEqual(self.win._last_valid_response, now)
                self.area.queue_draw.assert_called_once_with()
                self.urlopen.assert_called_with("http://127.0.0.1:4390/status", timeout=1)
        self.gtk.main_quit.assert_not_called()

    def test_unchanged_valid_states_reset_grace_without_redraw(self):
        self.respond()
        for now in (104, 108, 112):
            self.clock.return_value = now
            self.assertTrue(self.win.poll_status())
            self.assertEqual(self.win._last_valid_response, now)
        self.urlopen.side_effect = URLError("backend stopped")
        self.clock.return_value = 116.999
        self.assertTrue(self.win.poll_status())
        self.gtk.main_quit.assert_not_called()
        self.clock.return_value = 117
        self.assertFalse(self.win.poll_status())
        self.gtk.main_quit.assert_called_once_with()
        self.area.queue_draw.assert_not_called()

    def test_invalid_json_and_states_retain_color_and_do_not_reset_grace(self):
        self.respond(b'{"state":"red"}')
        self.assertTrue(self.win.poll_status())
        self.area.queue_draw.reset_mock()
        bodies = [b"", b"{", b"\xff", b'{"state":"green"} trailing']
        bodies.extend(json.dumps(value).encode() for value in (
            None, True, 42, "red", [], ["red"], [["state", "green"]], {},
            {"color": "green"}, {"state": None}, {"state": True}, {"state": 1},
            {"state": []}, {"state": {}}, {"state": ""}, {"state": "blue"},
            {"state": "GREEN"}, {"state": "green "},
        ))
        for body in bodies:
            with self.subTest(body=body):
                self.gtk.main_quit.reset_mock()
                self.respond(body)
                self.clock.return_value = 104.999
                self.assertTrue(self.win.poll_status())
                self.gtk.main_quit.assert_not_called()
                self.assertEqual(self.win.state, "red")
                self.assertEqual(self.win._last_valid_response, 100)
                self.clock.return_value = 105
                self.assertFalse(self.win.poll_status())
                self.gtk.main_quit.assert_called_once_with()
                self.assertEqual(self.win.state, "red")
                self.area.queue_draw.assert_not_called()

    def test_non_success_http_never_resets_grace_even_with_valid_state(self):
        for status in (199, 300, 302, 400, 404, 500, 503):
            with self.subTest(status=status):
                self.gtk.main_quit.reset_mock()
                self.respond(b'{"state":"red"}', status)
                self.clock.return_value = 104.999
                self.assertTrue(self.win.poll_status())
                self.gtk.main_quit.assert_not_called()
                self.assertEqual(self.win._last_valid_response, 100)
                self.clock.return_value = 105
                self.assertFalse(self.win.poll_status())
                self.gtk.main_quit.assert_called_once_with()
                self.assertEqual(self.win.state, "green")
                self.area.queue_draw.assert_not_called()

    def test_http_and_network_exceptions_still_check_deadline(self):
        errors = (
            URLError("unreachable"), TimeoutError("timed out"), ConnectionResetError("reset"),
            HTTPError(self.widget.STATUS_URL, 503, "unavailable", None, None),
        )
        for error in errors:
            with self.subTest(error=error):
                self.gtk.main_quit.reset_mock()
                self.urlopen.side_effect = error
                self.clock.return_value = 104
                self.assertTrue(self.win.poll_status())
                self.gtk.main_quit.assert_not_called()
                self.clock.return_value = 105
                self.assertFalse(self.win.poll_status())
                self.gtk.main_quit.assert_called_once_with()
                self.assertEqual(self.win._last_valid_response, 100)

    def test_response_read_error_still_checks_deadline(self):
        response = self.respond()
        response.read.side_effect = TimeoutError("read timed out")
        self.clock.return_value = 105
        self.assertFalse(self.win.poll_status())
        self.gtk.main_quit.assert_called_once_with()
        self.assertEqual(self.win._last_valid_response, 100)
        response.__exit__.assert_called_once()

    def test_failed_request_crossing_deadline_quits_in_same_poll(self):
        def timeout(*_args, **_kwargs):
            self.clock.return_value = 105.5
            raise TimeoutError("timed out")

        self.clock.return_value = 104.5
        self.urlopen.side_effect = timeout
        self.assertFalse(self.win.poll_status())
        self.gtk.main_quit.assert_called_once_with()

    def test_success_records_response_completion_time(self):
        def read():
            self.clock.return_value = 104.8
            return b'{"state":"green"}'

        self.clock.return_value = 104
        self.respond().read.side_effect = read
        self.assertTrue(self.win.poll_status())
        self.assertEqual(self.win._last_valid_response, 104.8)
        self.gtk.main_quit.assert_not_called()

    def test_valid_response_at_deadline_keeps_widget_alive(self):
        self.clock.return_value = 105
        self.respond()
        self.assertTrue(self.win.poll_status())
        self.assertEqual(self.win._last_valid_response, 105)
        self.gtk.main_quit.assert_not_called()

    def test_wall_clock_not_used_for_disconnect_timing(self):
        with mock.patch.object(self.widget.time, "time", side_effect=AssertionError("wall clock used")):
            self.clock.return_value = 104
            self.respond()
            self.assertTrue(self.win.poll_status())
            self.assertEqual(self.win._last_valid_response, 104)
            self.urlopen.side_effect = URLError("backend stopped")
            self.clock.return_value = 109
            self.assertFalse(self.win.poll_status())
        self.gtk.main_quit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
