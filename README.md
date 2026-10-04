# victus-control-py

Fan control for HP Victus laptops (and likely other hp-wmi platform devices): a root daemon, a tray icon, and a GUI, talking over a local unix socket.

Python rewrite of the original [victus-control](https://github.com/Batuhan4/victus-control) C++/GTK project — same hardware target, different implementation. No code shared between the two.

## Why

On this hardware, firmware AUTO (`pwm1_enable=2`) leaves the fans at 0 RPM through 83-97°C package temp under real load — it does not respond to Linux-visible thermal state. This project's "AUTO" mode instead runs a software curve on top of hardware MANUAL mode (`pwm1_enable=1`), reasserted periodically since the EC silently reverts control back to firmware after a timeout.

## Layout

- `daemon/` — root-owned process, owns the hwmon writes and the software AUTO curve, exposes a unix socket (`common/protocol.py` defines the wire format)
- `gui/` — PyQt GUI client (manual RPM control, mode switching, live sensor readout)
- `tray/` — system tray client (quick mode switch, no root needed)
- `common/` — shared protocol/constants used by all three
- `packaging/` — systemd units, desktop entry, install script

## Install

```
sudo packaging/install.sh
```

Installs sources to `/usr/lib/victus-control`, sets up the `victus` group (so the tray/GUI can talk to the daemon's socket without root), enables `victus-daemon.service` (system) and `victus-tray.service` (user).

## Fan modes

- `AUTO` — software curve (see above), the real default
- `MANUAL` — fixed RPM set via GUI/socket
- `MAX` — full speed, `pwm1_enable=0`
