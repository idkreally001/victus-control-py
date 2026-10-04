#!/bin/bash
# Installs victus-control: daemon (root, systemd system service), tray and
# GUI (user session). Run with sudo from the repo root's packaging/ dir or
# anywhere -- paths are resolved relative to this script.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALL_DIR="/usr/lib/victus-control"

echo "--> Installing Python sources to ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
for component in common daemon tray gui; do
    rm -rf "${INSTALL_DIR:?}/${component}"
    cp -r "${REPO_ROOT}/${component}" "${INSTALL_DIR}/"
done
# trace_logger.py is a one-off tuning tool, not part of the running
# product -- keep it out of the installed tree.
rm -f "${INSTALL_DIR}/daemon/trace_logger.py"
find "$INSTALL_DIR" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

if systemctl list-unit-files victus-backend.service &>/dev/null; then
    echo "--> Disabling old victus-backend.service (superseded by victus-daemon)..."
    systemctl disable --now victus-backend.service 2>/dev/null || true
fi

echo "--> Ensuring 'victus' group exists..."
groupadd -f victus
if [[ -n "${SUDO_USER:-}" ]]; then
    usermod -aG victus "${SUDO_USER}"
fi

echo "--> Installing GUI launcher wrapper to /usr/bin/victus-control-gui..."
cat > /usr/bin/victus-control-gui << EOF
#!/bin/bash
exec /usr/bin/python3 ${INSTALL_DIR}/gui/main.py "\$@"
EOF
chmod 0755 /usr/bin/victus-control-gui

echo "--> Installing desktop entry..."
install -D -m 0644 "${SCRIPT_DIR}/victus-control-gui.desktop" \
    /usr/share/applications/victus-control-gui.desktop

echo "--> Installing systemd units..."
install -D -m 0644 "${SCRIPT_DIR}/victus-daemon.service" \
    /usr/lib/systemd/system/victus-daemon.service
install -D -m 0644 "${SCRIPT_DIR}/victus-tray.service" \
    /usr/lib/systemd/user/victus-tray.service

systemctl daemon-reload

echo "--> Enabling+restarting victus-daemon.service (system)..."
systemctl enable victus-daemon.service
# restart, not "enable --now" -- the latter is a no-op if the service is
# already running, which would silently leave stale code loaded on a
# re-install/update run.
systemctl restart victus-daemon.service

if [[ -n "${SUDO_USER:-}" ]]; then
    uid="$(id -u "${SUDO_USER}")"
    if sudo -u "${SUDO_USER}" XDG_RUNTIME_DIR="/run/user/${uid}" \
           systemctl --user daemon-reload 2>/dev/null && \
       sudo -u "${SUDO_USER}" XDG_RUNTIME_DIR="/run/user/${uid}" \
           systemctl --user enable victus-tray.service 2>/dev/null && \
       sudo -u "${SUDO_USER}" XDG_RUNTIME_DIR="/run/user/${uid}" \
           systemctl --user restart victus-tray.service 2>/dev/null; then
        echo "Enabled+restarted victus-tray.service for user '${SUDO_USER}'."
    else
        echo "Note: could not enable victus-tray for '${SUDO_USER}' now" \
             "(no active session?). It will start on next login."
    fi
else
    echo "Note: run this installer with sudo from your desktop user to" \
         "auto-enable the tray. Otherwise enable it yourself with:" \
         "systemctl --user enable --now victus-tray.service"
fi

echo "--> Done."
