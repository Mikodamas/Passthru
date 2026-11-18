#!/usr/bin/env python3
"""
Passthru - a minimal, low-latency capture card viewer for Linux.

* Shows the capture card's video with as little buffering as possible
  (newest frame always wins, old frames are dropped, no A/V sync waiting).
* Automatically plays the capture card's audio to your default output.
* Remembers your device / mode / volume choices.
* Reconnects automatically when the card is unplugged and plugged back in.

Keys:
  F11 / F / double-click   fullscreen          Esc   leave fullscreen
  M                        mute                H     show/hide toolbar
  I                        stats overlay (capture fps vs display fps, decoder, renderer)
  F5                       rescan devices
  Ctrl+Q                   quit

Options:
  --no-vsync   ask the GPU not to wait for vblank (may tear, can shave ~1 frame)

Requires (Fedora):
  sudo dnf install python3-pyqt6 python3-gobject gstreamer1-plugins-base \
                   gstreamer1-plugins-good pipewire-gstreamer
"""

import glob
import os
import re
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from PyQt6.QtCore import QRect, QRectF, QSettings, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtOpenGL import (  # noqa: E402
    QOpenGLPixelTransferOptions,
    QOpenGLTexture,
    QOpenGLTextureBlitter,
)
from PyQt6.QtGui import (  # noqa: E402
    QActionGroup,
    QColor,
    QFont,
    QImage,
    QKeySequence,
    QPainter,
    QShortcut,
    QSurfaceFormat,
)
from PyQt6.QtOpenGLWidgets import QOpenGLWidget  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QLabel,
    QMainWindow,
    QMenu,
    QSizePolicy,
    QSlider,
    QToolBar,
    QToolButton,
    QWidget,
)

APP_NAME = "Passthru"
APP_ID = "passthru"
NO_AUDIO = "(no audio)"

# Words that appear in lots of device names and say nothing about *which* device it is.
GENERIC_WORDS = {
    "usb", "video", "audio", "capture", "input", "analog", "stereo", "digital",
    "device", "camera", "mono", "source", "card", "hdmi", "the", "and", "mic",
    "microphone", "interface", "multichannel", "pro", "output",
}


# --------------------------------------------------------------------------- helpers

def make(factory, name=None, **props):
    el = Gst.ElementFactory.make(factory, name)
    if el is None:
        raise RuntimeError(
            f"GStreamer element '{factory}' is missing.\n"
            "Install: sudo dnf install gstreamer1-plugins-base gstreamer1-plugins-good"
        )
    for key, value in props.items():
        el.set_property(key.replace("_", "-"), value)
    return el


def has_prop(el, prop):
    return el is not None and el.find_property(prop) is not None


def set_if(el, prop, value):
    if has_prop(el, prop):
        el.set_property(prop, value)


class VideoMode:
    def __init__(self, media, fmt, w, h, num, den):
        self.media, self.fmt, self.w, self.h, self.num, self.den = media, fmt, w, h, num, den

    @property
    def fps(self):
        return self.num / self.den

    @property
    def is_jpeg(self):
        return self.media == "image/jpeg"

    def label(self):
        kind = "MJPEG" if self.is_jpeg else (self.fmt or "raw")
        fps = f"{self.fps:.2f}".rstrip("0").rstrip(".")
        return f"{self.w}×{self.h} @ {fps} fps  [{kind}]"

    def caps(self):
        common = f"width=(int){self.w},height=(int){self.h},framerate=(fraction){self.num}/{self.den}"
        if self.is_jpeg:
            return Gst.Caps.from_string(f"image/jpeg,{common}")
        return Gst.Caps.from_string(f"video/x-raw,format=(string){self.fmt},{common}")

    # Preferred raw formats when everything else ties (NV12 = least data to move around).
    RAW_PREFERENCE = {"NV12": 3, "YUY2": 2, "I420": 1}

    def sort_key(self):
        # 1. Smooth (50+ fps) modes first.
        # 2. Then uncompressed modes of at least 720p: no JPEG encode on the card and no
        #    decode on the PC = lowest latency and least CPU. (Tiny raw modes don't count,
        #    so USB 2 cards still default to a proper MJPEG mode.)
        # 3. Then biggest, then highest fps, then preferred raw format.
        good_raw = not self.is_jpeg and self.w * self.h >= 1280 * 720
        return (self.fps >= 49.9, good_raw, self.w * self.h, round(self.fps, 2),
                self.RAW_PREFERENCE.get(self.fmt, 0))


# Hardware MJPEG decoders to try, best first (VA-API: Intel/AMD, nvcodec: NVIDIA).
HW_JPEG_DECODERS = ("vajpegdec", "nvjpegdec")


_FRAMERATE_RE = re.compile(r"framerate=(?:\(fraction\))?\s*(\{[^}]*\}|\[[^\]]*\]|\d+/\d+)")


