#!/usr/bin/env python3
"""victus-control tray: StatusNotifierItem for KDE Plasma.

Polls the daemon over the unix socket every ~3s, renders a half-circle
icon (left = CPU, right = GPU) colored by a continuous temperature
gradient, and flips the SNI Status property to NeedsAttention when temps
are sustained-hot for the current fan mode. No popups/notifications are
ever raised here -- Status is the only signal sent to the host.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib
from PIL import Image, ImageDraw

from common.protocol import request, ProtocolError

POLL_INTERVAL_S = 3
EMA_ALPHA = 0.3
SUSTAIN_HOLD_S = 5.0
GUI_BIN = "/usr/bin/victus-control-gui"

SNI_IFACE = "org.kde.StatusNotifierItem"
SNI_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
SNI_WATCHER_PATH = "/StatusNotifierWatcher"
SNI_OBJECT_PATH = "/StatusNotifierItem"
DBUSMENU_IFACE = "com.canonical.dbusmenu"
DBUSMENU_OBJECT_PATH = "/MenuBar"
QUIT_ITEM_ID = 1

# Gradient anchor points (temp_c -> RGB), piecewise-linear interpolation.
CPU_GRADIENT = [(40, (0, 200, 0)), (70, (230, 200, 0)), (95, (220, 0, 0))]
GPU_GRADIENT = [(40, (0, 200, 0)), (65, (230, 200, 0)), (90, (220, 0, 0))]
IDLE_GRAY = (110, 110, 110)


def _lerp(a, b, t):
    return a + (b - a) * t


def temp_to_color(temp_c, gradient):
    """Continuous piecewise-linear color gradient, clamped at the ends."""
    if temp_c <= gradient[0][0]:
        return gradient[0][1]
    if temp_c >= gradient[-1][0]:
        return gradient[-1][1]
    for (t0, c0), (t1, c1) in zip(gradient, gradient[1:]):
        if t0 <= temp_c <= t1:
            t = (temp_c - t0) / (t1 - t0)
            return tuple(round(_lerp(c0[i], c1[i], t)) for i in range(3))
    return gradient[-1][1]  # unreachable, but keeps type-checkers happy


def render_icon(cpu_temp, gpu_temp, gpu_idle, size=64):
    """Render the half-circle CPU/GPU status icon as an RGBA PIL Image.

    Left half = CPU (colored by CPU_GRADIENT), right half = GPU (colored
    by GPU_GRADIENT, or IDLE_GRAY if the GPU has no reading right now).
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cpu_color = temp_to_color(cpu_temp, CPU_GRADIENT) if cpu_temp is not None else IDLE_GRAY
    gpu_color = IDLE_GRAY if (gpu_idle or gpu_temp is None) else temp_to_color(gpu_temp, GPU_GRADIENT)

    bbox = (0, 0, size - 1, size - 1)
    # Left half: angles 90..270 (bottom-left around to top-left, i.e. the
    # left semicircle in PIL's clockwise-from-3-o'clock angle convention).
    draw.pieslice(bbox, 90, 270, fill=(*cpu_color, 255))
    # Right half: angles -90..90.
    draw.pieslice(bbox, 270, 360 + 90, fill=(*gpu_color, 255))

    return img


def pil_to_argb32(img):
    """Convert an RGBA PIL Image to the (width, height, ARGB32 bytes)
    struct format the SNI spec wants for IconPixmap. SNI's ARGB32 is
    network byte order (big-endian) 32-bit words: byte layout is
    A, R, G, B per pixel -- NOT the same as raw RGBA.
    """
    w, h = img.size
    rgba = img.tobytes("raw", "RGBA")
    argb = bytearray(len(rgba))
    argb[0::4] = rgba[3::4]  # A
    argb[1::4] = rgba[0::4]  # R
    argb[2::4] = rgba[1::4]  # G
    argb[3::4] = rgba[2::4]  # B
    return dbus.Struct(
        (dbus.Int32(w), dbus.Int32(h), dbus.ByteArray(bytes(argb))),
        signature="iiay",
    )


