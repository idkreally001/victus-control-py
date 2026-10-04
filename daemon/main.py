#!/usr/bin/env python3
"""victus-control daemon: root-owned fan control over a local unix socket.

AUTO does not mean firmware AUTO (pwm1_enable=2). Firmware AUTO was
measured twice on this hardware to leave fans at 0 RPM through 83-97C
package temp under real load -- it does not respond to Linux-visible
thermal state. "AUTO" here means: run auto_curve's software curve on top
of hardware MANUAL mode, the same approach the original victus-control
project used under the name "Better Auto" (and made its actual default).

Runs as root; talks to the desktop session only via the socket.
"""

import json
import os
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.protocol import SOCKET_PATH, FAN_MODES, PWM_ENABLE_BY_MODE, MODE_REASSERT_INTERVAL_S
from daemon import hardware
from daemon import auto_curve

_state_lock = threading.Lock()
# Placeholder only -- main() calls cmd_set_fan_mode(AUTO) immediately after
# the socket is bound, which starts the curve worker and sets this for
# real. Claiming AUTO here before that thread actually exists would be a
# lie (nothing driving the fans yet), so start conservatively; the window
# is brief (no client can connect until the socket is listening anyway).
_current_mode = "MAX"
_running = True

_auto_thread = None
_auto_stop_event = threading.Event()


def _hwmon_or_none():
    return hardware.find_hwmon_dir()


def cmd_get_cpu_temp(_args):
    temp = hardware.read_cpu_package_temp()
    if temp is None:
        return {"ok": False, "error": "CPU temperature unavailable"}
    return {"ok": True, "temp_c": round(temp)}


def cmd_get_gpu_temp(_args):
    if hardware.has_nvidia_gpu():
        temp, _usage = hardware.read_nvidia_gpu_cached()
        if temp is None:
            # Suspended dGPU or nvidia-smi hiccup -- not an error, just no
            # reading right now (matches old backend's "IDLE" semantics).
            return {"ok": True, "idle": True}
        return {"ok": True, "temp_c": round(temp)}
    return {"ok": False, "error": "GPU temperature unavailable"}

def cmd_get_fan_speed(args):
    fan_num = args.get("fan")
    if fan_num not in (1, 2):
        return {"ok": False, "error": "invalid fan number"}
    hwmon_dir = _hwmon_or_none()
    if hwmon_dir is None:
        return {"ok": False, "error": "hwmon directory not found"}
    rpm = hardware.read_fan_rpm(hwmon_dir, fan_num)
    if rpm is None:
        return {"ok": False, "error": "unable to read fan speed"}
    return {"ok": True, "rpm": rpm}


def cmd_get_fan_max(args):
    fan_num = args.get("fan")
    if fan_num not in (1, 2):
        return {"ok": False, "error": "invalid fan number"}
    hwmon_dir = _hwmon_or_none()
    if hwmon_dir is None:
        return {"ok": False, "error": "hwmon directory not found"}
    return {"ok": True, "rpm": hardware.read_fan_max(hwmon_dir, fan_num)}


def cmd_get_fan_mode(_args):
    with _state_lock:
        mode = _current_mode
    return {"ok": True, "mode": mode}


def _stop_auto_worker():
    """Stop and join the software-AUTO curve thread if running. Must be
    called before any code path that writes fan mode/speed outside the
    curve worker itself -- otherwise the worker's next tick overwrites
    whatever was just set (the "double writer" bug class already hit once
    in the GUI, at the daemon layer this time)."""
    global _auto_thread
    _auto_stop_event.set()
    if _auto_thread is not None:
        _auto_thread.join(timeout=5.0)
    _auto_thread = None