def parse_modes(caps):
    modes = {}
    if caps is None:
        return []
    for i in range(caps.get_size()):
        s = caps.get_structure(i)
        media = s.get_name()
        if media not in ("image/jpeg", "video/x-raw"):
            continue
        fmt = None
        if media == "video/x-raw":
            fmt = s.get_string("format")
            if not fmt:
                continue
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if not (ok_w and ok_h):
            continue
        m = _FRAMERATE_RE.search(s.to_string())
        if not m:
            continue
        rates = re.findall(r"(\d+)/(\d+)", m.group(1))
        if m.group(1).startswith("["):  # a range: just offer the maximum
            rates = rates[-1:]
        for n, d in rates:
            n, d = int(n), int(d)
            if n <= 0 or d <= 0:
                continue
            mode = VideoMode(media, fmt, w, h, n, d)
            modes[mode.label()] = mode
    return sorted(modes.values(), key=VideoMode.sort_key, reverse=True)


class Dev:
    """A capture source plus a friendly label."""

    def __init__(self, name, backend, factory):
        self.name = name
        self.backend = backend
        self._factory = factory
        self.key = f"{name}  [{backend}]"
        self.modes = []

    def create_element(self, name=None):
        return self._factory(name)

    @classmethod
    def from_gst(cls, device):
        el = device.create_element(None)
        factory = el.get_factory().get_name() if el is not None else "?"
        backend = factory[:-3] if factory.endswith("src") else factory
        return cls(device.get_display_name(), backend, device.create_element)


def probe_v4l2():
    """Fallback: open /dev/video* directly. Used when the device monitor can't see
    the cards (e.g. inside a Flatpak sandbox without access to the udev database)."""
    devs = []
    paths = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
    for path in paths:
        src = Gst.ElementFactory.make("v4l2src", None)
        if src is None:
            break
        src.set_property("device", path)
        try:
            if src.set_state(Gst.State.READY) == Gst.StateChangeReturn.FAILURE:
                continue
            caps = src.get_static_pad("src").query_caps(None)
            name = src.get_property("device-name") or path
            modes = parse_modes(caps)
        finally:
            src.set_state(Gst.State.NULL)
        if not modes:
            continue
        dev = Dev(f"{name} ({path})", "v4l2", lambda n, p=path: make("v4l2src", n, device=p))
        dev.modes = modes
        devs.append(dev)
    return devs


def device_base_name(name):
    """'Cam Link 4K: Cam Link 4K (/dev/video4)' and 'Cam Link 4K (V4L2)' -> 'cam link 4k'."""
    name = re.sub(r"\([^)]*\)", "", name)  # drop "(V4L2)", "(/dev/video4)", …
    name = name.split(":")[0]              # v4l2 names are often "Card: Card"
    return " ".join(name.lower().split())


def dedupe_video(devs):
    """The same card can show up via several backends (v4l2, PipeWire). Keep one entry
    per card: the one whose best mode is best, then the one with the most modes."""
    best = {}
    for d in devs:
        key = device_base_name(d.name)
        cur = best.get(key)
        if cur is None:
            best[key] = d
            continue
        score = (d.modes[0].sort_key(), len(d.modes))
        cur_score = (cur.modes[0].sort_key(), len(cur.modes))
        if score > cur_score:
            best[key] = d
    kept = set(map(id, best.values()))
    return [d for d in devs if id(d) in kept]  # keep the original ordering


def tokens(name):
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) >= 3 and t not in GENERIC_WORDS}


def guess_audio(video_name, audio_devs):
    """Pick the audio source whose name looks like the capture card's video name."""
    vt = tokens(video_name)
    best, best_score = None, 0
    for a in audio_devs:
        score = len(vt & tokens(a.name))
        if score > best_score:
            best, best_score = a, score
    return best


# --------------------------------------------------------------------------- video widget