class Ema:
    """Light exponential moving average, used purely for icon-color
    smoothing -- never for attention-threshold decisions (those use the
    raw instantaneous reading, see StatusNotifierItemService._poll)."""

    def __init__(self, alpha=EMA_ALPHA):
        self.alpha = alpha
        self.value = None

    def update(self, sample):
        if sample is None:
            return self.value
        if self.value is None:
            self.value = float(sample)
        else:
            self.value = self.alpha * sample + (1 - self.alpha) * self.value
        return self.value


class QuitMenu(dbus.service.Object):
    """Minimal com.canonical.dbusmenu implementation: a single "Quit"
    entry. This is the standard way a right-click context menu gets
    rendered for an SNI item -- there's no separate "call Quit on the
    item" convention KDE honors, the menu *is* the quit path. Deliberately
    not a full dbusmenu implementation (no submenus, no icons, no
    checkable items) -- just enough for a working Quit.
    """

    def __init__(self, bus, object_path, on_quit):
        super().__init__(bus, object_path)
        self._on_quit = on_quit
        self._revision = 1

    @dbus.service.method(DBUSMENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, recursion_depth, property_names):
        quit_item = dbus.Struct(
            (dbus.Int32(QUIT_ITEM_ID),
             dbus.Dictionary({"label": "Quit", "enabled": True, "visible": True}, signature="sv"),
             dbus.Array([], signature="v")),
            signature="ia{sv}av",
        )
        root = dbus.Struct(
            (dbus.Int32(0),
             dbus.Dictionary({"children-display": "submenu"}, signature="sv"),
             dbus.Array([quit_item], signature="v")),
            signature="ia{sv}av",
        )
        return dbus.UInt32(self._revision), root

    @dbus.service.method(DBUSMENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, property_names):
        result = []
        for item_id in ids:
            if item_id == QUIT_ITEM_ID:
                props = dbus.Dictionary({"label": "Quit", "enabled": True, "visible": True}, signature="sv")
            else:
                props = dbus.Dictionary({}, signature="sv")
            result.append(dbus.Struct((dbus.Int32(item_id), props), signature="ia{sv}"))
        return result

    @dbus.service.method(DBUSMENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, item_id, name):
        return dbus.String("Quit") if (item_id == QUIT_ITEM_ID and name == "label") else dbus.String("")

    @dbus.service.method(DBUSMENU_IFACE, in_signature="isvu", out_signature="")
    def Event(self, item_id, event_id, data, timestamp):
        if item_id == QUIT_ITEM_ID and event_id == "clicked":
            self._on_quit()

    @dbus.service.method(DBUSMENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, item_id):
        return False

    @dbus.service.signal(DBUSMENU_IFACE, signature="a(ia{sv})as")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass

    @dbus.service.signal(DBUSMENU_IFACE, signature="u")
    def LayoutUpdated(self, revision):
        pass


