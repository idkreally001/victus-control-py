#!/usr/bin/env python3
"""One-off trace collector for tuning the Better Auto curve replacement.

Logs (timestamp, package_temp, p_core_mean_temp, gpu_temp_or_idle,
fan1_rpm, fan2_rpm, mode) to a CSV every 3s. Read-only against hardware --
no fan writes. Run as root (needs the same sysfs read access as the
daemon). Safe to run alongside the daemon; doesn't touch the socket.
"""

import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daemon import hardware

OUT_PATH = os.path.join(os.path.dirname(__file__), "trace.csv")
INTERVAL_S = 3.0


def main():
    if os.geteuid() != 0:
        print("trace_logger: must run as root (reads coretemp/hwmon)", file=sys.stderr)
        return 1

    new_file = not os.path.exists(OUT_PATH)
    with open(OUT_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["ts", "package_c", "p_core_mean_c", "gpu_c",
                              "gpu_idle", "fan1_rpm", "fan2_rpm", "pwm_enable"])

        print(f"trace_logger: writing to {OUT_PATH}, ctrl-c to stop", file=sys.stderr)
        try:
            while True:
                ts = time.time()
                pkg = hardware.read_cpu_package_temp()
                p_cores = hardware.read_p_core_temps()
                p_mean = sum(p_cores) / len(p_cores) if p_cores else None

                gpu_temp = None
                gpu_idle = True
                if hardware.has_nvidia_gpu():
                    gpu_temp, _ = hardware.read_nvidia_gpu_cached()
                    gpu_idle = gpu_temp is None

                hwmon_dir = hardware.find_hwmon_dir()
                fan1 = hardware.read_fan_rpm(hwmon_dir, 1) if hwmon_dir else None
                fan2 = hardware.read_fan_rpm(hwmon_dir, 2) if hwmon_dir else None
                pwm = hardware.read_pwm_enable(hwmon_dir) if hwmon_dir else None

                writer.writerow([ts, pkg, p_mean, gpu_temp, gpu_idle, fan1, fan2, pwm])
                f.flush()
                time.sleep(INTERVAL_S)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