class VideoWidget(QOpenGLWidget):
    """Shows the newest frame. Two renderers:
    * "gpu": uploads each frame into one reused OpenGL texture and draws it with
      QOpenGLTextureBlitter (fast, no per-frame allocations).
    * "painter": QPainter.drawImage (slower, but works everywhere)."""

    frame_ready = pyqtSignal()
    double_clicked = pyqtSignal()
    mouse_moved = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 180)
        self._lock = threading.Lock()
        self._pending = None
        self._current = None  # (data, w, h, stride) of the frame on screen
        self._image = None
        self._message = ""
        self.show_stats = False
        self.use_gpu_renderer = True
        self.decoder_label = ""
        self.mode_label = ""
        self._blitter = None
        self._tex = None
        self._tex_size = None
        self._tex_dirty = False
        self._is_gles = False
        self.parallel = False  # True when several decoder threads feed this widget
        self.reset_counters()
        self.frame_ready.connect(self.update, Qt.ConnectionType.QueuedConnection)

    def reset_counters(self):
        with self._lock:
            self.captured_total = 0
            self._cap_count = 0
            self._dec_count = 0
            self._last_pts = None
        self._disp_count = 0
        self._cap_fps = 0.0
        self._dec_fps = 0.0
        self._disp_fps = 0.0
        self._t0 = time.monotonic()

    def count_captured(self):  # called by the JPEG dispatcher thread
        with self._lock:
            self._cap_count += 1
            self.captured_total += 1

    # Called from a GStreamer streaming thread.
    def on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        s = sample.get_caps().get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        data = buf.extract_dup(0, buf.get_size())
        if not w or not h or len(data) < w * h * 4:
            return Gst.FlowReturn.OK
        pts = buf.pts
        with self._lock:
            # With several decoder threads a frame can finish after a newer one;
            # never step backwards in time.
            if pts != Gst.CLOCK_TIME_NONE:
                if self._last_pts is not None and pts <= self._last_pts:
                    return Gst.FlowReturn.OK
                self._last_pts = pts
            self._pending = (data, w, h, len(data) // h)
            self._dec_count += 1
            if not self.parallel:
                self._cap_count += 1
                self.captured_total += 1
        self.frame_ready.emit()
        return Gst.FlowReturn.OK

    def set_message(self, text, clear_image=True):
        self._message = text
        if clear_image:
            with self._lock:
                self._pending = None
            self._current = None
            self._image = None
        self.update()

    def _fit_rect(self, w, h):
        W, H = self.width(), self.height()
        scale = min(W / w, H / h)
        dw, dh = w * scale, h * scale
        return QRectF((W - dw) / 2, (H - dh) / 2, dw, dh)

    # ---------------------------------------------------------------- GL renderer
    def initializeGL(self):
        # Called again if the GL context is recreated (e.g. moving to another screen).
        self._blitter = None
        self._tex = None
        self._tex_size = None
        self._tex_dirty = self._current is not None
        ctx = self.context()
        self._is_gles = bool(ctx and ctx.isOpenGLES())

    def _gl_draw(self, data, w, h, stride):
        if self._blitter is None:
            blitter = QOpenGLTextureBlitter()
            if not blitter.create():
                raise RuntimeError("QOpenGLTextureBlitter.create() failed")
            # GLES can't upload BGRA directly: upload as RGBA and swap in the shader.
            blitter.setRedBlueSwizzle(self._is_gles)
            self._blitter = blitter

        if self._tex is None or self._tex_size != (w, h):
            if self._tex is not None:
                self._tex.destroy()
            tex = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            tex.setFormat(QOpenGLTexture.TextureFormat.RGBA8_UNorm)
            tex.setSize(w, h)
            tex.setMipLevels(1)
            tex.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
            tex.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            tex.allocateStorage()
            if not tex.isCreated():
                raise RuntimeError("could not create texture")
            self._tex = tex
            self._tex_size = (w, h)
            self._tex_dirty = True

        if self._tex_dirty:
            opts = QOpenGLPixelTransferOptions()
            opts.setAlignment(4)
            if stride != w * 4:
                opts.setRowLength(stride // 4)
            fmt = QOpenGLTexture.PixelFormat.RGBA if self._is_gles else QOpenGLTexture.PixelFormat.BGRA
            self._tex.setData(fmt, QOpenGLTexture.PixelType.UInt8, data, opts)
            self._tex_dirty = False

        dpr = self.devicePixelRatioF()
        r = self._fit_rect(w, h)
        target = QRectF(r.x() * dpr, r.y() * dpr, r.width() * dpr, r.height() * dpr)
        viewport = QRect(0, 0, round(self.width() * dpr), round(self.height() * dpr))
        matrix = QOpenGLTextureBlitter.targetTransform(target, viewport)
        self._blitter.bind()
        self._blitter.blit(self._tex.textureId(), matrix, QOpenGLTextureBlitter.Origin.OriginTopLeft)
        self._blitter.release()

    # ---------------------------------------------------------------- painting
    def paintGL(self):
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            self._current = pending
            self._image = None
            self._tex_dirty = True
            self._disp_count += 1

        now = time.monotonic()
        dt = now - self._t0
        if dt >= 1.0:
            with self._lock:
                cap, self._cap_count = self._cap_count, 0
                dec, self._dec_count = self._dec_count, 0
            self._cap_fps = cap / dt
            self._dec_fps = dec / dt
            self._disp_fps = self._disp_count / dt
            self._disp_count = 0
            self._t0 = now

        p = QPainter(self)
        p.fillRect(self.rect(), Qt.GlobalColor.black)
        if self._current is not None:
            data, w, h, stride = self._current
            drawn = False
            if self.use_gpu_renderer:
                p.beginNativePainting()
                try:
                    self._gl_draw(data, w, h, stride)
                    drawn = True
                except Exception as e:
                    print(f"GPU renderer failed, falling back to painter: {e}", file=sys.stderr)
                    self.use_gpu_renderer = False
                finally:
                    p.endNativePainting()
            if not drawn:
                if self._image is None:
                    self._image = QImage(data, w, h, stride, QImage.Format.Format_RGB32)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(self._fit_rect(w, h), self._image)
            if self.show_stats:
                self._draw_stats(p, w, h)
        elif self._message:
            p.setPen(QColor(220, 220, 220))
            p.setFont(QFont("sans-serif", 13))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
        p.end()

    def _draw_stats(self, p, w, h):
        lines = [
            f"{w}×{h}   {self.mode_label}",
            f"capture {self._cap_fps:5.1f}   decoded {self._dec_fps:5.1f}   display {self._disp_fps:5.1f} fps",
            f"decoder: {self.decoder_label or '-'}   renderer: {'OpenGL' if self.use_gpu_renderer else 'painter'}",
        ]
        p.setFont(QFont("monospace", 10))
        fm = p.fontMetrics()
        lh = fm.height()
        width = max(fm.horizontalAdvance(t) for t in lines) + 16
        box = QRectF(10, 10, width, lh * len(lines) + 10)
        p.fillRect(box, QColor(0, 0, 0, 180))
        p.setPen(Qt.GlobalColor.white)
        for i, t in enumerate(lines):
            p.drawText(QRectF(18, 15 + i * lh, width, lh), Qt.AlignmentFlag.AlignVCenter, t)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()

    def mouseMoveEvent(self, event):
        self.mouse_moved.emit()
        super().mouseMoveEvent(event)


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 780)
        self.settings = QSettings(APP_ID, APP_ID)

        self.video = VideoWidget(self)
        self.video.double_clicked.connect(self.toggle_fullscreen)
        self.video.mouse_moved.connect(self._on_mouse_activity)
        self.setCentralWidget(self.video)

        self.vpipe = None
        self.apipe = None
        self.volume_el = None
        self.video_devs = []
        self.audio_devs = []
        self._was_maximized = False
        # "cpu" (parallel, default) or "gpu" (experimental). Old "auto" setting -> cpu.
        self.decoder_pref = "gpu" if self.settings.value("decoder", "cpu", type=str) == "gpu" else "cpu"
        self.vworkers = []
        self._worker_srcs = []
        self._rr = 0
        self.video.use_gpu_renderer = self.settings.value("renderer", "gpu", type=str) == "gpu"

        self.monitor = Gst.DeviceMonitor.new()
        self.monitor.add_filter("Video/Source", None)
        self.monitor.add_filter("Audio/Source", None)
        self.monitor.start()

        self._build_toolbar()
        self._build_shortcuts()

        self.bus_timer = QTimer(self, interval=100, timeout=self._poll_buses)
        self.retry_timer = QTimer(self, singleShot=True, interval=2000, timeout=self._retry)
        self.hotplug_timer = QTimer(self, singleShot=True, interval=1500, timeout=self._on_hotplug)
        self.cursor_timer = QTimer(self, singleShot=True, interval=2000, timeout=self._hide_cursor)

        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        self._inhibit_cookie = None
        self._inhibit_screensaver()

        self.refresh_devices()
        self.bus_timer.start()

    # ---------------------------------------------------------------- UI
    def _build_toolbar(self):
        tb = QToolBar("Controls", self)
        tb.setMovable(False)
        tb.setFloatable(False)
        self.addToolBar(tb)
        self.toolbar = tb

        self.video_combo = QComboBox()
        self.mode_combo = QComboBox()
        self.audio_combo = QComboBox()
        for c in (self.video_combo, self.mode_combo, self.audio_combo):
            c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
            c.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        tb.addWidget(QLabel(" Video "))
        tb.addWidget(self.video_combo)
        tb.addWidget(QLabel("  Mode "))
        tb.addWidget(self.mode_combo)
        tb.addWidget(QLabel("  Audio "))
        tb.addWidget(self.audio_combo)

        refresh = QToolButton(text="⟳", toolTip="Rescan devices (F5)")
        refresh.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        refresh.clicked.connect(self.refresh_devices)
        tb.addWidget(refresh)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.mute_btn = QToolButton(checkable=True, toolTip="Mute (M)")
        self.mute_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.mute_btn.setChecked(self.settings.value("muted", False, type=bool))
        self.mute_btn.toggled.connect(self._apply_volume)
        tb.addWidget(self.mute_btn)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setFixedWidth(120)
        self.vol_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.vol_slider.setValue(self.settings.value("volume", 100, type=int))
        self.vol_slider.valueChanged.connect(self._apply_volume)
        tb.addWidget(self.vol_slider)

        tb.addWidget(self._build_settings_button())

        fs = QToolButton(text="⛶", toolTip="Fullscreen (F11 / double-click)")
        fs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        fs.clicked.connect(self.toggle_fullscreen)
        tb.addWidget(fs)

        self.video_combo.currentIndexChanged.connect(self._on_video_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_changed)
        self._update_mute_icon()

    def _build_settings_button(self):
        btn = QToolButton(text="⚙", toolTip="Settings")
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(btn)

        def radio_group(title, options, current, on_pick):
            sub = menu.addMenu(title)
            group = QActionGroup(self)
            group.setExclusive(True)
            for key, label in options:
                act = sub.addAction(label)
                act.setCheckable(True)
                act.setChecked(key == current)
                act.setData(key)
                group.addAction(act)
            group.triggered.connect(lambda a: on_pick(a.data()))
            return group

        self._dec_group = radio_group(
            "MJPEG decoding",
            [("cpu", "CPU, multi-threaded (recommended)"), ("gpu", "GPU (experimental)")],
            self.decoder_pref, self._set_decoder,
        )
        self._ren_group = radio_group(
            "Rendering",
            [("gpu", "OpenGL texture (fastest)"), ("painter", "Qt painter (compatibility)")],
            "gpu" if self.video.use_gpu_renderer else "painter", self._set_renderer,
        )
        menu.addSeparator()
        self.stats_action = menu.addAction("Show stats overlay\tI")
        self.stats_action.setCheckable(True)
        self.stats_action.toggled.connect(self._set_stats)
        btn.setMenu(menu)
        return btn

    def _set_decoder(self, key):
        self.decoder_pref = key
        self.settings.setValue("decoder", key)
        self.start_video()

    def _set_renderer(self, key):
        self.settings.setValue("renderer", key)
        self.video.use_gpu_renderer = key == "gpu"
        self.video.update()

    def _set_stats(self, on):
        self.video.show_stats = on
        self.video.update()

    def _build_shortcuts(self):
        def sc(keys, slot):
            for k in keys:
                QShortcut(QKeySequence(k), self, activated=slot)

        sc(["F11", "F"], self.toggle_fullscreen)
        sc(["Esc"], self._leave_fullscreen)
        sc(["M"], self.mute_btn.toggle)
        sc(["H"], lambda: self.toolbar.setVisible(not self.toolbar.isVisible()))
        sc(["I"], self.stats_action.toggle)
        sc(["F5"], self.refresh_devices)
        sc(["Ctrl+Q"], self.close)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self._leave_fullscreen()
        else:
            self._was_maximized = self.isMaximized()
            self.toolbar.hide()
            self.statusBar().hide()
            self.showFullScreen()
            self.cursor_timer.start()

    def _leave_fullscreen(self):
        if not self.isFullScreen():
            return
        self.cursor_timer.stop()
        self.video.unsetCursor()
        self.toolbar.show()
        self.statusBar().show()
        if self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def _on_mouse_activity(self):
        self.video.unsetCursor()
        if self.isFullScreen():
            self.cursor_timer.start()

    def _hide_cursor(self):
        if self.isFullScreen():
            self.video.setCursor(Qt.CursorShape.BlankCursor)

    # ---------------------------------------------------------------- devices
    def current_video_dev(self):
        i = self.video_combo.currentIndex()
        return self.video_devs[i] if 0 <= i < len(self.video_devs) else None

    def current_audio_dev(self):
        i = self.audio_combo.currentIndex() - 1  # index 0 is "(no audio)"
        return self.audio_devs[i] if 0 <= i < len(self.audio_devs) else None

    def _scan(self):
        video, audio = [], []
        for d in self.monitor.get_devices() or []:
            cls = d.get_device_class()
            try:
                if "Video/Source" in cls:
                    dev = Dev.from_gst(d)
                    dev.modes = parse_modes(d.get_caps())
                    if dev.modes:  # skips metadata-only /dev/videoN nodes
                        video.append(dev)
                elif "Audio/Source" in cls:
                    props = d.get_properties()
                    if props is not None and props.get_string("device.class") == "monitor":
                        continue
                    if d.get_display_name().lower().startswith("monitor of"):
                        continue
                    audio.append(Dev.from_gst(d))
            except Exception as e:  # one weird device shouldn't break the list
                print(f"Skipping device {d.get_display_name()!r}: {e}", file=sys.stderr)
        if not any(v.backend == "v4l2" for v in video):
            try:
                video += probe_v4l2()
            except Exception as e:
                print(f"Direct /dev/video probe failed: {e}", file=sys.stderr)
        video = dedupe_video(video)
        vorder = {"v4l2": 0, "pipewire": 1}
        aorder = {"pulse": 0, "pipewire": 1}
        video.sort(key=lambda x: (vorder.get(x.backend, 2), x.name.lower()))
        audio.sort(key=lambda x: (aorder.get(x.backend, 2), x.name.lower()))
        return video, audio

    def refresh_devices(self):
        self.stop_video()
        self.stop_audio()
        self.video_devs, self.audio_devs = self._scan()

        saved_v = self.settings.value("video_device", "", type=str)
        saved_a = self.settings.value("audio_device", "", type=str)

        for c in (self.video_combo, self.audio_combo):
            c.blockSignals(True)
            c.clear()

        for d in self.video_devs:
            self.video_combo.addItem(d.key)
        vi = self.video_combo.findText(saved_v)
        self.video_combo.setCurrentIndex(vi if vi >= 0 else 0)

        self.audio_combo.addItem(NO_AUDIO)
        for d in self.audio_devs:
            self.audio_combo.addItem(d.key)
        ai = self.audio_combo.findText(saved_a) if saved_a else -1
        if ai < 0:
            vdev = self.current_video_dev()
            guess = guess_audio(vdev.name, self.audio_devs) if vdev else None
            ai = self.audio_combo.findText(guess.key) if guess else 0
        self.audio_combo.setCurrentIndex(max(ai, 0))

        for c in (self.video_combo, self.audio_combo):
            c.blockSignals(False)

        self._populate_modes()

        if not self.video_devs:
            self.video.set_message(
                "No capture devices found.\n\nPlug in your capture card — it will be picked up automatically.\n"
                "(Or press F5 to rescan.)"
            )
            self.retry_timer.start()  # keep looking (hotplug events may not reach us in a sandbox)
        else:
            self.start_video()
        self.start_audio()

    def _populate_modes(self):
        dev = self.current_video_dev()
        self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        if dev:
            for m in dev.modes:
                self.mode_combo.addItem(m.label(), m)
            mi = self.mode_combo.findText(self.settings.value("video_mode_v3", "", type=str))
            self.mode_combo.setCurrentIndex(mi if mi >= 0 else 0)
        self.mode_combo.blockSignals(False)

    def _on_video_changed(self, _i):
        dev = self.current_video_dev()
        if dev:
            self.settings.setValue("video_device", dev.key)
            if self.audio_combo.currentIndex() == 0:
                guess = guess_audio(dev.name, self.audio_devs)
                if guess:
                    self.audio_combo.setCurrentIndex(self.audio_combo.findText(guess.key))
        self._populate_modes()
        self.start_video()

    def _on_mode_changed(self, _i):
        m = self.mode_combo.currentData()
        if m:
            self.settings.setValue("video_mode_v3", m.label())
        self.start_video()

    def _on_audio_changed(self, _i):
        self.settings.setValue("audio_device", self.audio_combo.currentText())
        self.start_audio()

    # ---------------------------------------------------------------- video pipeline
    #
    # MJPEG + CPU (default):  the card's JPEG frames are handed out round-robin to several
    #   small decoder pipelines, so several CPU cores decode at once. The display only
    #   accepts frames newer than the last one shown, so order is kept.
    #
    #   v4l2src → appsink ──┬→ appsrc → jpegdec → videoconvert → appsink ─┐
    #                       ├→ appsrc → jpegdec → videoconvert → appsink ─┼→ screen
    #                       └→ …                                          ─┘
    #
    # Raw (YUY2/NV12) modes, or MJPEG + GPU (experimental): one simple pipeline.

    @staticmethod
    def _leaky_queue():
        q = make("queue", max_size_buffers=1, max_size_bytes=0, max_size_time=0)
        Gst.util_set_object_arg(q, "leaky", "downstream")
        return q

    @staticmethod
    def _bgrx_appsink(callback):
        sink = make("appsink", emit_signals=True, sync=False, max_buffers=1, drop=True)
        set_if(sink, "qos", False)
        sink.connect("new-sample", callback)
        return sink

    @staticmethod
    def _link_all(pipe, chain):
        for el in chain:
            pipe.add(el)
        for a, b in zip(chain, chain[1:]):
            if not a.link(b):
                raise RuntimeError(f"could not link {a.get_name()} → {b.get_name()}")

    def _decoder_threads(self):
        return max(2, min(4, (os.cpu_count() or 4) // 2))

    def start_video(self):
        self.stop_video()
        dev = self.current_video_dev()
        mode = self.mode_combo.currentData()
        if not dev or not mode:
            return
        self.video.mode_label = mode.label()
        self.video.reset_counters()
        self.video.set_message("Waiting for signal…")
        try:
            if mode.is_jpeg and self.decoder_pref != "gpu":
                self._start_parallel_jpeg(dev, mode)
            else:
                self._start_simple(dev, mode)
            self.setWindowTitle(f"{APP_NAME} — {dev.name}")
        except Exception as e:
            self.stop_video()
            self.video.set_message(f"Could not start video:\n{e}")

    def _start_simple(self, dev, mode):
        self.video.parallel = False
        pipe = Gst.Pipeline.new("video")
        src = dev.create_element("vsrc")
        set_if(src, "do-timestamp", True)
        chain = [src, make("capsfilter", caps=mode.caps()), self._leaky_queue()]
        label = "none (raw)"
        if mode.is_jpeg:  # GPU decoding (experimental)
            dec = None
            for factory in HW_JPEG_DECODERS:
                dec = Gst.ElementFactory.make(factory, None)
                if dec is not None:
                    label = f"{factory} (GPU, experimental)"
                    break
            if dec is None:
                raise RuntimeError("no GPU MJPEG decoder found (vajpegdec / nvjpegdec).\n"
                                   "Switch to CPU in ⚙ → MJPEG decoding.")
            set_if(dec, "max-errors", -1)
            chain.append(dec)
            # JPEG colours are always full range (0-255), but GPU decoders often label their
            # output as limited range (16-235). videoconvert then "expands" it a second time,
            # which crushes blacks and blows out whites (the boosted-contrast look).
            # Re-label it correctly: full range, BT.601 matrix — what jpegdec reports.
            fix = Gst.ElementFactory.make("capssetter", None)
            if fix is not None:
                fix.set_property("caps", Gst.Caps.from_string(
                    "video/x-raw,colorimetry=(string)1:4:0:0"))
                fix.set_property("join", True)
                chain.append(fix)
            chain.append(self._leaky_queue())
        conv = make("videoconvert")
        set_if(conv, "n-threads", 0)
        chain += [conv,
                  make("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=BGRx")),
                  self._bgrx_appsink(self.video.on_sample)]
        self._link_all(pipe, chain)
        self.video.decoder_label = label
        self.vpipe = pipe
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("the device refused to start.\nIs OBS or another program using it?")

    def _start_parallel_jpeg(self, dev, mode):
        self.video.parallel = True
        n = self._decoder_threads()

        # Decoder workers first, so they're ready when frames arrive.
        srcs = []
        for i in range(n):
            wp = Gst.Pipeline.new(f"decoder{i}")
            asrc = make("appsrc", caps=mode.caps(), is_live=True, do_timestamp=False)
            Gst.util_set_object_arg(asrc, "format", "time")
            if has_prop(asrc, "leaky-type"):  # GStreamer ≥ 1.20: hold ≤2 frames, drop the oldest
                set_if(asrc, "max-buffers", 2)
                set_if(asrc, "max-bytes", 0)
                Gst.util_set_object_arg(asrc, "leaky-type", "downstream")
            dec = make("jpegdec")
            set_if(dec, "max-errors", -1)
            conv = make("videoconvert")
            set_if(conv, "n-threads", 1)
            chain = [asrc, dec, conv,
                     make("capsfilter", caps=Gst.Caps.from_string("video/x-raw,format=BGRx")),
                     self._bgrx_appsink(self.video.on_sample)]
            self._link_all(wp, chain)
            self.vworkers.append(wp)
            if wp.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("could not start a decoder pipeline")
            srcs.append(asrc)
        self._worker_srcs = srcs
        self._rr = 0

        # Capture pipeline: just grabs the compressed frames.
        pipe = Gst.Pipeline.new("video")
        src = dev.create_element("vsrc")
        set_if(src, "do-timestamp", True)
        sink = make("appsink", emit_signals=True, sync=False, max_buffers=1, drop=True)
        set_if(sink, "qos", False)
        sink.connect("new-sample", self._dispatch_jpeg)
        self._link_all(pipe, [src, make("capsfilter", caps=mode.caps()), sink])
        self.video.decoder_label = f"jpegdec (CPU) × {n} threads"
        self.vpipe = pipe
        if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("the device refused to start.\nIs OBS or another program using it?")

    # Called from the capture pipeline's streaming thread.
    def _dispatch_jpeg(self, sink):
        sample = sink.emit("pull-sample")
        srcs = self._worker_srcs
        if sample is None or not srcs:
            return Gst.FlowReturn.OK
        self._rr = (self._rr + 1) % len(srcs)
        self.video.count_captured()
        srcs[self._rr].emit("push-buffer", sample.get_buffer())
        return Gst.FlowReturn.OK

    def stop_video(self):
        self._worker_srcs = []
        if self.vpipe is not None:
            self.vpipe.set_state(Gst.State.NULL)
            self.vpipe = None
        for wp in self.vworkers:
            wp.set_state(Gst.State.NULL)
        self.vworkers = []

    # ---------------------------------------------------------------- audio pipeline
    def start_audio(self):
        self.stop_audio()
        dev = self.current_audio_dev()
        if not dev:
            return
        pipe = Gst.Pipeline.new("audio")
        try:
            src = dev.create_element("asrc")
            # Small buffers = low delay. Values are in microseconds.
            set_if(src, "buffer-time", 20000)
            set_if(src, "latency-time", 5000)
            set_if(src, "client-name", APP_NAME)

            vol = make("volume")
            sink = Gst.ElementFactory.make("pulsesink", None) or make("autoaudiosink")
            set_if(sink, "buffer-time", 40000)
            set_if(sink, "latency-time", 10000)
            set_if(sink, "client-name", APP_NAME)

            chain = [src, make("audioconvert"), make("audioresample"), vol, sink]
            for el in chain:
                pipe.add(el)
            for a, b in zip(chain, chain[1:]):
                if not a.link(b):
                    raise RuntimeError(f"could not link {a.get_name()} → {b.get_name()}")

            self.volume_el = vol
            self._apply_volume()
            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("the audio device refused to start")
            self.apipe = pipe
        except Exception as e:
            pipe.set_state(Gst.State.NULL)
            self.volume_el = None
            self.statusBar().showMessage(f"Audio error: {e}", 10000)

    def stop_audio(self):
        if self.apipe is not None:
            self.apipe.set_state(Gst.State.NULL)
            self.apipe = None
        self.volume_el = None

    def _apply_volume(self, *_):
        self.settings.setValue("volume", self.vol_slider.value())
        self.settings.setValue("muted", self.mute_btn.isChecked())
        self._update_mute_icon()
        if self.volume_el is not None:
            self.volume_el.set_property("volume", self.vol_slider.value() / 100.0)
            self.volume_el.set_property("mute", self.mute_btn.isChecked())

    def _update_mute_icon(self):
        self.mute_btn.setText("🔇" if self.mute_btn.isChecked() else "🔊")

    # ---------------------------------------------------------------- bus / errors / hotplug
    def _poll_buses(self):
        mbus = self.monitor.get_bus()
        while True:
            msg = mbus.pop_filtered(Gst.MessageType.DEVICE_ADDED | Gst.MessageType.DEVICE_REMOVED)
            if msg is None:
                break
            self.hotplug_timer.start()  # debounce: video + audio usually appear together

        wanted = Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS
        pipes = [("video", p) for p in [self.vpipe, *self.vworkers]] + [("audio", self.apipe)]
        failed = None
        for kind, pipe in pipes:
            if pipe is None:
                continue
            bus = pipe.get_bus()
            while failed is None:
                msg = bus.pop_filtered(wanted)
                if msg is None:
                    break
                if msg.type == Gst.MessageType.ERROR:
                    err, _dbg = msg.parse_error()
                    failed = (kind, err.message)
                elif msg.type == Gst.MessageType.EOS:
                    failed = (kind, "stream ended")
                else:
                    warn, _dbg = msg.parse_warning()
                    self.statusBar().showMessage(f"{kind}: {warn.message}", 5000)
            if failed:
                break
        if failed:
            self._on_pipeline_error(*failed)

    def _on_pipeline_error(self, kind, text):
        if kind == "video":
            self.stop_video()
            self.video.set_message(f"Video stopped: {text}\n\nRetrying…")
        else:
            self.stop_audio()
            self.statusBar().showMessage(f"Audio stopped: {text} — retrying…", 5000)
        self.retry_timer.start()

    def _retry(self):
        if self.vpipe is None or (self.apipe is None and self.current_audio_dev()):
            self.refresh_devices()

    def _on_hotplug(self):
        # Only rescan if we're not happily showing video (avoids interrupting playback).
        if self.vpipe is None or (self.apipe is None and self.audio_combo.currentIndex() == 0):
            self.refresh_devices()

    # ---------------------------------------------------------------- screensaver
    def _inhibit_screensaver(self):
        try:
            from PyQt6.QtDBus import QDBusConnection, QDBusInterface

            self._ss = QDBusInterface(
                "org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver", QDBusConnection.sessionBus(),
            )
            reply = self._ss.call("Inhibit", APP_NAME, "Watching capture card")
            args = reply.arguments()
            self._inhibit_cookie = args[0] if args else None
        except Exception:
            self._inhibit_cookie = None

    def _uninhibit_screensaver(self):
        if self._inhibit_cookie is not None:
            try:
                self._ss.call("UnInhibit", self._inhibit_cookie)
            except Exception:
                pass

    # ---------------------------------------------------------------- shutdown
    def closeEvent(self, event):
        if self.isFullScreen():
            self._leave_fullscreen()
        self.settings.setValue("geometry", self.saveGeometry())
        self.bus_timer.stop()
        self.stop_video()
        self.stop_audio()
        self.monitor.stop()
        self._uninhibit_screensaver()
        super().closeEvent(event)


def main():
    Gst.init(None)

    if "--no-vsync" in sys.argv:
        fmt = QSurfaceFormat.defaultFormat()
        fmt.setSwapInterval(0)
        QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(os.environ.get("FLATPAK_ID", APP_ID))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
