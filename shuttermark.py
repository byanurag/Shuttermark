#!/usr/bin/env python3
# Shuttermark — local Wayland screenshot capture and annotation for GNOME.
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""Shuttermark — local Wayland screenshot capture and annotation for GNOME."""
import configparser
import copy
import io
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

import cairo

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango, PangoCairo


APP_ID = "io.github.byanurag.shuttermark"
VERSION = "0.6.3"
DEBUG = os.environ.get("SHUTTERMARK_DEBUG") == "1"
SIDEBAR_WIDTH = 138
INK = (0.94, 0.27, 0.22, 1.0)
HIGHLIGHT = (1.0, 0.82, 0.15, 0.42)
TOOLS = ("Select", "Pen", "Arrow", "Rectangle", "Ellipse", "Text", "Highlight", "Pixelate")
THEMES = ("System", "Light", "Dark")
PIXEL_BLOCK = 14
WATCH_EVENTS = ("CREATED", "CHANGES_DONE_HINT", "MOVED_IN", "RENAMED")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def config_path():
    return os.path.join(GLib.get_user_config_dir(), "shuttermark", "settings.ini")


def _read_config():
    cfg = configparser.ConfigParser()
    try:
        cfg.read(config_path())
    except (configparser.Error, OSError):
        pass
    return cfg


def _write_config_value(section, key, value):
    cfg = _read_config()
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, key, value)
    try:
        os.makedirs(os.path.dirname(config_path()), exist_ok=True)
        with open(config_path(), "w") as fh:
            cfg.write(fh)
    except OSError as exc:
        print(f"Could not save settings: {exc}")


def load_theme_pref():
    try:
        name = _read_config().get("ui", "theme").strip().capitalize()
        if name in THEMES:
            return name
    except (configparser.Error, OSError):
        pass
    return "System"


def save_theme_pref(name):
    _write_config_value("ui", "theme", name)


def default_save_dir():
    try:
        directory = _read_config().get("save", "directory")
        if directory and os.path.isdir(directory):
            return directory
    except (configparser.Error, OSError):
        pass
    pics = os.path.expanduser("~/Pictures")
    if os.path.isdir(pics):
        return pics
    return os.path.expanduser("~")


def save_save_dir(directory):
    _write_config_value("save", "directory", directory)


def system_prefers_dark():
    try:
        iface = Gio.Settings.new("org.gnome.desktop.interface")
        return iface.get_string("color-scheme") == "prefer-dark"
    except GLib.Error:
        pass
    except Exception as exc:
        print(f"Theme probe failed: {exc}")
    return False


def apply_theme(name):
    settings = Gtk.Settings.get_default()
    if settings is None:
        return
    if name == "Dark":
        dark = True
    elif name == "Light":
        dark = False
    else:
        dark = system_prefers_dark()
    settings.set_property("gtk-application-prefer-dark-theme", dark)


def looks_like_screenshot(path):
    base = os.path.basename(path)
    low = base.lower()
    return (base.startswith("Screenshot")
            and low.endswith(IMAGE_EXTS)
            and os.path.isfile(path))


@dataclass
class Mark:
    kind: str
    points: list = field(default_factory=list)
    color: tuple = INK
    width: int = 4
    text: str = ""
    font_size: int = 0  # Text only; 0 = legacy (derived from width)


def text_mark_size(mark):
    if mark.font_size:
        return mark.font_size
    return max(12, mark.width * 5)


def mark_bbox(mark):
    """Bounding box (x, y, w, h) for a mark, used for selection and hit-testing."""
    if not mark.points:
        return None
    if mark.kind == "Text":
        x, y = mark.points[0]
        # Approximate until we measure with Pango; wide enough for selection.
        size = text_mark_size(mark)
        lines = mark.text.splitlines() or [""]
        w = max(24.0, max((len(line) for line in lines), default=0) * size * 0.62)
        return (x, y, w, size * 1.35 * len(lines))
    xs = [p[0] for p in mark.points]
    ys = [p[1] for p in mark.points]
    if mark.kind == "Arrow":
        pad = mark.width * 3.0 + 4.0  # arrowhead extends past the tip
    else:
        pad = mark.width / 2.0 + 4.0
    x1, y1 = min(xs) - pad, min(ys) - pad
    x2, y2 = max(xs) + pad, max(ys) + pad
    return (x1, y1, x2 - x1, y2 - y1)


def point_segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def mark_hit_test(mark, x, y):
    if not mark.points:
        return False
    if mark.kind == "Pen":
        tol = mark.width / 2.0 + 12.0
        pts = mark.points
        if len(pts) == 1:
            return math.hypot(x - pts[0][0], y - pts[0][1]) <= tol
        return any(
            point_segment_distance(x, y, ax, ay, bx, by) <= tol
            for (ax, ay), (bx, by) in zip(pts, pts[1:])
        )
    # Everything else (shapes, arrows, text) grabs by its selection box,
    # so right/left-drag anywhere inside the dashed outline moves it.
    box = mark_bbox(mark)
    if box is None:
        return False
    bx, by, bw, bh = box
    return bx <= x <= bx + bw and by <= y <= by + bh


def pen_grab_hit(mark, x, y, extra):
    """Fat screen-constant hit test for Pen/Arrow strokes.

    `extra` is in image coords (caller passes e.g. 16/zoom so the grab
    stays chunky at any zoom). Returns True when the cursor is within
    stroke width + extra of any segment.
    """
    if not mark.points:
        return False
    tol = mark.width / 2.0 + extra
    pts = mark.points
    if len(pts) == 1:
        return math.hypot(x - pts[0][0], y - pts[0][1]) <= tol
    return any(
        point_segment_distance(x, y, ax, ay, bx, by) <= tol
        for (ax, ay), (bx, by) in zip(pts, pts[1:])
    )


HANDLE_R = 14
EDGE_TOL = 16.0
HANDLE_CURSORS = {
    "nw": "nwse-resize", "se": "nwse-resize",
    "ne": "nesw-resize", "sw": "nesw-resize",
    "n": "ns-resize", "s": "ns-resize",
    "e": "ew-resize", "w": "ew-resize",
}


def selection_handles(box):
    """Eight resize handles (corners + edge midpoints) for a bbox."""
    x, y, w, h = box
    return {
        "nw": (x, y), "n": (x + w / 2, y), "ne": (x + w, y),
        "w": (x, y + h / 2), "e": (x + w, y + h / 2),
        "sw": (x, y + h), "s": (x + w / 2, y + h), "se": (x + w, y + h),
    }


def handle_at(box, px, py, r=HANDLE_R):
    for name, (hx, hy) in selection_handles(box).items():
        if abs(px - hx) <= r and abs(py - hy) <= r:
            return name
    return None


def resized_bbox(box, handle, dx, dy, min_size=10):
    """Drag a handle by (dx, dy); substring matching covers shared edges
    (e.g. "w" matches the west edge of "sw"). Never inverts or collapses."""
    x, y, w, h = box
    nx, ny, nw, nh = x, y, w, h
    if "e" in handle:
        nw = max(min_size, w + dx)
    if "s" in handle:
        nh = max(min_size, h + dy)
    if "w" in handle:
        edge = min(x + dx, x + w - min_size)
        nw, nx = (x + w) - edge, edge
    if "n" in handle:
        edge = min(y + dy, y + h - min_size)
        nh, ny = (y + h) - edge, edge
    return (nx, ny, nw, nh)


def apply_resize(mark, old_box, new_box, handle, orig_font_size=0):
    """Map a mark from old_box to new_box. Text scales its font in place,
    keeping its anchor glued to the dragged corner."""
    ox, oy, ow, oh = old_box
    nx, ny, nw, nh = new_box
    if mark.kind == "Text":
        base = orig_font_size or text_mark_size(mark)
        k = max(nw / max(ow, 1), nh / max(oh, 1))
        mark.font_size = max(8, min(160, round(base * k)))
        ax, ay = mark.points[0]
        if "w" in handle:
            ax += nx - ox
        if "n" in handle:
            ay += ny - oy
        mark.points = [(ax, ay)]
        return
    sx = nw / ow if ow else 1.0
    sy = nh / oh if oh else 1.0
    mark.points = [(nx + (px - ox) * sx, ny + (py - oy) * sy)
                   for px, py in mark.points]


def paint_pixelated_pixbuf(cr, pixbuf, x, y, w, h, block=PIXEL_BLOCK):
    """Paint a truly pixelated copy of the pixbuf region (privacy-safe)."""
    ix, iy, iw, ih = int(x), int(y), int(w), int(h)
    if iw < 2 or ih < 2:
        return
    # Clamp to image bounds.
    ix = max(0, ix)
    iy = max(0, iy)
    iw = min(iw, pixbuf.get_width() - ix)
    ih = min(ih, pixbuf.get_height() - iy)
    if iw < 2 or ih < 2:
        return
    try:
        sub = GdkPixbuf.Pixbuf.new_subpixbuf(pixbuf, ix, iy, iw, ih)
    except GLib.Error:
        return
    tw = max(1, iw // block)
    th = max(1, ih // block)
    tiny = sub.scale_simple(tw, th, GdkPixbuf.InterpType.NEAREST)
    if tiny is None:
        return
    big = tiny.scale_simple(iw, ih, GdkPixbuf.InterpType.NEAREST)
    if big is None:
        return
    Gdk.cairo_set_source_pixbuf(cr, big, ix, iy)
    cr.rectangle(ix, iy, iw, ih)
    cr.fill()


class CanvasScroll(Gtk.ScrolledWindow):
    """ScrolledWindow that reports viewport resizes (GTK4 has no
    size-allocate signal, so we hook the allocator instead)."""

    def __init__(self, on_resize):
        super().__init__(hexpand=True, vexpand=True)
        self._on_resize = on_resize

    def do_size_allocate(self, width, height, baseline):
        Gtk.ScrolledWindow.do_size_allocate(self, width, height, baseline)
        if self._on_resize is not None:
            self._on_resize(width, height)


class Canvas(Gtk.DrawingArea):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.pixbuf = None
        self.marks = []
        self.ocr_boxes = []
        self.active = None
        self.selected = None
        self._move_grab = None
        self._resize_grab = None
        self._hover_cursor = None
        self.zoom = 1.0
        self.set_content_width(900)
        self.set_content_height(560)
        self.set_draw_func(self.draw)
        self.set_focusable(True)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.motion)
        self.add_controller(motion)
        # Dedicated controllers so left/right are never confused
        # (get_current_button proved unreliable in the wild).
        left = Gtk.GestureClick()
        left.set_button(1)
        left.connect("pressed", lambda g, n, x, y: self.press(g, n, x, y, button=1))
        left.connect("released", self.release)
        self.add_controller(left)
        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("pressed", lambda g, n, x, y: self.press(g, n, x, y, button=3))
        right.connect("released", self.release)
        self.add_controller(right)

    def load(self, path):
        self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(path)
        self.zoom = 1.0
        self.set_content_width(self.pixbuf.get_width())
        self.set_content_height(self.pixbuf.get_height())
        self.marks, self.ocr_boxes = [], []
        self.active = None
        self.selected = None
        self._move_grab = None
        self._resize_grab = None
        self.queue_draw()
        # Fit immediately when the viewport already has a size (repeat
        # loads) so no full-size frame flashes; the idle pass covers
        # first show while the window is still laying out.
        self.window.fit_layout()
        GLib.idle_add(lambda: (self.window.fit_layout(), False)[1])

    def image_coords(self, x, y):
        z = self.zoom or 1.0
        return (x / z, y / z)

    def tool(self):
        return self.window.tool_name()

    def text_at(self, x, y):
        for mark in reversed(self.marks):
            if mark.kind == "Text" and mark_hit_test(mark, x, y):
                return mark
        return None

    def _pop_ocr_box(self, x, y):
        """Remove one OCR highlight under the cursor.

        OCR boxes aren't marks, so Select couldn't touch them at all —
        every click felt dead. Returns True when one was removed.
        """
        for i in range(len(self.ocr_boxes) - 1, -1, -1):
            ox, oy, ow, oh, _text = self.ocr_boxes[i]
            if ox <= x <= ox + ow and oy <= y <= oy + oh:
                self.ocr_boxes.pop(i)
                self.selected = None
                self._move_grab = None
                self.queue_draw()
                self.window.set_status(
                    "Removed that OCR highlight — Clear marks removes all")
                return True
        return False

    def _handle_radius(self):
        return HANDLE_R / (self.zoom or 1.0)

    def _pen_tolerance(self):
        # Fat screen-constant grab for ink so thin strokes are easy to
        # pick up even when zoomed to fit.
        return EDGE_TOL / (self.zoom or 1.0)

    def _start_resize_grab(self, x, y, handle, box):
        self._move_grab = None
        self._resize_grab = (
            handle, x, y,
            [tuple(p) for p in self.selected.points], box,
            text_mark_size(self.selected),
        )
        self.queue_draw()

    def _start_move_grab(self, mark, x, y):
        self.selected = mark
        self._move_grab = (x, y, [tuple(p) for p in mark.points])
        self.window.set_status(
            f"Selected {mark.kind} — drag to move, handles to resize")
        self.queue_draw()

    def _press_button(self, gesture):
        try:
            if gesture is not None and hasattr(gesture, "get_current_button"):
                return gesture.get_current_button()
        except (AttributeError, TypeError):
            pass
        return 1

    def _shift_held(self, gesture):
        try:
            if gesture is not None and hasattr(gesture, "get_current_event_state"):
                state = gesture.get_current_event_state()
                if isinstance(state, tuple):
                    state = state[-1]
                return bool(state & Gdk.ModifierType.SHIFT_MASK)
        except (AttributeError, TypeError):
            pass
        return False

    def _full_hit(self, x, y):
        """Topmost mark under (x, y), whole interior included.

        Used for right-drag so the entire inside of a rectangle (or any
        shape) grabs and moves.
        """
        for mark in reversed(self.marks):
            if mark_hit_test(mark, x, y):
                return mark
        return None

    # -- input ---------------------------------------------------------
    def press(self, gesture, n_press, x, y, button=None):
        self.grab_focus()
        if not self.pixbuf:
            return
        x, y = self.image_coords(x, y)
        if button is None:
            button = self._press_button(gesture)
        if DEBUG:
            print(f"DBG press tool={self.tool()} button={button} n_press={n_press} "
                  f"x={x:.1f} y={y:.1f} marks={len(self.marks)} "
                  f"ocr={len(self.ocr_boxes)}", flush=True)
        # Resize handles work in every tool so a drawn mark can always
        # be grabbed without switching tools.
        if self.selected is not None and self.selected in self.marks:
            box = mark_bbox(self.selected)
            handle = handle_at(box, x, y, r=self._handle_radius()) if box else None
            if handle is not None:
                self._start_resize_grab(x, y, handle, box)
                return
        if button == 3:
            # Right-hold drags the whole mark by its interior in any
            # tool.
            hit = self._full_hit(x, y)
            if hit is not None:
                self._start_move_grab(hit, x, y)
                if DEBUG:
                    print(f"DBG right-drag {hit.kind}", flush=True)
            else:
                self.selected = None
                self._move_grab = None
                self.queue_draw()
            return
        if self.tool() == "Text":
            self.window.prompt_text(x, y, existing=self.text_at(x, y))
            return
        if self.tool() == "Select":
            if n_press == 2:
                hit_text = self.text_at(x, y)
                if hit_text is not None:
                    self.selected = hit_text
                    self.queue_draw()
                    self.window.prompt_text(x, y, existing=hit_text)
                    return
            hit = None
            for mark in reversed(self.marks):
                if mark_hit_test(mark, x, y):
                    hit = mark
                    break
            self.selected = hit
            if hit is not None:
                self._move_grab = (x, y, [tuple(p) for p in hit.points])
                self.window.set_status(
                    f"Selected {hit.kind} — drag to move, handles to resize")
                if DEBUG:
                    print(f"DBG selected {hit.kind} points={hit.points}", flush=True)
            elif self._pop_ocr_box(x, y):
                return
            else:
                self._move_grab = None
                if self.marks:
                    self.window.set_status(
                        "Click a mark to select it — drag to move it")
                else:
                    self.window.set_status(
                        "Nothing to move yet — pick Pen, Arrow or a shape and draw")
                if DEBUG:
                    print("DBG selected nothing", flush=True)
            self.queue_draw()
            return
        # Drawing tools: left-drag anywhere inside an existing mark
        # picks it up to move (whole interior, not just the edge).
        # Hold Shift to force a new drawing on top of an old mark.
        # Clicks on empty space draw as before.
        if not (button == 1 and self._shift_held(gesture)):
            hit = self._full_hit(x, y)
            if hit is not None:
                self._start_move_grab(hit, x, y)
                if DEBUG:
                    print(f"DBG picked up {hit.kind} with {self.tool()} tool",
                          flush=True)
                return
        self.selected = None
        self.active = Mark(self.tool(), [(x, y)],
                           self.window.current_color(),
                           self.window.stroke_width())
        self.marks.append(self.active)
        self.queue_draw()

    def motion(self, controller, x, y):
        if not self.pixbuf:
            return
        x, y = self.image_coords(x, y)
        # An in-progress move/resize wins in every tool so a mark picked
        # up with Pen/Arrow/shapes keeps following the cursor.
        if self._resize_grab is not None \
                and self.selected is not None \
                and self.selected in self.marks:
            handle, sx, sy, orig_pts, orig_box, orig_font = self._resize_grab
            mark = self.selected
            mark.points = [tuple(p) for p in orig_pts]
            if mark.kind == "Text":
                mark.font_size = orig_font
            new_box = resized_bbox(orig_box, handle, x - sx, y - sy)
            apply_resize(mark, orig_box, new_box, handle, orig_font)
            if mark.kind == "Text":
                try:
                    self.window.size_text.set_value(mark.font_size)
                except (AttributeError, TypeError):
                    pass
            self.queue_draw()
            return
        if self._move_grab is not None \
                and self.selected is not None \
                and self.selected in self.marks:
            gx, gy, orig = self._move_grab
            dx, dy = x - gx, y - gy
            self.selected.points = [(ox + dx, oy + dy) for ox, oy in orig]
            self.queue_draw()
            return
        if self.active is not None:
            if self.active.kind == "Pen":
                self.active.points.append((x, y))
            else:
                if len(self.active.points) == 1:
                    self.active.points.append((x, y))
                else:
                    self.active.points[-1] = (x, y)
            self.queue_draw()
            return
        self._update_hover_cursor(x, y)

    def release(self, gesture, n_press, x, y):
        if self._resize_grab is not None:
            self._resize_grab = None
            self.queue_draw()
            return
        if self._move_grab is not None:
            self._move_grab = None
            self.queue_draw()
            return
        if self.active is not None and self.pixbuf:
            x, y = self.image_coords(x, y)
            if self.active.kind != "Pen" and len(self.active.points) > 1:
                self.active.points[-1] = (x, y)
                x1, y1 = self.active.points[0]
                x2, y2 = self.active.points[-1]
                if abs(x2 - x1) < 3 and abs(y2 - y1) < 3:
                    self.marks.pop()  # accidental click, not a shape
                else:
                    self.selected = self.active
                    self.window.set_status(
                        f"{self.selected.kind} added — drag inside it to move it")
            elif self.active.kind == "Pen":
                self.active.points.append((x, y))
                if len(self.active.points) < 2:
                    self.marks.pop()
                else:
                    self.selected = self.active
                    self.window.set_status(
                        "Stroke added — drag inside it to move it")
            self.active = None
            self.queue_draw()

    def _update_hover_cursor(self, x, y):
        if not hasattr(self, "set_cursor"):
            return
        name = None
        hr = self._handle_radius()
        if self.selected is not None and self.selected in self.marks:
            box = mark_bbox(self.selected)
            if box is not None:
                handle = handle_at(box, x, y, r=hr)
                if handle is not None:
                    name = HANDLE_CURSORS[handle]
                elif mark_hit_test(self.selected, x, y):
                    # Left- or right-drag anywhere inside moves.
                    name = "move"
                elif self.selected.kind in ("Pen", "Arrow"):
                    if pen_grab_hit(self.selected, x, y,
                                    self._pen_tolerance()):
                        name = "move"
        if name is None:
            if self.tool() != "Text":
                if self._full_hit(x, y) is not None:
                    name = "move"
            elif self.tool() == "Text":
                # Text tool still shows move on other marks so they look
                # draggable (right-drag moves them even in Text tool).
                if self._full_hit(x, y) is not None:
                    name = "move"
        if name != self._hover_cursor:
            self._hover_cursor = name
            try:
                self.set_cursor(Gdk.Cursor.new_from_name(name, None) if name else None)
            except (GLib.Error, TypeError):
                pass

    def delete_selected(self):
        if self.selected in self.marks:
            self.marks.remove(self.selected)
            self.selected = None
            self._move_grab = None
            self._resize_grab = None
            self.queue_draw()
            return True
        return False

    # -- painting ------------------------------------------------------
    def draw_mark(self, cr, mark, pixbuf_for_pixelate=None):
        if not mark.points:
            return
        r, g, b, a = mark.color
        cr.set_line_width(mark.width)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        x1, y1 = mark.points[0]
        if mark.kind == "Pen":
            cr.set_source_rgba(r, g, b, a)
            cr.move_to(x1, y1)
            for x, y in mark.points[1:]:
                cr.line_to(x, y)
            cr.stroke()
        elif mark.kind in ("Rectangle", "Highlight", "Pixelate") and len(mark.points) > 1:
            x2, y2 = mark.points[-1]
            x, y = min(x1, x2), min(y1, y2)
            w, h = abs(x2 - x1), abs(y2 - y1)
            if mark.kind == "Highlight":
                cr.set_source_rgba(r, g, b, 0.35)
                cr.rectangle(x, y, w, h)
                cr.fill()
            elif mark.kind == "Pixelate":
                if pixbuf_for_pixelate is not None:
                    paint_pixelated_pixbuf(cr, pixbuf_for_pixelate, x, y, w, h)
                else:
                    cr.set_source_rgba(.12, .12, .14, .70)
                    cr.rectangle(x, y, w, h)
                    cr.fill()
            else:
                cr.set_source_rgba(r, g, b, a)
                cr.rectangle(x, y, w, h)
                cr.stroke()
        elif mark.kind == "Ellipse" and len(mark.points) > 1:
            x2, y2 = mark.points[-1]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            cr.set_source_rgba(r, g, b, a)
            cr.save()
            cr.translate(cx, cy)
            cr.scale(max(abs(x2 - x1) / 2, 1), max(abs(y2 - y1) / 2, 1))
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.stroke()
        elif mark.kind == "Arrow" and len(mark.points) > 1:
            x2, y2 = mark.points[-1]
            angle = math.atan2(y2 - y1, x2 - x1)
            head = max(10, mark.width * 3)
            cr.set_source_rgba(r, g, b, a)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            for d in (math.pi * .78, -math.pi * .78):
                cr.move_to(x2, y2)
                cr.line_to(x2 + head * math.cos(angle + d),
                           y2 + head * math.sin(angle + d))
            cr.stroke()
        elif mark.kind == "Text":
            size = text_mark_size(mark)
            layout = PangoCairo.create_layout(cr)
            layout.set_text(mark.text, -1)
            layout.set_font_description(Pango.FontDescription(f"Sans Bold {size}"))
            off = max(1.5, size / 12.0)
            cr.save()
            cr.move_to(x1 + off, y1 + off)
            cr.set_source_rgba(0, 0, 0, 0.45)
            PangoCairo.show_layout(cr, layout)
            cr.restore()
            cr.move_to(x1, y1)
            cr.set_source_rgba(r, g, b, a)
            PangoCairo.show_layout(cr, layout)

    def draw_selection(self, cr):
        if self.selected is None or self.selected not in self.marks:
            return
        box = mark_bbox(self.selected)
        if box is None:
            return
        # Keep the outline chunky on screen at any zoom so it is easy to
        # see and grab (drawing happens inside a zoom-scaled context).
        z = self.zoom or 1.0
        lw = 2.2 / z
        dash = [9.0 / z, 6.0 / z]
        hs = 8.0 / z  # half-size -> 16px handles on screen
        x, y, w, h = box
        cr.save()
        cr.set_dash(dash, 0)
        cr.set_line_width(lw)
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        cr.set_dash(dash, dash[0])
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.9)
        cr.rectangle(x, y, w, h)
        cr.stroke()
        cr.restore()
        # Handles are shown in every tool so it is obvious the selection
        # can be dragged/resized without switching back to Select.
        for hx, hy in selection_handles(box).values():
            cr.set_source_rgba(1, 1, 1, 0.98)
            cr.rectangle(hx - hs, hy - hs, hs * 2, hs * 2)
            cr.fill()
            cr.set_source_rgba(0.1, 0.1, 0.1, 0.9)
            cr.set_line_width(1.4 / z)
            cr.rectangle(hx - hs, hy - hs, hs * 2, hs * 2)
            cr.stroke()

    def draw_ocr_labels(self, cr):
        for x, y, w, h, text in self.ocr_boxes:
            cr.set_source_rgba(*HIGHLIGHT)
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(.18, .12, 0, .9)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10)
            cr.move_to(x, max(10, y - 3))
            # Clamp to single line so cairo toy text never fails on newlines.
            cr.show_text(" ".join(str(text).split()))

    def draw(self, area, cr, width, height):
        cr.set_source_rgb(.11, .12, .14)
        cr.paint()
        if not self.pixbuf:
            cr.set_source_rgb(.7, .72, .75)
            cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(20)
            cr.move_to(32, 50)
            cr.show_text("Capture or open an image to start annotating")
            return
        cr.save()
        cr.scale(self.zoom, self.zoom)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        self.draw_ocr_labels(cr)
        for mark in self.marks:
            self.draw_mark(cr, mark, pixbuf_for_pixelate=self.pixbuf)
        self.draw_selection(cr)
        cr.restore()

    def surface(self):
        """Render the annotated image for export/clipboard/OCR."""
        w, h = self.pixbuf.get_width(), self.pixbuf.get_height()
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surface)
        Gdk.cairo_set_source_pixbuf(cr, self.pixbuf, 0, 0)
        cr.paint()
        for x, y, bw, bh, _text in self.ocr_boxes:
            cr.set_source_rgba(*HIGHLIGHT)
            cr.rectangle(x, y, bw, bh)
            cr.fill()
        for mark in self.marks:
            self.draw_mark(cr, mark, pixbuf_for_pixelate=self.pixbuf)
        surface.flush()
        return surface

    def png_bytes(self):
        buf = io.BytesIO()
        self.surface().write_to_png(buf)
        return buf.getvalue()


class Shuttermark(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Shuttermark")
        self.set_default_size(1120, 720)
        self.set_icon_name(APP_ID)
        self._monitors = []
        self._pending_watch = {}
        self._ignored_outputs = {}
        self._capturing = False
        self.build()
        self._start_watching()

    # -- ui ------------------------------------------------------------
    def build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)
        header = Gtk.HeaderBar()
        root.append(header)
        capture = Gtk.Button(label="Capture region", icon_name="camera-photo-symbolic")
        capture.connect("clicked", self.capture)
        header.pack_start(capture)
        self.side_btn = Gtk.ToggleButton(icon_name="sidebar-show-symbolic",
                                         tooltip_text="Toggle sidebar (F9)",
                                         active=True)
        self.side_btn.connect("toggled", self._on_sidebar_toggled)
        header.pack_start(self.side_btn)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open image")
        open_btn.connect("clicked", self.open_file)
        header.pack_start(open_btn)
        zoombox = Gtk.Box(spacing=0)
        zoombox.add_css_class("linked")
        zoom_out = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Zoom out (Ctrl+-)")
        zoom_out.connect("clicked", lambda *_: self.bump_zoom(1 / 1.25))
        zoombox.append(zoom_out)
        self.zoom_label = Gtk.Label(label="100%")
        self.zoom_label.set_size_request(48, -1)
        zoombox.append(self.zoom_label)
        zoom_in = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Zoom in (Ctrl+=)")
        zoom_in.connect("clicked", lambda *_: self.bump_zoom(1.25))
        zoombox.append(zoom_in)
        header.pack_start(zoombox)
        zoom_fit = Gtk.Button(icon_name="zoom-fit-best-symbolic", tooltip_text="Zoom to fit (Ctrl+0)")
        zoom_fit.connect("clicked", lambda *_: self.zoom_fit())
        header.pack_start(zoom_fit)
        self.status = Gtk.Label(label=f"Pick a tool, then draw (v{VERSION})")
        header.set_title_widget(self.status)
        save = Gtk.Button(icon_name="document-save-symbolic", tooltip_text="Export PNG (Ctrl+S)")
        save.connect("clicked", self.export)
        header.pack_end(save)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy image (Ctrl+C)")
        copy.connect("clicked", self.copy)
        header.pack_end(copy)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_margin_top(8)
        sidebar.set_margin_bottom(8)
        sidebar.set_margin_start(8)
        sidebar.set_margin_end(8)
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        side_scroll.set_size_request(SIDEBAR_WIDTH, -1)
        side_scroll.set_child(sidebar)
        self.side_scroll = side_scroll
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        root.append(pane)
        pane.set_start_child(side_scroll)
        # Keep the sidebar at its minimum: extra window space always
        # goes to the canvas, and the divider can't squeeze it further.
        pane.set_shrink_start_child(False)
        pane.set_resize_start_child(False)
        self.pane = pane
        self.tool_model = Gtk.StringList.new(TOOLS)
        dropdown = Gtk.DropDown(model=self.tool_model)
        dropdown.set_selected(0)  # Select: marks are movable from the start
        sidebar.append(Gtk.Label(label="Tool", xalign=0))
        sidebar.append(dropdown)
        self.tool_dropdown = dropdown
        sidebar.append(Gtk.Label(label="Stroke size", xalign=0))
        self.size = Gtk.SpinButton.new_with_range(1, 32, 1)
        self.size.set_value(4)
        sidebar.append(self.size)
        sidebar.append(Gtk.Label(label="Text size", xalign=0))
        self.size_text = Gtk.SpinButton.new_with_range(8, 96, 1)
        self.size_text.set_value(24)
        sidebar.append(self.size_text)
        sidebar.append(Gtk.Label(label="Color", xalign=0))
        self.color_btn = self._make_color_button()
        sidebar.append(self.color_btn)
        sidebar.append(Gtk.Label(label="Appearance", xalign=0))
        self.theme_model = Gtk.StringList.new(THEMES)
        theme_dropdown = Gtk.DropDown(model=self.theme_model)
        try:
            theme_dropdown.set_selected(THEMES.index(load_theme_pref()))
        except ValueError:
            theme_dropdown.set_selected(0)
        theme_dropdown.connect("notify::selected", self._on_theme_changed)
        sidebar.append(theme_dropdown)
        self.theme_dropdown = theme_dropdown
        apply_theme(self.theme_name())
        self._follow_system_theme()
        sidebar.append(Gtk.Label(label="Save location", xalign=0))
        self.save_dir_btn = Gtk.Button(icon_name="folder-symbolic")
        self.save_dir_btn.connect("clicked", self.choose_save_dir)
        sidebar.append(self.save_dir_btn)
        self._refresh_save_button()
        ocr = Gtk.Button(label="Find text (OCR)", icon_name="edit-find-symbolic",
                         tooltip_text="Find text (OCR)")
        ocr.connect("clicked", self.ocr)
        sidebar.append(ocr)
        undo = Gtk.Button(label="Undo", icon_name="edit-undo-symbolic",
                          tooltip_text="Undo (Ctrl+Z)")
        undo.connect("clicked", lambda *_: self.undo())
        sidebar.append(undo)
        clear = Gtk.Button(label="Clear marks", icon_name="edit-clear-symbolic",
                           tooltip_text="Clear marks")
        clear.connect("clicked", lambda *_: self.clear())
        sidebar.append(clear)
        self.watch_toggle = Gtk.CheckButton()
        self.watch_toggle.set_tooltip_text(
            "While open, load new GNOME screenshots from ~/Pictures")
        self.watch_toggle.set_active(True)
        watch_label = Gtk.Label(label="Auto-open screenshots", wrap=True, xalign=0)
        self.watch_toggle.set_child(watch_label)
        sidebar.append(self.watch_toggle)

        # While the image is in auto-fit mode (fresh load or Fit),
        # keep it fitted when the canvas area changes (window resize,
        # sidebar drag). Any manual zoom turns this off.
        self._auto_zoom = True
        self._refit_pending = False
        scroll = CanvasScroll(self._on_viewport_resized)
        self.canvas = Canvas(self)
        scroll.set_child(self.canvas)
        pane.set_end_child(scroll)
        self.scroll = scroll

        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>s"),
            Gtk.NamedAction.new("win.save")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>c"),
            Gtk.NamedAction.new("win.copy")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>z"),
            Gtk.NamedAction.new("win.undo")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>d"),
            Gtk.NamedAction.new("win.duplicate")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>minus"),
            Gtk.NamedAction.new("win.zoom-out")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>equal"),
            Gtk.NamedAction.new("win.zoom-in")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>0"),
            Gtk.NamedAction.new("win.zoom-fit")))
        keys.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("F9"),
            Gtk.NamedAction.new("win.sidebar")))
        self.add_controller(keys)
        key_events = Gtk.EventControllerKey()
        key_events.connect("key-pressed", self.on_key)
        self.add_controller(key_events)

        actions = Gio.SimpleActionGroup()
        for name, handler in (("save", self.export),
                              ("copy", self.copy),
                              ("undo", self.undo),
                              ("duplicate", self.duplicate_selected),
                               ("zoom-out", lambda: self.bump_zoom(1 / 1.25)),
                               ("zoom-in", lambda: self.bump_zoom(1.25)),
                               ("zoom-fit", self.zoom_fit),
                               ("sidebar", self.toggle_sidebar)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, h=handler: h())
            actions.add_action(action)
        self.insert_action_group("win", actions)

    def toggle_sidebar(self, *_args):
        self.side_btn.set_active(not self.side_btn.get_active())

    def _on_sidebar_toggled(self, _btn):
        shown = self.side_btn.get_active()
        self.side_scroll.set_visible(shown)
        if shown:
            GLib.idle_add(lambda: (self.fit_layout(), False)[1])

    # -- zoom ----------------------------------------------------------
    def set_zoom(self, zoom):
        if not self.canvas.pixbuf:
            return
        zoom = max(0.05, min(4.0, zoom))
        self.canvas.zoom = zoom
        iw = self.canvas.pixbuf.get_width()
        ih = self.canvas.pixbuf.get_height()
        self.canvas.set_content_width(max(1, round(iw * zoom)))
        self.canvas.set_content_height(max(1, round(ih * zoom)))
        self.zoom_label.set_text(f"{round(zoom * 100)}%")
        self.canvas.queue_draw()

    def bump_zoom(self, factor):
        self._auto_zoom = False
        self.set_zoom(self.canvas.zoom * factor)

    def zoom_fit(self, _retries=10):
        self._auto_zoom = True
        if not self.canvas.pixbuf:
            return
        vw, vh = self.scroll.get_width() - 24, self.scroll.get_height() - 24
        if (vw <= 50 or vh <= 50) and _retries > 0:
            GLib.timeout_add(80, lambda: (self.zoom_fit(_retries - 1), False)[1])
            return
        iw = self.canvas.pixbuf.get_width()
        ih = self.canvas.pixbuf.get_height()
        self.set_zoom(min(vw / iw, vh / ih, 1.0))

    def fit_layout(self):
        """Collapse the sidebar to its minimum and fit the image.

        Called on every image load so small screens always show as
        much of the picture as possible.
        """
        self.pane.set_position(SIDEBAR_WIDTH)
        self.zoom_fit()

    def _on_viewport_resized(self, _width, _height):
        if not self._auto_zoom:
            return
        if self.canvas.pixbuf is None or self._refit_pending:
            return
        self._refit_pending = True

        def _do():
            self._refit_pending = False
            if self._auto_zoom and self.canvas.pixbuf is not None:
                self.zoom_fit()
            return False

        GLib.idle_add(_do)

    def _make_color_button(self):
        rgba = Gdk.RGBA()
        rgba.parse("rgb(240,69,56)")
        if hasattr(Gtk, "ColorDialogButton"):
            dialog = Gtk.ColorDialog()
            dialog.set_with_alpha(True)
            btn = Gtk.ColorDialogButton.new(dialog)
            btn.set_rgba(rgba)
            return btn
        btn = Gtk.ColorButton()
        btn.set_rgba(rgba)
        return btn

    def theme_name(self):
        return THEMES[self.theme_dropdown.get_selected()]

    def _on_theme_changed(self, _dropdown, _pspec):
        name = self.theme_name()
        apply_theme(name)
        save_theme_pref(name)

    def _follow_system_theme(self):
        try:
            iface = Gio.Settings.new("org.gnome.desktop.interface")
            iface.connect("changed::color-scheme",
                          lambda *_: apply_theme(self.theme_name())
                          if self.theme_name() == "System" else None)
            self._theme_settings = iface  # keep referenced
        except (GLib.Error, Exception):
            pass

    # -- helpers -------------------------------------------------------
    def set_status(self, text):
        self.status.set_text(text)

    def current_color(self):
        rgba = self.color_btn.get_rgba()
        return (rgba.red, rgba.green, rgba.blue, rgba.alpha)

    def stroke_width(self):
        return self.size.get_value_as_int()

    def text_size(self):
        return self.size_text.get_value_as_int()

    def tool_name(self):
        return TOOLS[self.tool_dropdown.get_selected()]

    def load_path(self, path, what="Opened"):
        try:
            self.canvas.load(path)
            self.set_status(f"{what} {os.path.basename(path)}")
            return True
        except GLib.Error as exc:
            self.set_status(f"Could not open image: {exc.message}")
            return False

    # -- auto-open GNOME screenshots -----------------------------------
    def _screenshot_dirs(self):
        pics = os.path.expanduser("~/Pictures")
        return [d for d in (pics, os.path.join(pics, "Screenshots"))
                if os.path.isdir(d)]

    def _start_watching(self):
        for directory in self._screenshot_dirs():
            try:
                mon = Gio.File.new_for_path(directory).monitor_directory(
                    Gio.FileMonitorFlags.WATCH_MOVES, None)
                mon.connect("changed", self._on_watch_event)
                self._monitors.append(mon)  # keep referenced
            except GLib.Error as exc:
                print(f"Watch failed for {directory}: {exc.message}")

    def _on_watch_event(self, _mon, gfile, _other, event):
        if not self.watch_toggle.get_active():
            return
        if self._capturing:
            return
        try:
            wanted = {int(getattr(Gio.FileMonitorEvent, n)) for n in WATCH_EVENTS}
        except (TypeError, AttributeError):
            wanted = set()
        if wanted and int(event) not in wanted:
            return
        path = gfile.get_path()
        if not path or not looks_like_screenshot(path):
            return
        if path in self._pending_watch:
            return
        if time.monotonic() < self._ignored_outputs.get(path, 0):
            return
        self._pending_watch[path] = GLib.timeout_add(700, self._maybe_autoload, path)

    def _maybe_autoload(self, path):
        self._pending_watch.pop(path, None)
        if not self.watch_toggle.get_active():
            return False
        if self._capturing:
            # A capture is in progress and loads its file itself.
            return False
        if time.monotonic() < self._ignored_outputs.get(path, 0):
            return False
        try:
            if (not os.path.isfile(path) or os.path.getsize(path) == 0
                    or time.time() - os.path.getmtime(path) > 120):
                return False
        except OSError:
            return False
        try:
            if self.load_path(path, "Opened screenshot"):
                if not self.is_active():
                    self.present()
        except GLib.Error:
            pass
        return False

    def _note_output(self, path):
        # Ignore our own exports — and captures we've already loaded —
        # in the watcher for a while so saving an annotated
        # "Screenshot-*.png" doesn't reload itself.
        self._ignored_outputs[path] = time.monotonic() + 30

    def on_key(self, _controller, keyval, _keycode, state):
        name = Gdk.keyval_name(keyval)
        if name in ("Delete", "BackSpace", "KP_Delete"):
            if self.canvas.delete_selected():
                self.set_status("Mark deleted")
                return True
        elif name == "Escape":
            self.canvas.active = None
            self.canvas.selected = None
            self.canvas.queue_draw()
            return True
        elif name in ("Tab", "KP_Tab", "ISO_Left_Tab"):
            marks = self.canvas.marks
            if marks:
                shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
                if self.canvas.selected in marks:
                    idx = marks.index(self.canvas.selected)
                    step = -1 if shift else 1
                    self.canvas.selected = marks[(idx + step) % len(marks)]
                else:
                    self.canvas.selected = marks[-1] if shift else marks[0]
                sel = self.canvas.selected
                self.set_status(
                    f"Selected {sel.kind} "
                    f"({marks.index(sel) + 1}/{len(marks)})")
                self.canvas.queue_draw()
                return True
        else:
            arrow = {"Up": (0, -1), "Down": (0, 1), "Left": (-1, 0), "Right": (1, 0),
                     "KP_Up": (0, -1), "KP_Down": (0, 1),
                     "KP_Left": (-1, 0), "KP_Right": (1, 0)}.get(name)
            if arrow is not None:
                sel = self.canvas.selected
                if sel is not None and sel in self.canvas.marks:
                    step = 10 if state & Gdk.ModifierType.SHIFT_MASK else 1
                    dx, dy = arrow[0] * step, arrow[1] * step
                    sel.points = [(px + dx, py + dy) for px, py in sel.points]
                    self.canvas.queue_draw()
                    return True
        return False

    def prompt_text(self, x, y, existing=None):
        is_edit = existing is not None and existing in self.canvas.marks
        if is_edit:
            self.size_text.set_value(text_mark_size(existing))
        dialog = Gtk.Dialog(title="Edit label" if is_edit else "Add label",
                            transient_for=self, modal=True)
        dialog.set_default_size(340, -1)
        box = dialog.get_content_area()
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_spacing(8)
        box.append(Gtk.Label(label="Size comes from Text size in the sidebar.",
                             wrap=True, xalign=0))
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow(min_content_height=70)
        scroll.set_child(view)
        box.append(scroll)
        if is_edit:
            view.get_buffer().set_text(existing.text)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Save" if is_edit else "Add", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.connect("response", self._finish_text, x, y, view, existing)
        dialog.present()
        view.grab_focus()

    def _finish_text(self, dialog, response, x, y, view, existing):
        try:
            buf = view.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                                False).strip()
            if response == Gtk.ResponseType.OK and text:
                size = self.text_size()
                if existing is not None and existing in self.canvas.marks:
                    existing.text = text
                    existing.font_size = size
                    existing.width = max(1, size // 5)
                    self.canvas.selected = existing
                    self.set_status("Label updated")
                else:
                    mark = Mark("Text", [(x, y)], self.current_color(),
                                max(1, size // 5), text)
                    mark.font_size = size
                    self.canvas.marks.append(mark)
                    self.canvas.selected = mark
                    self.set_status("Label added")
                self.canvas.queue_draw()
        finally:
            dialog.close()

    def duplicate_selected(self, *_args):
        sel = self.canvas.selected
        if sel is None or sel not in self.canvas.marks:
            return
        dup = copy.copy(sel)
        dup.points = [(px + 14, py + 14) for px, py in sel.points]
        self.canvas.marks.insert(self.canvas.marks.index(sel) + 1, dup)
        self.canvas.selected = dup
        self.canvas.queue_draw()
        self.set_status(f"Duplicated {sel.kind}")

    # -- capture -------------------------------------------------------
    def capture(self, *_):
        self.set_status("Select an area in GNOME…")
        # Flag the blocking portal/Shell calls below: their file events
        # must not trigger a second watcher load of the same capture.
        self._capturing = True
        try:
            self._do_capture()
        finally:
            self._capturing = False

    def _do_capture(self):
        path, cancelled = self.capture_via_portal(interactive=True)
        if path:
            try:
                self.canvas.load(path)
                # Portal/Shell save into ~/Pictures; keep the user's file,
                # only remove our own temp copies (gnome-screenshot below).
                # Already loaded: keep the watcher from loading it again.
                self._note_output(path)
                self.set_status("Captured — draw or run OCR")
                return
            except GLib.Error as exc:
                self.set_status(f"Could not load capture: {exc.message}")
                return
        if cancelled:
            self.set_status("Capture cancelled")
            return
        path = self.capture_via_shell()
        if path:
            try:
                self.canvas.load(path)
                self._note_output(path)
                self.set_status("Captured — draw or run OCR")
                return
            except GLib.Error as exc:
                self.set_status(f"Could not load capture: {exc.message}")
                return
        if shutil.which("gnome-screenshot"):
            fd, path = tempfile.mkstemp(suffix=".png", prefix="shuttermark-")
            os.close(fd)
            try:
                proc = subprocess.run(["gnome-screenshot", "-a", "-f", path])
                if (proc.returncode == 0 and os.path.exists(path)
                        and os.path.getsize(path)):
                    self.canvas.load(path)
                    self.set_status("Captured — draw or run OCR")
                else:
                    self.set_status("Capture cancelled")
            finally:
                try:
                    if os.path.exists(path):
                        os.unlink(path)
                except OSError:
                    pass
            return
        self.set_status("Capture unavailable: portal denied the request")

    def capture_via_portal(self, interactive=True):
        """Capture via XDG Desktop Portal (works on GNOME Wayland).

        Returns (path, cancelled): path set on success, cancelled True
        when the user dismissed the system UI.
        """
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            proxy = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot", None)
            token = f"shuttermark_{os.getpid()}_{random.randint(0, 99999)}"
            opts = {
                "handle_token": GLib.Variant("s", token),
                "modal": GLib.Variant("b", True),
                "interactive": GLib.Variant("b", interactive),
            }
            res = proxy.call_sync(
                "Screenshot", GLib.Variant("(sa{sv})", ("", opts)),
                Gio.DBusCallFlags.NONE, -1, None)
            handle = res.unpack()[0]
            result = {}
            loop = GLib.MainLoop()

            def on_response(_conn, _sender, _path, _iface, _sig, params, _data):
                try:
                    response, results = params.unpack()
                    result["response"] = response
                    result["results"] = results or {}
                finally:
                    loop.quit()

            sub = bus.signal_subscribe(
                "org.freedesktop.portal.Desktop",
                "org.freedesktop.portal.Request", "Response",
                handle, None, Gio.DBusSignalFlags.NONE, on_response, None)
            try:
                GLib.timeout_add_seconds(
                    300 if interactive else 30, lambda: (loop.quit(), False)[1])
                loop.run()
            finally:
                bus.signal_unsubscribe(sub)
            if result.get("response") == 0:
                uri = (result.get("results") or {}).get("uri")
                if uri:
                    path, _host = GLib.filename_from_uri(uri)
                    if path and os.path.exists(path) and os.path.getsize(path):
                        return path, False
                return None, False
            if result.get("response") == 1:
                return None, True
            return None, False
        except GLib.Error as exc:
            print(f"Portal screenshot failed: {exc.message}")
        except Exception as exc:  # keep capture resilient
            print(f"Portal screenshot failed: {exc}")
        return None, False

    def capture_via_shell(self):
        """Use GNOME Shell's Wayland-native interactive screenshot."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            proxy = Gio.DBusProxy.new_sync(
                bus, Gio.DBusProxyFlags.NONE, None,
                "org.gnome.Shell", "/org/gnome/Shell/Screenshot",
                "org.gnome.Shell.Screenshot", None)
            # No input args; returns (success: b, uri: s). Blocks while the
            # user selects a region, like gnome-screenshot -a does.
            result = proxy.call_sync(
                "InteractiveScreenshot", None,
                Gio.DBusCallFlags.NONE, -1, None)
            success = result.get_child_value(0).get_boolean()
            uri = result.get_child_value(1).get_string()
            if success and uri:
                path, _host = GLib.filename_from_uri(uri)
                if path and os.path.exists(path) and os.path.getsize(path):
                    return path
        except GLib.Error as exc:
            print(f"Shell screenshot failed: {exc.message}")
        except Exception as exc:  # keep capture resilient
            print(f"Shell screenshot failed: {exc}")
        return None

    # -- file ----------------------------------------------------------
    def open_file(self, *_):
        dialog = Gtk.FileChooserNative(
            title="Open image", transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Open")
        filt = Gtk.FileFilter()
        filt.set_name("Images")
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            filt.add_pattern(pattern)
        dialog.set_filter(filt)
        dialog.connect("response", self._finish_open)
        dialog.show()

    def _finish_open(self, dialog, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                self.load_path(dialog.get_file().get_path())
        finally:
            dialog.destroy()

    # -- ocr -----------------------------------------------------------
    def ocr(self, *_):
        if not self.canvas.pixbuf:
            return
        if not shutil.which("tesseract"):
            self.set_status("OCR needs: sudo dnf install tesseract")
            return
        self.set_status("Running OCR…")
        fd, path = tempfile.mkstemp(suffix=".png", prefix="shuttermark-ocr-")
        os.close(fd)
        try:
            self.canvas.surface().write_to_png(path)
            result = subprocess.run(
                ["tesseract", path, "stdout", "tsv"],
                capture_output=True, text=True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        boxes = []
        if result.returncode == 0:
            for line in result.stdout.splitlines()[1:]:
                cells = line.split("\t")
                if len(cells) >= 12 and cells[11].strip():
                    try:
                        boxes.append((int(cells[6]), int(cells[7]),
                                      int(cells[8]), int(cells[9]),
                                      cells[11].strip()))
                    except ValueError:
                        pass
        self.canvas.ocr_boxes = boxes
        self.canvas.queue_draw()
        self.set_status(f"OCR found {len(boxes)} words")

    def undo(self, *_args):
        if self.canvas.marks:
            removed = self.canvas.marks.pop()
            if self.canvas.selected is removed:
                self.canvas.selected = None
            self.canvas.queue_draw()
            self.set_status("Undone")

    def clear(self):
        self.canvas.marks = []
        self.canvas.ocr_boxes = []
        self.canvas.selected = None
        self.canvas.queue_draw()
        self.set_status("Cleared")

    def _refresh_save_button(self):
        directory = default_save_dir()
        name = os.path.basename(directory) or directory
        if len(name) > 16:
            name = name[:7] + "…" + name[-8:]
        self.save_dir_btn.set_label(name)
        self.save_dir_btn.set_tooltip_text(f"Save location: {directory}")

    def choose_save_dir(self, *_):
        dialog = Gtk.FileChooserNative(
            title="Default save location", transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Choose")
        try:
            dialog.set_current_folder(Gio.File.new_for_path(default_save_dir()))
        except GLib.Error:
            pass
        dialog.connect("response", self._finish_save_dir)
        dialog.show()

    def _finish_save_dir(self, dialog, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                directory = dialog.get_file().get_path()
                if directory and os.path.isdir(directory):
                    save_save_dir(directory)
                    self._refresh_save_button()
                    self.set_status(f"Save location: {directory}")
        finally:
            dialog.destroy()

    def export(self, *_):
        if not self.canvas.pixbuf:
            return
        dialog = Gtk.FileChooserNative(
            title="Export annotated screenshot", transient_for=self,
            action=Gtk.FileChooserAction.SAVE, accept_label="Export")
        dialog.set_current_name("screenshot.png")
        try:
            dialog.set_current_folder(Gio.File.new_for_path(default_save_dir()))
        except GLib.Error:
            pass
        filt = Gtk.FileFilter()
        filt.set_name("PNG image")
        filt.add_pattern("*.png")
        dialog.set_filter(filt)
        dialog.connect("response", self.finish_export)
        dialog.show()

    def finish_export(self, dialog, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                path = dialog.get_file().get_path()
                if not path.lower().endswith(".png"):
                    path += ".png"
                try:
                    self.canvas.surface().write_to_png(path)
                    self._note_output(path)
                    save_save_dir(os.path.dirname(path))
                    self._refresh_save_button()
                    self.set_status(f"Saved {os.path.basename(path)}")
                except GLib.Error as exc:
                    self.set_status(f"Export failed: {exc.message}")
        finally:
            dialog.destroy()

    def copy(self, *_):
        if not self.canvas.pixbuf:
            return
        try:
            data = self.canvas.png_bytes()
            provider = Gdk.ContentProvider.new_for_bytes(
                "image/png", GLib.Bytes.new(data))
            self.get_clipboard().set_content(provider)
            self.set_status("Annotated image copied to clipboard")
        except GLib.Error as exc:
            self.set_status(f"Copy failed: {exc.message}")


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def _open_window(self):
        win = self.props.active_window
        if win is None:
            win = Shuttermark(self)
        win.present()
        return win

    def do_activate(self):
        self._open_window()

    def do_open(self, files, _n_files, _hint):
        win = self._open_window()
        for gfile in files:
            path = gfile.get_path()
            if path and os.path.isfile(path):
                if win.load_path(path):
                    break


if __name__ == "__main__":
    App().run(sys.argv)
