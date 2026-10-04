#!/usr/bin/env python3
"""victus-control GUI: fan control window talking to the daemon over the
unix socket. No keyboard/RGB control, no Better Auto curve UI -- both out
of scope for this rewrite.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from common.protocol import ProtocolError, request

POLL_INTERVAL_MS = 2000
FAN_MODES = ("AUTO", "MANUAL", "MAX")
FAN_MAX_REFRESH_EVERY_N_POLLS = 15  # ~30s at the default 2s poll interval


RPM_STEP = 500


DEFAULT_TARGET_RPM = 3000


class FanControl(QWidget):
    def __init__(self, fan_num, on_speed_changed, parent=None):
        super().__init__(parent)
        self.fan_num = fan_num
        self._on_speed_changed = on_speed_changed
        self.max_rpm = 1
        # The user's last chosen target, independent of whatever hardware
        # happens to be doing right now -- MANUAL should restore *this*,
        # not whatever RPM the fan was coasting at from AUTO/MAX.
        self.last_target_rpm = DEFAULT_TARGET_RPM

        box = QGroupBox(f"Fan {fan_num}")
        layout = QVBoxLayout(box)

        self.current_label = QLabel("Current: -- RPM")
        self.current_label.setAlignment(Qt.AlignCenter)
        self.target_label = QLabel("Target: -- RPM")
        self.target_label.setAlignment(Qt.AlignCenter)

        # Slider range is in units of RPM_STEP, not raw RPM -- QSlider has no
        # native "snap to every Nth value" when max isn't itself a multiple
        # of the step (fan maxes are 5800/6100, not round), so we map
        # slider-steps <-> RPM ourselves instead of fighting tickInterval.
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(1)
        self.slider.setTickPosition(QSlider.TicksBelow)
        self.slider.setTickInterval(1)
        self.slider.valueChanged.connect(self._handle_value_changed)
        self.slider.sliderReleased.connect(self._handle_slider_released)

        layout.addWidget(self.current_label)
        layout.addWidget(self.target_label)
        layout.addWidget(self.slider)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

    def _step_to_rpm(self, step):
        return min(step * RPM_STEP, self.max_rpm)

    def _rpm_to_step(self, rpm):
        return round(min(rpm, self.max_rpm) / RPM_STEP)

    def _handle_value_changed(self, step):
        self.target_label.setText(f"Target: {self._step_to_rpm(step)} RPM")

    def _handle_slider_released(self):
        rpm = self._step_to_rpm(self.slider.value())
        self.last_target_rpm = rpm
        self._on_speed_changed(self.fan_num, rpm)

    def set_max_rpm(self, max_rpm):
        if max_rpm <= 0:
            max_rpm = 1
        self.max_rpm = max_rpm
        # Blocked: Qt clamps the current value into the new range and fires
        # valueChanged as a side effect of resizing the range, which is not
        # a user edit and must not overwrite Target.
        self.slider.blockSignals(True)
        self.slider.setMaximum(self._rpm_to_step(max_rpm))
        self.slider.blockSignals(False)

    def update_current_label(self, rpm):
        """Called every poll, in every mode. Text only -- never touches the
        slider. The slider is a target *input*, not a live gauge; polling
        must never move it (that was the source of it "moving on its own"
        and of Current/Target/slider-position all disagreeing)."""
        self.current_label.setText(f"Current: {rpm} RPM")

    def restore_last_target(self):
        """Called once when entering MANUAL. Restores the user's own last
        chosen target (not whatever RPM the fan happened to be coasting at
        from AUTO/MAX) and re-sends it to the daemon so hardware actually
        matches what's displayed -- entering MANUAL otherwise leaves the
        fan frozen at its old AUTO/MAX speed with nothing driving it."""
        self.slider.blockSignals(True)
        self.slider.setValue(self._rpm_to_step(self.last_target_rpm))
        self.slider.blockSignals(False)
        rpm = self._step_to_rpm(self.slider.value())
        self.target_label.setText(f"Target: {rpm} RPM")
        self._on_speed_changed(self.fan_num, rpm)

    def sync_display_to_rpm(self, rpm):
        """Reflects a daemon-driven RPM into the slider/Target display
        without resending anything (not a user drag). Two call sites:
        daemon clamped a just-sent target down (exceeded fan max), or this
        is the first poll and MANUAL/a target were already set by
        something else before the GUI existed."""
        self.last_target_rpm = rpm
        self.slider.blockSignals(True)
        self.slider.setValue(self._rpm_to_step(rpm))
        self.slider.blockSignals(False)
        self.target_label.setText(f"Target: {self._step_to_rpm(self.slider.value())} RPM")

    def set_interactive(self, enabled):
        self.slider.setEnabled(enabled)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Victus Control")

        self._daemon_was_up = None  # tri-state: None = unknown yet
        self._poll_count = 0
        self._current_mode = None
        self._propagating_lock = False
        self._settings = QSettings("victus-control", "gui")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        status_box = QGroupBox()
        status_outer = QVBoxLayout(status_box)

        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("<b>Status</b>"))
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #888888; font-size: 12px;")
        self.status_dot.setToolTip("Connecting...")
        title_row.addWidget(self.status_dot)
        title_row.addStretch()
        status_outer.addLayout(title_row)

        status_grid = QGridLayout()
        self.cpu_temp_label = QLabel("--")
        self.gpu_temp_label = QLabel("--")
        self.fan1_rpm_label = QLabel("--")
        self.fan2_rpm_label = QLabel("--")
        status_grid.addWidget(QLabel("CPU Temp:"), 0, 0)
        status_grid.addWidget(self.cpu_temp_label, 0, 1)
        status_grid.addWidget(QLabel("GPU Temp:"), 1, 0)
        status_grid.addWidget(self.gpu_temp_label, 1, 1)
        status_grid.addWidget(QLabel("Fan 1:"), 2, 0)
        status_grid.addWidget(self.fan1_rpm_label, 2, 1)
        status_grid.addWidget(QLabel("Fan 2:"), 3, 0)
        status_grid.addWidget(self.fan2_rpm_label, 3, 1)
        status_outer.addLayout(status_grid)
        root.addWidget(status_box)

        mode_box = QGroupBox("Fan Mode")
        mode_layout = QHBoxLayout(mode_box)
        self.mode_buttons = {}
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for mode in FAN_MODES:
            btn = QPushButton(mode)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked, m=mode: self._handle_mode_clicked(m))
            mode_layout.addWidget(btn)
            self.mode_group.addButton(btn)
            self.mode_buttons[mode] = btn
        root.addWidget(mode_box)

        self.lock_checkbox = QCheckBox("Lock fans together")
        self.lock_checkbox.setChecked(bool(self._settings.value("lock_fans", False, type=bool)))
        self.lock_checkbox.toggled.connect(self._handle_lock_toggled)
        root.addWidget(self.lock_checkbox)

        speeds_layout = QHBoxLayout()
        self.fan1 = FanControl(1, self._handle_fan_speed_changed)
        self.fan2 = FanControl(2, self._handle_fan_speed_changed)
        self.fan1.slider.valueChanged.connect(self._handle_fan1_slider_moved)
        self.fan2.slider.valueChanged.connect(self._handle_fan2_slider_moved)
        speeds_layout.addWidget(self.fan1)
        speeds_layout.addWidget(self.fan2)
        root.addLayout(speeds_layout)

        self._set_controls_enabled(False)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_INTERVAL_MS)

        self._fetch_fan_maxes()
        self._poll()

    def _set_controls_enabled(self, enabled):
        for btn in self.mode_buttons.values():
            btn.setEnabled(enabled)
        if not enabled:
            self.fan1.set_interactive(False)
            self.fan2.set_interactive(False)

    def _mark_daemon_down(self):
        if self._daemon_was_up is not False:
            self.status_dot.setStyleSheet("color: #c0392b; font-size: 12px;")
            self.status_dot.setToolTip("Daemon not running -- retrying...")
            self._set_controls_enabled(False)
        self._daemon_was_up = False

    def _mark_daemon_up(self):
        if self._daemon_was_up is not True:
            self.status_dot.setStyleSheet("color: #27ae60; font-size: 12px;")
            self.status_dot.setToolTip("Connected")
            self._set_controls_enabled(True)
            self._fetch_fan_maxes()
        self._daemon_was_up = True

    def _fetch_fan_maxes(self):
        try:
            r1 = request("GET_FAN_MAX_SPEED", fan=1)
            r2 = request("GET_FAN_MAX_SPEED", fan=2)
        except (ProtocolError, OSError):
            return
        self.fan1.set_max_rpm(r1.get("rpm", 1))
        self.fan2.set_max_rpm(r2.get("rpm", 1))

    def _poll(self):
        try:
            cpu = request("GET_CPU_TEMP")
            gpu = request("GET_GPU_TEMP")
            fan1_speed = request("GET_FAN_SPEED", fan=1)
            fan2_speed = request("GET_FAN_SPEED", fan=2)
            mode_resp = request("GET_FAN_MODE")
        except (ProtocolError, OSError):
            self._mark_daemon_down()
            return

        self._mark_daemon_up()

        self.cpu_temp_label.setText(f"{cpu.get('temp_c')} C")
        if gpu.get("idle"):
            self.gpu_temp_label.setText("idle")
        else:
            self.gpu_temp_label.setText(f"{gpu.get('temp_c')} C")

        fan1_rpm = fan1_speed.get("rpm", 0)
        fan2_rpm = fan2_speed.get("rpm", 0)
        self.fan1_rpm_label.setText(f"{fan1_rpm} RPM")
        self.fan2_rpm_label.setText(f"{fan2_rpm} RPM")

        mode = mode_resp.get("mode")
        self.fan1.update_current_label(fan1_rpm)
        self.fan2.update_current_label(fan2_rpm)

        # On AUTO/MAX -> MANUAL (triggered externally, e.g. another client --
        # our own click path handles this itself in _handle_mode_clicked and
        # sets _current_mode before this ever sees the edge), restore the
        # user's last chosen target rather than whatever RPM the fan
        # happened to be coasting at. Excludes the very first poll after
        # launch: _current_mode is None then only because we haven't
        # observed anything yet, not because a transition just happened --
        # treating that as a transition meant simply opening the GUI while
        # something else had already put the daemon in MANUAL (e.g. a
        # manual test script) would silently stomp its fan targets with
        # the GUI's own default 3000 RPM.
        first_poll = self._current_mode is None
        entering_manual = (
            not first_poll and mode == "MANUAL" and self._current_mode != "MANUAL"
        )
        if entering_manual:
            self.fan1.restore_last_target()
            self.fan2.restore_last_target()
        elif first_poll and mode == "MANUAL":
            # Daemon already in MANUAL before this GUI even existed --
            # sync the display to real hardware state without resending
            # anything (no fan write here, unlike the transition case
            # above). Also seeds last_target_rpm so a *later* AUTO/MAX ->
            # MANUAL transition restores this observed value instead of
            # the class default.
            self.fan1.sync_display_to_rpm(fan1_rpm)
            self.fan2.sync_display_to_rpm(fan2_rpm)

        self._apply_mode(mode)

        self._poll_count += 1
        if self._poll_count % FAN_MAX_REFRESH_EVERY_N_POLLS == 0:
            self._fetch_fan_maxes()

    def _apply_mode(self, mode):
        self._current_mode = mode
        for m, btn in self.mode_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(m == mode)
            btn.blockSignals(False)
        manual = mode == "MANUAL"
        self.fan1.set_interactive(manual)
        self.fan2.set_interactive(manual)

    def _handle_lock_toggled(self, locked):
        self._settings.setValue("lock_fans", locked)
        if locked:
            # Bring both fans to the same RPM immediately so "locked" starts
            # from a sane shared state rather than two different speeds.
            shared = self.fan1.last_target_rpm
            self.fan2.last_target_rpm = shared
            self.fan2.slider.blockSignals(True)
            self.fan2.slider.setValue(self.fan2._rpm_to_step(shared))
            self.fan2.slider.blockSignals(False)
            self.fan2.target_label.setText(f"Target: {self.fan2._step_to_rpm(self.fan2.slider.value())} RPM")

    def _mirror_slider(self, source, target):
        if not self.lock_checkbox.isChecked():
            return
        # Mirror by RPM, not raw step -- the two fans have different max
        # RPM (5800 vs 6100), so the same step index would mean different
        # speeds on each; mirroring the RPM value and letting the target
        # slider clamp to its own max is the correct shared behaviour.
        rpm = source._step_to_rpm(source.slider.value())
        target.slider.blockSignals(True)
        target.slider.setValue(target._rpm_to_step(rpm))
        target.slider.blockSignals(False)
        target.target_label.setText(f"Target: {target._step_to_rpm(target.slider.value())} RPM")

    def _handle_fan1_slider_moved(self, _step):
        self._mirror_slider(self.fan1, self.fan2)

    def _handle_fan2_slider_moved(self, _step):
        self._mirror_slider(self.fan2, self.fan1)

    def _handle_mode_clicked(self, mode):
        entering_manual = mode == "MANUAL" and self._current_mode != "MANUAL"
        try:
            request("SET_FAN_MODE", mode=mode)
        except (ProtocolError, OSError):
            self._mark_daemon_down()
            return
        self._apply_mode(mode)
        if entering_manual:
            # We know definitively this is an entry (the user just clicked
            # it), so restore+resend the target here instead of waiting for
            # _poll's edge-detection -- _apply_mode above already set
            # _current_mode to MANUAL, which would make that edge invisible
            # to the next poll otherwise.
            self.fan1.restore_last_target()
            self.fan2.restore_last_target()

    def _handle_fan_speed_changed(self, fan_num, rpm):
        try:
            resp = request("SET_FAN_SPEED", fan=fan_num, rpm=rpm)
        except (ProtocolError, OSError):
            self._mark_daemon_down()
            return
        clamped = resp.get("rpm", rpm)
        control = self.fan1 if fan_num == 1 else self.fan2
        if clamped != rpm:
            # Daemon clamped the requested RPM down (exceeded fan max) --
            # reflect the real accepted target back into the slider/Target.
            control.sync_display_to_rpm(clamped)
        # SET_FAN_SPEED always forces the daemon into MANUAL mode -- reflect
        # that in the mode selector even though we didn't call SET_FAN_MODE.
        self._apply_mode("MANUAL")

        # Locked: the release only reported the dragged fan -- _mirror_slider
        # already moved the other fan's slider/Target visually while
        # dragging, but hardware still needs its own SET_FAN_SPEED. Guard
        # against infinite recursion (this call itself re-enters here) by
        # only forwarding once, from the originally-dragged fan's release.
        if self.lock_checkbox.isChecked() and not self._propagating_lock:
            other = self.fan2 if fan_num == 1 else self.fan1
            other_rpm = other._step_to_rpm(other.slider.value())
            self._propagating_lock = True
            try:
                self._handle_fan_speed_changed(other.fan_num, other_rpm)
            finally:
                self._propagating_lock = False


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(360, 420)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
