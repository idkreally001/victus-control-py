"""Shared constants and wire-protocol helpers for victus-control.

Daemon and clients (tray, GUI) talk newline-delimited JSON over a unix
socket. One JSON object per line, both directions.
"""

import json
import socket

SOCKET_PATH = "/run/victus-control/victus.sock"

HWMON_BASE = "/sys/devices/platform/hp-wmi/hwmon"

# Fallback max RPM per fan if fan{N}_max is unreadable. Asymmetric on
# purpose -- the two fans on this chassis have different physical maxes.
FAN_MAX_FALLBACK = {1: 5800, 2: 6100}

FAN_MODES = ("AUTO", "MANUAL", "MAX")
PWM_ENABLE_BY_MODE = {"AUTO": "2", "MANUAL": "1", "MAX": "0"}
MODE_BY_PWM_ENABLE = {v: k for k, v in PWM_ENABLE_BY_MODE.items()}

# Re-write pwm1_enable this often while in MANUAL/MAX so the EC doesn't
# silently revert control back to firmware AUTO ("weird HP behaviour",
# inherited quirk from the original C++ backend -- confirmed necessary,
# not cosmetic).
MODE_REASSERT_INTERVAL_S = 80.0


class ProtocolError(Exception):
    pass


def send_msg(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


def recv_msg(sock_file):
    line = sock_file.readline()
    if not line:
        raise ProtocolError("connection closed")
    return json.loads(line)


def connect():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(4.0)
    s.connect(SOCKET_PATH)
    return s


def request(cmd, **kwargs):
    """One-shot request/response over a fresh connection. Raises
    ProtocolError on any failure; returns the response dict on success.
    """
    s = connect()
    try:
        f = s.makefile("r")
        send_msg(s, {"cmd": cmd, **kwargs})
        resp = recv_msg(f)
        if not resp.get("ok", False):
            raise ProtocolError(resp.get("error", "unknown error"))
        return resp
    finally:
        s.close()