class VictusTray(dbus.service.Object):
    def __init__(self, bus, object_path):
        super().__init__(bus, object_path)
        self._bus = bus

        self._cpu_ema = Ema()
        self._gpu_ema = Ema()

        self._fan_mode = "AUTO"
        self._fan1_rpm = None
        self._fan2_rpm = None
        self._raw_cpu_temp = None
        self._raw_gpu_temp = None
        self._gpu_idle = True

        self._cpu_attn_since = None
        self._gpu_attn_since = None
        self._cpu_sustained_hot = False
        self._gpu_sustained_hot = False

        # Always visible -- KDE's auto-hide is for Passive icons, and the
        # user wants this pinned regardless of temperature state. Status is
        # no longer used as a visibility signal; sustained-hot state only
        # affects tooltip text (see _build_tooltip).
        self._status = "Active"
        self._icon = render_icon(None, None, True)
        self._icon_pixmaps = [pil_to_argb32(self._icon)]

        self._warned_daemon_down = False

    # -- properties -----------------------------------------------------

    @dbus.service.method("org.freedesktop.DBus.Properties",
                          in_signature="ss", out_signature="v")
    def Get(self, interface_name, property_name):
        return self.GetAll(interface_name)[property_name]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                          in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface_name):
        if interface_name != SNI_IFACE:
            return {}
        return {
            "Category": "Hardware",
            "Id": "victus-control",
            "Title": "Victus Control",
            "Status": self._status,
            "WindowId": dbus.UInt32(0),
            "IconName": "",
            "IconPixmap": dbus.Array(self._icon_pixmaps, signature="(iiay)"),
            "OverlayIconName": "",
            "AttentionIconName": "",
            "ToolTip": self._build_tooltip(),
            "ItemIsMenu": False,
            "Menu": dbus.ObjectPath(DBUSMENU_OBJECT_PATH),
        }

    def _build_tooltip(self):
        def fmt(v, unit=""):
            return f"{v}{unit}" if v is not None else "?"

        cpu_s = fmt(self._raw_cpu_temp, "°C")
        gpu_s = "idle" if self._gpu_idle else fmt(self._raw_gpu_temp, "°C")
        f1_s = fmt(self._fan1_rpm, " RPM")
        f2_s = fmt(self._fan2_rpm, " RPM")
        text = (f"CPU {cpu_s} · GPU {gpu_s} · Fan1 {f1_s} · "
                f"Fan2 {f2_s} · Mode: {self._fan_mode}")
        if self._cpu_sustained_hot:
            text += " · CPU sustained hot"
        if self._gpu_sustained_hot:
            text += " · GPU sustained hot"
        return dbus.Struct(
            ("", dbus.Array([], signature="(iiay)"), "Victus Control", text),
            signature="sa(iiay)ss",
        )

    # -- methods ----------------------------------------------------------

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def Activate(self, x, y):
        try:
            subprocess.Popen([GUI_BIN])
        except OSError as e:
            print(f"victus-tray: failed to launch {GUI_BIN}: {e}", file=sys.stderr)

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def SecondaryActivate(self, x, y):
        pass

    @dbus.service.method(SNI_IFACE, in_signature="ii", out_signature="")
    def ContextMenu(self, x, y):
        # Real context menu is provided via the dbusmenu object at the
        # Menu property (see QuitMenu) -- KDE renders that directly and
        # doesn't normally call this method, so it's a no-op fallback.
        pass

    @dbus.service.method(SNI_IFACE, in_signature="", out_signature="")
    def Quit(self):
        GLib.idle_add(_quit_mainloop)

    # -- signals ------------------------------------------------------

    @dbus.service.signal(SNI_IFACE)
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewStatus(self, status):
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewToolTip(self):
        pass

    # -- polling / state update -----------------------------------------

    def _update_icon(self):
        self._icon = render_icon(self._cpu_ema.value, self._gpu_ema.value, self._gpu_idle)
        self._icon_pixmaps = [pil_to_argb32(self._icon)]
        self.NewIcon()

    def _fetch_state(self):
        """Fetch fresh state from the daemon. Each field is requested and
        degraded independently -- a hiccup on one call (daemon down,
        socket missing, connection refused) doesn't block the others.
        `request()` can raise either ProtocolError (protocol-level
        failure) or OSError (socket/connect failure, e.g. daemon not
        running yet), so both are caught here.
        """
        try:
            # Using package temp, not top-8 P-core mean, until the daemon
            # exposes per-core data.
            cpu_resp = request("GET_CPU_TEMP")
            self._raw_cpu_temp = cpu_resp.get("temp_c")
        except (OSError, ProtocolError):
            self._raw_cpu_temp = None

        try:
            gpu_resp = request("GET_GPU_TEMP")
            if gpu_resp.get("idle"):
                self._gpu_idle = True
                self._raw_gpu_temp = None
            else:
                self._gpu_idle = False
                self._raw_gpu_temp = gpu_resp.get("temp_c")
        except (OSError, ProtocolError):
            self._gpu_idle = True
            self._raw_gpu_temp = None

        try:
            mode_resp = request("GET_FAN_MODE")
            self._fan_mode = mode_resp.get("mode", self._fan_mode)
        except (OSError, ProtocolError):
            pass

        try:
            f1 = request("GET_FAN_SPEED", fan=1)
            self._fan1_rpm = f1.get("rpm")
        except (OSError, ProtocolError):
            self._fan1_rpm = None

        try:
            f2 = request("GET_FAN_SPEED", fan=2)
            self._fan2_rpm = f2.get("rpm")
        except (OSError, ProtocolError):
            self._fan2_rpm = None

    def _poll(self):
        now = time.monotonic()

        self._fetch_state()

        daemon_down = self._raw_cpu_temp is None and self._fan1_rpm is None and self._fan2_rpm is None
        if daemon_down:
            if not self._warned_daemon_down:
                print("victus-tray: daemon unreachable, will keep retrying", file=sys.stderr)
                self._warned_daemon_down = True
        elif self._warned_daemon_down:
            print("victus-tray: daemon connection restored", file=sys.stderr)
            self._warned_daemon_down = False

        # EMA for icon color only.
        self._cpu_ema.update(self._raw_cpu_temp)
        self._gpu_ema.update(None if self._gpu_idle else self._raw_gpu_temp)

        # Sustained-hot uses RAW readings with a 5s sustained-hold timer.
        # This no longer drives Status/visibility (icon is always Active) --
        # it only adds a tooltip note. See _build_tooltip.
        cpu_hot = False
        if self._raw_cpu_temp is not None:
            if self._fan_mode == "AUTO":
                cpu_hot = self._raw_cpu_temp >= 100
            elif self._fan_mode == "MANUAL":
                cpu_hot = self._raw_cpu_temp >= 90

        if cpu_hot:
            if self._cpu_attn_since is None:
                self._cpu_attn_since = now
        else:
            self._cpu_attn_since = None
        self._cpu_sustained_hot = (self._cpu_attn_since is not None
                                    and (now - self._cpu_attn_since) >= SUSTAIN_HOLD_S)

        gpu_hot = (self._fan_mode == "MANUAL"
                   and not self._gpu_idle
                   and self._raw_gpu_temp is not None
                   and self._raw_gpu_temp >= 75)

        if gpu_hot:
            if self._gpu_attn_since is None:
                self._gpu_attn_since = now
        else:
            self._gpu_attn_since = None
        self._gpu_sustained_hot = (self._gpu_attn_since is not None
                                    and (now - self._gpu_attn_since) >= SUSTAIN_HOLD_S)

        self._update_icon()
        self.NewToolTip()

        return True  # keep the GLib timeout alive


def _quit_mainloop():
    if _mainloop is not None:
        _mainloop.quit()
    return False


_mainloop = None


def register_with_watcher(bus, service_name):
    watcher = bus.get_object(SNI_WATCHER_NAME, SNI_WATCHER_PATH)
    watcher_iface = dbus.Interface(watcher, SNI_WATCHER_NAME)
    watcher_iface.RegisterStatusNotifierItem(service_name)


def main():
    global _mainloop

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()

    pid = os.getpid()
    service_name = f"org.kde.StatusNotifierItem-{pid}-1"
    bus_name = dbus.service.BusName(service_name, bus)

    tray = VictusTray(bus, SNI_OBJECT_PATH)
    QuitMenu(bus, DBUSMENU_OBJECT_PATH, on_quit=lambda: GLib.idle_add(_quit_mainloop))

    try:
        register_with_watcher(bus, service_name)
    except dbus.DBusException as e:
        print(f"victus-tray: failed to register with StatusNotifierWatcher: {e}",
              file=sys.stderr)
        return 1

    tray._poll()
    GLib.timeout_add_seconds(POLL_INTERVAL_S, tray._poll)

    _mainloop = GLib.MainLoop()
    try:
        _mainloop.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