def cmd_set_fan_mode(args):
    global _current_mode, _auto_thread
    mode = str(args.get("mode", "")).upper()
    if mode not in FAN_MODES:
        return {"ok": False, "error": f"invalid fan mode: {mode}"}
    hwmon_dir = _hwmon_or_none()
    if hwmon_dir is None:
        return {"ok": False, "error": "hwmon directory not found"}

    _stop_auto_worker()

    if mode == "AUTO":
        # Software AUTO rides on hardware MANUAL -- firmware AUTO
        # (pwm1_enable=2) does not respond to real thermal state on this
        # hardware (measured twice: fans stayed at 0 through 83-97C
        # package temp under load).
        try:
            hardware.write_pwm_enable(hwmon_dir, PWM_ENABLE_BY_MODE["MANUAL"])
        except OSError as e:
            return {"ok": False, "error": f"failed to set fan mode: {e}"}
        with _state_lock:
            _current_mode = "AUTO"
        _auto_stop_event.clear()
        _auto_thread = threading.Thread(target=auto_curve_worker, daemon=True)
        _auto_thread.start()
        return {"ok": True}

    try:
        hardware.write_pwm_enable(hwmon_dir, PWM_ENABLE_BY_MODE[mode])
    except OSError as e:
        return {"ok": False, "error": f"failed to set fan mode: {e}"}
    with _state_lock:
        _current_mode = mode
    return {"ok": True}


def cmd_set_fan_speed(args):
    global _current_mode
    fan_num = args.get("fan")
    rpm = args.get("rpm")
    if fan_num not in (1, 2) or not isinstance(rpm, int) or rpm < 0:
        return {"ok": False, "error": "invalid fan number or speed"}
    hwmon_dir = _hwmon_or_none()
    if hwmon_dir is None:
        return {"ok": False, "error": "hwmon directory not found"}
    # A manual speed set is an explicit user override -- stop the curve so
    # it doesn't overwrite this target on its next tick.
    _stop_auto_worker()
    max_rpm = hardware.read_fan_max(hwmon_dir, fan_num)
    clamped = min(rpm, max_rpm)
    try:
        hardware.write_fan_target(hwmon_dir, fan_num, clamped)
    except OSError as e:
        return {"ok": False, "error": f"failed to set fan speed: {e}"}
    with _state_lock:
        _current_mode = "MANUAL"  # write_fan_target always forces MANUAL
    return {"ok": True, "rpm": clamped}


def auto_curve_worker():
    """Software AUTO's control loop. Runs entirely against the hardware
    layer directly -- never through cmd_set_fan_speed/cmd_set_fan_mode,
    which have _current_mode side effects that would fight this loop's own
    mode bookkeeping (cmd_set_fan_mode already set _current_mode="AUTO"
    once, before this thread started; nothing in here should touch it
    again except the finally-block failsafe on exit).
    """
    global _current_mode
    state = auto_curve.AutoCurveState()
    consecutive_sensor_failures = 0

    try:
        while not _auto_stop_event.is_set():
            now = time.monotonic()
            hwmon_dir = _hwmon_or_none()

            p_cores = hardware.read_p_core_temps()
            cpu_temp = sum(p_cores) / len(p_cores) if p_cores else None
            gpu_temp = None
            if hardware.has_nvidia_gpu():
                gpu_temp, _usage = hardware.read_nvidia_gpu_cached()

            if cpu_temp is None:
                consecutive_sensor_failures += 1
            else:
                consecutive_sensor_failures = 0

            if consecutive_sensor_failures >= 3:
                # Can't see temperature at all -- bias to max rather than
                # hold a stale (possibly too-low) level indefinitely.
                level = auto_curve.MAX_LEVEL
            else:
                level = state.compute_target_level(cpu_temp, gpu_temp)

            if hwmon_dir is not None:
                if state.should_reassert_mode(now):
                    # Independent of RPM apply below and on its own
                    # cadence -- this is what stops the EC silently
                    # reverting to firmware AUTO (fans-to-0) during a long
                    # steady-state period where the level never changes.
                    try:
                        hardware.write_pwm_enable(hwmon_dir, PWM_ENABLE_BY_MODE["MANUAL"])
                    except OSError:
                        pass
                    state.mark_mode_asserted(now)

                if state.should_reapply(now, level):
                    max1 = hardware.read_fan_max(hwmon_dir, 1)
                    max2 = hardware.read_fan_max(hwmon_dir, 2)
                    try:
                        hardware.write_fan_target(hwmon_dir, 1, auto_curve.rpm_for_level(level, max1))
                        hardware.write_fan_target(hwmon_dir, 2, auto_curve.rpm_for_level(level, max2))
                    except OSError:
                        pass
                    state.mark_applied(now, level)

            _auto_stop_event.wait(auto_curve.TICK_INTERVAL_S)
    finally:
        # A deliberate stop (_stop_auto_worker, called from cmd_set_fan_mode
        # or cmd_set_fan_speed before the caller writes the user's actual
        # requested mode/speed) sets the stop event as the *intended* exit
        # signal -- nothing further needed here, the caller has it handled.
        # Anything else (an unhandled exception in the loop body) leaves
        # the stop event unset, which is the crash case: fans would be
        # frozen at a stale low target with nothing driving them, so force
        # MAX -- the only safe fallback, since firmware AUTO does not cool
        # on this hardware.
        if not _auto_stop_event.is_set():
            hwmon_dir = _hwmon_or_none()
            if hwmon_dir is not None:
                try:
                    hardware.write_pwm_enable(hwmon_dir, PWM_ENABLE_BY_MODE["MAX"])
                except OSError:
                    pass
            with _state_lock:
                _current_mode = "MAX"


