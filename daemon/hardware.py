"""Direct hardware access: hwmon sysfs, coretemp, nvidia-smi.

Runs as root. No sudo indirection -- the daemon itself is the privileged
process, so every write here is direct.
"""

import glob
import os
import subprocess
import time

from common.protocol import HWMON_BASE, FAN_MAX_FALLBACK


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_hwmon_dir():
    """Highest-numbered hwmonN under HWMON_BASE. Must agree with the old
    C++/shell selection (both used "highest N wins") or fan writes can land
    on a stale/wrong hwmon instance after a reboot renumbers them.
    """
    try:
        entries = os.listdir(HWMON_BASE)
    except OSError:
        return None
    numbered = []
    for name in entries:
        if name.startswith("hwmon") and len(name) > 5:
            try:
                numbered.append((int(name[5:]), name))
            except ValueError:
                continue
    if not numbered:
        return None
    numbered.sort()
    return os.path.join(HWMON_BASE, numbered[-1][1])


def read_fan_rpm(hwmon_dir, fan_num):
    val = _read(os.path.join(hwmon_dir, f"fan{fan_num}_input"))
    return int(val) if val is not None else None


def read_fan_max(hwmon_dir, fan_num):
    val = _read(os.path.join(hwmon_dir, f"fan{fan_num}_max"))
    if val is not None:
        try:
            n = int(val)
            if n > 0:
                return n
        except ValueError:
            pass
    return FAN_MAX_FALLBACK[fan_num]


def read_pwm_enable(hwmon_dir):
    return _read(os.path.join(hwmon_dir, "pwm1_enable"))


def write_pwm_enable(hwmon_dir, value):
    with open(os.path.join(hwmon_dir, "pwm1_enable"), "w") as f:
        f.write(value)


def write_fan_target(hwmon_dir, fan_num, rpm):
    # Every speed write also forces MANUAL -- ported deliberately from the
    # old set-fan-speed.sh behaviour: setting a speed only makes sense in
    # MANUAL, and the EC ignores fan{N}_target outside it anyway.
    write_pwm_enable(hwmon_dir, "1")
    with open(os.path.join(hwmon_dir, f"fan{fan_num}_target"), "w") as f:
        f.write(str(rpm))


def _find_coretemp_dir():
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        if _read(os.path.join(hwmon, "name")) == "coretemp":
            return hwmon
    return None


# Physical-core labels for the 8 P-cores on the i7-14700HX (8P+12E). E-core
# labels (32-43) are deliberately excluded -- they run cooler under
# background/light load and would dilute a "how hot is real work" signal.
P_CORE_LABELS = {"Core 0", "Core 4", "Core 8", "Core 12",
                  "Core 16", "Core 20", "Core 24", "Core 28"}


def read_p_core_temps():
    """Returns a list of P-core temps in °C, or [] if coretemp unavailable."""
    coretemp_dir = _find_coretemp_dir()
    if coretemp_dir is None:
        return []
    temps = []
    for label_path in glob.glob(os.path.join(coretemp_dir, "temp*_label")):
        label = _read(label_path)
        if label not in P_CORE_LABELS:
            continue
        input_path = label_path.replace("_label", "_input")
        val = _read(input_path)
        if val is not None:
            try:
                temps.append(int(val) / 1000.0)
            except ValueError:
                pass
    return temps


def read_cpu_package_temp():
    coretemp_dir = _find_coretemp_dir()
    if coretemp_dir is None:
        return None
    for label_path in glob.glob(os.path.join(coretemp_dir, "temp*_label")):
        if _read(label_path) == "Package id 0":
            val = _read(label_path.replace("_label", "_input"))
            if val is not None:
                try:
                    return int(val) / 1000.0
                except ValueError:
                    return None
    return None


def _find_nvidia_pci_dir():
    for dev in glob.glob("/sys/bus/pci/devices/*"):
        cls = _read(os.path.join(dev, "class"))
        if not cls or not cls.startswith("0x03"):
            continue
        vendor = _read(os.path.join(dev, "vendor"))
        if vendor and "0x10de" in vendor.lower():
            return dev
    return None


_nvidia_pci_dir = "unset"  # sentinel: probed lazily, cached after first call


def _nvidia_pci_dir_cached():
    global _nvidia_pci_dir
    if _nvidia_pci_dir == "unset":
        _nvidia_pci_dir = _find_nvidia_pci_dir()
    return _nvidia_pci_dir


def nvidia_gpu_is_powered():
    dev = _nvidia_pci_dir_cached()
    if dev is None:
        return False
    # Never poll a runtime-suspended GPU just to check on it -- that would
    # force a resume. "active" is the only value that means "safe to query".
    return _read(os.path.join(dev, "power", "runtime_status")) == "active"


def read_nvidia_gpu():
    """Returns (temp_c, usage_pct), either possibly None.

    None for both means: no NVIDIA GPU, or it's runtime-suspended (asleep,
    contributing no heat -- this is a normal state, not an error), or
    nvidia-smi failed/timed out.
    """
    if _nvidia_pci_dir_cached() is None or not nvidia_gpu_is_powered():
        return None, None
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=temperature.gpu,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3.0,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    parts = [p.strip() for p in out.split(",")]
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None, None


def has_nvidia_gpu():
    return _nvidia_pci_dir_cached() is not None


# nvidia-smi itself resets the GPU's runtime-PM idle timer on every call
# (unlike reading runtime_status, which is a PM-core state read that
# doesn't touch the device). "active" per runtime_status just means "not
# yet suspended" -- it stays active for a few seconds after real work
# ends, while the autosuspend delay counts down. Polling nvidia-smi during
# that tail would keep resetting the timer and the GPU would never
# actually reach suspend. So: while genuinely busy (last-seen utilization
# above the idle threshold), poll fast for live readings; once utilization
# drops to ~idle, back off so the autosuspend countdown can complete
# uninterrupted. The cheap runtime_status check still runs every call, so
# a suspended GPU is reported as suspended immediately either way.
NVIDIA_SMI_BUSY_MIN_INTERVAL_S = 2.0
NVIDIA_SMI_IDLE_MIN_INTERVAL_S = 30.0
NVIDIA_BUSY_UTIL_THRESHOLD = 5.0  # percent

_nvidia_cache_temp = None
_nvidia_cache_usage = None
_nvidia_cache_time = 0.0


def read_nvidia_gpu_cached():
    """Same contract as read_nvidia_gpu(), but throttled. All consumers
    (daemon command handlers, curve worker) should use this, not the raw
    read_nvidia_gpu(), to share one poll budget instead of each hammering
    nvidia-smi independently."""
    global _nvidia_cache_temp, _nvidia_cache_usage, _nvidia_cache_time

    if _nvidia_pci_dir_cached() is None or not nvidia_gpu_is_powered():
        _nvidia_cache_temp = None
        _nvidia_cache_usage = None
        _nvidia_cache_time = 0.0
        return None, None

    last_usage = _nvidia_cache_usage or 0.0
    min_interval = (
        NVIDIA_SMI_BUSY_MIN_INTERVAL_S
        if last_usage >= NVIDIA_BUSY_UTIL_THRESHOLD
        else NVIDIA_SMI_IDLE_MIN_INTERVAL_S
    )

    now = time.monotonic()
    if now - _nvidia_cache_time < min_interval:
        return _nvidia_cache_temp, _nvidia_cache_usage

    _nvidia_cache_temp, _nvidia_cache_usage = read_nvidia_gpu()
    _nvidia_cache_time = now
    return _nvidia_cache_temp, _nvidia_cache_usage