COMMANDS = {
    "GET_CPU_TEMP": cmd_get_cpu_temp,
    "GET_GPU_TEMP": cmd_get_gpu_temp,
    "GET_FAN_SPEED": cmd_get_fan_speed,
    "GET_FAN_MAX_SPEED": cmd_get_fan_max,
    "GET_FAN_MODE": cmd_get_fan_mode,
    "SET_FAN_MODE": cmd_set_fan_mode,
    "SET_FAN_SPEED": cmd_set_fan_speed,
}


def handle_client(conn):
    f = conn.makefile("r")
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                conn.sendall((json.dumps({"ok": False, "error": "bad json"}) + "\n").encode())
                continue
            cmd = msg.get("cmd")
            handler = COMMANDS.get(cmd)
            if handler is None:
                resp = {"ok": False, "error": "unknown command"}
            else:
                resp = handler(msg)
            conn.sendall((json.dumps(resp) + "\n").encode())
    except (OSError, ConnectionError):
        pass
    finally:
        conn.close()


def mode_reassert_loop():
    """The EC silently reverts fan control if pwm1_enable isn't periodically
    rewritten -- ported quirk from the original backend, confirmed load-
    bearing (not cosmetic): without this, fan control just stops working
    with no error surfaced anywhere.
    """
    while _running:
        time.sleep(MODE_REASSERT_INTERVAL_S)
        with _state_lock:
            mode = _current_mode
        if mode == "AUTO":
            continue
        hwmon_dir = _hwmon_or_none()
        if hwmon_dir is None:
            continue
        try:
            hardware.write_pwm_enable(hwmon_dir, PWM_ENABLE_BY_MODE[mode])
        except OSError:
            pass


def main():
    global _running

    if os.geteuid() != 0:
        print("victus-daemon: must run as root", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    # Group-own by "victus" so desktop-session clients (tray/GUI, running as
    # the normal user) can connect without needing root -- same group the
    # old backend's socket used.
    try:
        import grp
        victus_gid = grp.getgrnam("victus").gr_gid
        os.chown(SOCKET_PATH, 0, victus_gid)
    except KeyError:
        print("victus-daemon: 'victus' group not found; socket left root-only",
              file=sys.stderr)
    server.listen(5)

    def shutdown(_signum, _frame):
        global _running
        _running = False
        server.close()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=mode_reassert_loop, daemon=True).start()

    # Start in software AUTO, not MAX -- the curve is validated now (was
    # not, when MAX-at-startup was first chosen as the safe default before
    # this module existed). AUTO is quiet at idle (~2500 RPM measured) and
    # responsive under load (measured ramping under real load in testing),
    # strictly better than a loud MAX or a guessed-at fixed MANUAL speed.
    cmd_set_fan_mode({"mode": "AUTO"})

    print("victus-daemon: listening", file=sys.stderr)
    while _running:
        try:
            conn, _addr = server.accept()
        except OSError:
            if not _running:
                break
            continue
        threading.Thread(target=handle_client, args=(conn,), daemon=True).start()

    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
