from __future__ import annotations

import json
import logging
import math
import os
import struct
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from scca.styles import DASHBOARD_QSS
from scca.udp_receiver import UDPReceiver, CommandSender, MockRaspberryAutopilot, MANEUVER_IDS


class ToggleSliderButton(QAbstractButton):
    def __init__(self, text: str, danger: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(270, 42)
        self.danger = danger

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        frame_rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#304156"), 1.2))
        painter.setBrush(QColor("#13202d"))
        painter.drawRoundedRect(frame_rect, 9, 9)

        painter.setPen(QColor("#dbe5ef"))
        painter.setFont(QFont("Rajdhani", 12, QFont.Weight.Bold))
        text_rect = self.rect().adjusted(12, 0, -92, 0)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())

        track_w = 52
        track_h = 26
        track_x = self.width() - track_w - 14
        track_y = (self.height() - track_h) // 2

        if self.isChecked():
            track_color = QColor("#f04a54") if self.danger else QColor("#16ff9a")
            border_color = QColor("#b83a44") if self.danger else QColor("#1e8f62")
            knob_x = track_x + track_w - 22 - 2
        else:
            track_color = QColor("#334354")
            border_color = QColor("#3b4f63")
            knob_x = track_x + 2

        painter.setPen(QPen(border_color, 1.1))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_x, track_y, track_w, track_h, 13, 13)

        painter.setPen(QPen(QColor("#0f141b"), 1.0))
        painter.setBrush(QColor("#ecf3fb"))
        painter.drawEllipse(knob_x, track_y + 2, 22, 22)

        if self.underMouse():
            painter.setPen(QPen(QColor("#4e6a85"), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(frame_rect, 9, 9)


class LedIndicator(QWidget):
    def __init__(self, color_on: str, color_off: str = "#3b4754", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.color_on = QColor(color_on)
        self.color_off = QColor(color_off)
        self.is_on = False

    def set_on(self, enabled: bool) -> None:
        self.is_on = enabled
        self.update()

    def paintEvent(self, event) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.color_on if self.is_on else self.color_off
        painter.setPen(QPen(QColor("#0f141b"), 1.5))
        painter.setBrush(color)
        painter.drawEllipse(1, 1, 16, 16)


class CircularForceGauge(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(230, 230)
        self.kg_value = 0.0
        self.max_kg = 80.0

    def set_force_kg(self, value: float) -> None:
        self.kg_value = max(0.0, min(self.max_kg, value))
        self.update()

    def paintEvent(self, event) -> None:
        del event
        width = self.width()
        height = self.height()
        side = min(width, height) - 14
        rect_x = (width - side) // 2
        rect_y = (height - side) // 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setPen(QPen(QColor("#243548"), 14, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect_x, rect_y, side, side, 225 * 16, -270 * 16)

        span = int((self.kg_value / self.max_kg) * 270)
        painter.setPen(QPen(QColor("#f58f2d"), 14, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(rect_x, rect_y, side, side, 225 * 16, -span * 16)

        painter.setPen(QColor("#eaf3ff"))
        value_font = QFont("Rajdhani", 26, QFont.Weight.Bold)
        painter.setFont(value_font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{self.kg_value:04.1f} KG")

        newtons = self.kg_value * 9.80665
        sub_rect = self.rect().adjusted(0, 48, 0, 0)
        painter.setPen(QColor("#82d8ff"))
        unit_font = QFont("Rajdhani", 13, QFont.Weight.DemiBold)
        painter.setFont(unit_font)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignCenter, f"{newtons:05.1f} N")

        painter.setPen(QColor("#7f93a8"))
        tick_font = QFont("Rajdhani", 10, QFont.Weight.Medium)
        painter.setFont(tick_font)
        for pct, label in [(0.0, "0"), (0.25, "20"), (0.5, "40"), (0.75, "60"), (1.0, "80")]:
            angle_deg = 225 - (270 * pct)
            rad = math.radians(angle_deg)
            r = (side / 2) - 4
            tx = width / 2 + (r - 15) * math.cos(rad)
            ty = height / 2 - (r - 15) * math.sin(rad)
            painter.drawText(int(tx) - 10, int(ty) + 5, label)


class LinuxJoystickReader(QObject):
    """Leitura local do joystick do Linux usado para a posição do coletivo."""

    position_changed = Signal(float)
    raw_position_changed = Signal(int)
    connection_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.device_path = os.getenv("SCCA_JOYSTICK_DEVICE", "/dev/input/js0")
        axis_env = os.getenv("SCCA_JOYSTICK_AXIS", "")
        self.axis_number = int(axis_env) if axis_env.strip().isdigit() else None
        self.invert_axis = os.getenv("SCCA_JOYSTICK_INVERT", "0").strip().lower() in {"1", "true", "yes", "on"}
        self.min_raw = int(os.getenv("SCCA_JOYSTICK_MIN_RAW", "32767"))
        self.max_raw = int(os.getenv("SCCA_JOYSTICK_MAX_RAW", "-32767"))
        self.smoothing_alpha = float(os.getenv("SCCA_JOYSTICK_SMOOTHING_ALPHA", "0.18"))
        self.deadband_percent = float(os.getenv("SCCA_JOYSTICK_DEADBAND_PERCENT", "0.25"))
        self.smoothing_alpha = max(0.01, min(1.0, self.smoothing_alpha))
        self.deadband_percent = max(0.0, min(5.0, self.deadband_percent))
        self._fd: int | None = None
        self._connected = False
        self._position_percent = 0.0
        self._filtered_position_percent = 0.0
        self._has_filtered_position = False
        self._raw_position = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_device)

    @property
    def position_percent(self) -> float:
        return self._position_percent

    @property
    def raw_position(self) -> int:
        """Último valor bruto do eixo (escala nativa do driver de joystick,
        sem suavização nem conversão). É essa escala que deve circular no
        protocolo UDP com o Raspberry Pi."""
        return self._raw_position

    def start(self, poll_interval_ms: int = 20) -> None:
        self._timer.start(max(10, poll_interval_ms))
        self._poll_device()

    def stop(self) -> None:
        self._timer.stop()
        self._close_device()

    def _set_connected(self, connected: bool) -> None:
        if self._connected != connected:
            self._connected = connected
            self.connection_changed.emit(connected)

    def _close_device(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._set_connected(False)

    def _open_device(self) -> bool:
        if self._fd is not None:
            return True

        try:
            self._fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
            self._set_connected(True)
            return True
        except FileNotFoundError:
            self._set_connected(False)
            return False
        except PermissionError as exc:
            self._close_device()
            self.error_occurred.emit(f"Sem permissão para ler {self.device_path}: {exc}")
            return False
        except OSError as exc:
            self._close_device()
            self.error_occurred.emit(f"Falha ao abrir {self.device_path}: {exc}")
            return False

    def _raw_to_percent(self, raw_value: int) -> float:
        low = self.min_raw
        high = self.max_raw
        if high == low:
            return 0.0

        if low > high:
            low, high = high, low

        clipped = max(low, min(high, raw_value))
        percent = ((clipped - high) / (low - high)) * 100.0
        if self.invert_axis:
            percent = 100.0 - percent
        return max(0.0, min(100.0, percent))

    def _smooth_position(self, new_position: float) -> float:
        if not self._has_filtered_position:
            self._filtered_position_percent = new_position
            self._has_filtered_position = True
            return new_position

        delta = new_position - self._filtered_position_percent
        if abs(delta) <= self.deadband_percent:
            return self._filtered_position_percent

        self._filtered_position_percent += delta * self.smoothing_alpha
        return self._filtered_position_percent

    def _poll_device(self) -> None:
        if not self._open_device() or self._fd is None:
            return

        while True:
            try:
                chunk = os.read(self._fd, 8)
            except BlockingIOError:
                break
            except OSError as exc:
                self.error_occurred.emit(f"Erro ao ler joystick em {self.device_path}: {exc}")
                self._close_device()
                return

            if len(chunk) < 8:
                break

            _event_time, value, event_type, number = struct.unpack("<IhBB", chunk)
            event_kind = event_type & 0x7F
            if event_kind != 0x02:
                continue

            if self.axis_number is None:
                self.axis_number = number

            if number != self.axis_number:
                continue

            self._raw_position = int(value)
            self.raw_position_changed.emit(self._raw_position)

            new_position = self._raw_to_percent(value)
            smoothed_position = self._smooth_position(new_position)
            if abs(smoothed_position - self._position_percent) >= 0.05:
                self._position_percent = smoothed_position
                self.position_changed.emit(smoothed_position)


class SccaDashboard(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dashboard do Sistema de Comando Coletivo")
        self.resize(1300, 760)

        # Inicializar UDP Receiver (recebe telemetria do Raspberry Pi)
        self.udp_receiver = UDPReceiver(host="0.0.0.0", port=5006)
        self.udp_receiver.packet_received.connect(self._on_udp_packet_received)
        self.udp_receiver.error_occurred.connect(self._on_udp_error)
        self.udp_receiver.connection_status_changed.connect(self._on_udp_connection_changed)

        # Inicializar Command Sender (envia manobras para Raspberry Pi)
        initial_host = os.getenv("SCCA_COMMAND_HOST", "10.0.0.1")
        try:
            initial_port = int(os.getenv("SCCA_COMMAND_PORT", "5005"))
        except ValueError:
            initial_port = 5005
            self.logger = logging.getLogger("Dashboard")
            self.logger.warning("SCCA_COMMAND_PORT inválida; usando 5005")
        try:
            initial_interval_ms = int(os.getenv("SCCA_COMMAND_INTERVAL_MS", "50"))
        except ValueError:
            initial_interval_ms = 50
        self.command_sender = CommandSender(
            receiver_host=initial_host,
            receiver_port=initial_port,
            send_interval_ms=initial_interval_ms,
        )
        self.command_sender.command_sent.connect(self._on_command_sent)
        self.command_sender.error_occurred.connect(self._on_command_error)
        self._command_target_host = initial_host
        self._command_target_port = initial_port

        # Simulador local opcional de Raspberry (comando + telemetria)
        self.mock_raspberry = MockRaspberryAutopilot(
            command_host="127.0.0.1",
            command_port=initial_port,
            telemetry_host="127.0.0.1",
            telemetry_port=5006,
        )
        self.mock_raspberry.status_changed.connect(self._on_mock_status_changed)
        self.mock_raspberry.error_occurred.connect(self._on_mock_error)

        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setSpacing(12)

        title = QLabel("DASHBOARD")
        title.setObjectName("title")
        subtitle = QLabel("Sistema de Comando Coletivo | Supervisao Integrada")
        subtitle.setObjectName("subtitle")
        main_layout.addWidget(title)
        main_layout.addWidget(subtitle)

        body = QHBoxLayout()
        body.setSpacing(12)
        main_layout.addLayout(body, 1)

        position_panel = self._build_position_panel()
        states_panel = self._build_states_panel()
        telemetry_panel = self._build_telemetry_panel()
        maneuver_panel = self._build_maneuver_panel()
        udp_panel = self._build_udp_panel()

        center_container = QWidget()
        center_layout = QVBoxLayout(center_container)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(12)
        center_layout.addWidget(telemetry_panel, 1)
        center_layout.addWidget(maneuver_panel)
        center_layout.addWidget(udp_panel)

        body.addWidget(position_panel)
        body.addWidget(center_container, 1)
        body.addWidget(states_panel)

        self.joystick_reader = LinuxJoystickReader()
        self.joystick_reader.position_changed.connect(self._on_joystick_position_changed)
        self.joystick_reader.raw_position_changed.connect(self._on_joystick_raw_position_changed)
        self.joystick_reader.connection_changed.connect(self._on_joystick_connection_changed)
        self.joystick_reader.error_occurred.connect(self._on_joystick_error)

        self.flash_timer = QTimer(self)
        self.flash_timer.setInterval(300)
        self.flash_timer.timeout.connect(self._toggle_alert_flash)
        self._flash_on = False
        self._last_udp_packet_time = 0.0

        self._calibration_pos_top: int | None = None
        self._calibration_pos_bottom: int | None = None
        self._is_calibrated = False
        self._joystick_raw_position = 0
        self._filtered_calibrated_percent = 0.0
        self._has_filtered_calibrated_percent = False

        # Estado local do comando enviado ao Raspberry (pacote P)
        self._control_tick_s = max(0.05, min(0.15, initial_interval_ms / 1000.0))
        self._control_time_s = 0.0
        self._transducer_cmd = 0
        self._selected_maneuver_name = "Manobra 1"

        self.control_timer = QTimer(self)
        self.control_timer.setInterval(int(self._control_tick_s * 1000.0))
        self.control_timer.timeout.connect(self._update_control_stream_state)
        
        # Logger
        self.logger = logging.getLogger("Dashboard")

        # Iniciar servidor UDP
        self.udp_receiver.start()
        self.joystick_reader.start()
        self.command_sender.start_stream()
        self.control_timer.start()

        self.logger.info(
            "Dashboard UDP ativo | telemetria :5006 | comandos -> "
            f"{self._command_target_host}:{self._command_target_port}"
        )
        self.logger.info(
            "Ordem de partida: 1) este dashboard  2) agente na Raspberry (calibragem exige pacotes P)."
        )
        self.maneuver_hint.setText(
            f"Enviando P -> {self._command_target_host}:{self._command_target_port} | "
            "Inicie a Raspberry depois do dashboard"
        )

    def _panel_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("panel")
        frame.setFrameShape(QFrame.Shape.NoFrame)
        return frame

    def _build_position_panel(self) -> QFrame:
        panel = self._panel_frame()
        panel.setProperty("sidebar", "true")
        panel.setFixedWidth(260)
        layout = QVBoxLayout(panel)

        head = QLabel("Monitoramento de Posicao (Joystick)")
        head.setObjectName("subtitle")
        layout.addWidget(head)

        row = QHBoxLayout()
        self.position_bar = QProgressBar()
        self.position_bar.setObjectName("verticalGauge")
        self.position_bar.setOrientation(Qt.Orientation.Vertical)
        self.position_bar.setRange(0, 1000)
        self.position_bar.setValue(523)
        self.position_bar.setFixedWidth(64)
        self.position_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.position_bar.setTextVisible(False)

        self.position_display = QLabel("52.3%")
        self.position_display.setObjectName("displayValue")

        row.addWidget(self.position_bar)
        row.addWidget(self.position_display, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(row, 1)

        status_row = QHBoxLayout()
        status_label = QLabel("Entrada local")
        status_label.setObjectName("subtitle")
        self.joystick_led = LedIndicator("#16ff9a", "#3b4754")
        self.joystick_status = QLabel("Aguardando joystick do Arduino")
        self.joystick_status.setObjectName("subtitle")
        self.joystick_status.setStyleSheet("color: #82d8ff; font-size: 10px;")
        status_row.addWidget(status_label)
        status_row.addWidget(self.joystick_led)
        status_row.addWidget(self.joystick_status)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        layout.addSpacing(8)

        calib_row = QHBoxLayout()
        calib_label = QLabel("Calibragem do transdutor")
        calib_label.setObjectName("subtitle")
        self.calibration_led = LedIndicator("#16ff9a", "#3b4754")
        calib_row.addWidget(calib_label)
        calib_row.addWidget(self.calibration_led)
        calib_row.addStretch(1)
        layout.addLayout(calib_row)

        self.calibration_status = QLabel("Aguardando calibragem do Raspberry Pi...")
        self.calibration_status.setObjectName("subtitle")
        self.calibration_status.setStyleSheet("color: #82d8ff; font-size: 10px;")
        self.calibration_status.setWordWrap(True)
        layout.addWidget(self.calibration_status)
        return panel

    def _state_label(self, text: str, state: str = "off") -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("stateLabel")
        lbl.setProperty("state", state)
        return lbl

    def _build_states_panel(self) -> QFrame:
        panel = self._panel_frame()
        panel.setFixedWidth(280)
        layout = QVBoxLayout(panel)

        head = QLabel("Estados do Sistema")
        head.setObjectName("subtitle")
        layout.addWidget(head)

        self.trim_hold_lbl = self._state_label("Trim: HOLD", "ok")
        self.trim_release_lbl = self._state_label("Trim: RELEASE", "off")

        self.beep_up_lbl = self._state_label("Beep Trim: UP", "off")
        self.beep_down_lbl = self._state_label("Beep Trim: DOWN", "off")

        self.pa_active_lbl = self._state_label("PA: ACTIVE", "off")
        self.pa_override_lbl = self._state_label("PA: OVERRIDE", "off")

        self.alert_lbl = QLabel("ALERTA CRITICO: FALHA HIDRAULICA")
        self.alert_lbl.setObjectName("criticalAlert")
        self.alert_lbl.hide()

        layout.addWidget(self.trim_hold_lbl)
        layout.addWidget(self.trim_release_lbl)
        layout.addSpacing(8)
        layout.addWidget(self.beep_up_lbl)
        layout.addWidget(self.beep_down_lbl)
        layout.addSpacing(8)
        layout.addWidget(self.pa_active_lbl)
        layout.addWidget(self.pa_override_lbl)
        layout.addSpacing(10)
        layout.addWidget(self.alert_lbl)
        layout.addStretch(1)
        return panel

    def _build_telemetry_panel(self) -> QFrame:
        panel = self._panel_frame()
        layout = QVBoxLayout(panel)

        head = QLabel("Telemetria em Tempo Real")
        head.setObjectName("subtitleTelemetry")
        layout.addWidget(head)

        self.force_gauge = CircularForceGauge()
        self.force_gauge.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.force_gauge, alignment=Qt.AlignmentFlag.AlignCenter)

        raw_frame = QFrame()
        raw_frame.setObjectName("rawLoadCellFrame")
        raw_frame.setMinimumHeight(72)
        raw_frame.setStyleSheet(
            "QFrame#rawLoadCellFrame {"
            "background-color: rgba(10, 20, 30, 170);"
            "border: 1px solid #3b4f63;"
            "border-radius: 8px;"
            "padding: 8px;"
            "}"
        )
        raw_layout = QVBoxLayout(raw_frame)
        raw_layout.setContentsMargins(10, 8, 10, 8)
        raw_layout.setSpacing(2)

        raw_title = QLabel("Célula de carga bruta")
        raw_title.setObjectName("subtitle")
        self.raw_load_cell_value = QLabel("--")
        self.raw_load_cell_value.setObjectName("displayValue")
        self.raw_load_cell_value.setStyleSheet("font-size: 26px; color: #82d8ff;")
        self.raw_load_cell_value.setAlignment(Qt.AlignmentFlag.AlignCenter)

        raw_layout.addWidget(raw_title, alignment=Qt.AlignmentFlag.AlignCenter)
        raw_layout.addWidget(self.raw_load_cell_value)
        layout.addWidget(raw_frame)

        conn_row = QHBoxLayout()
        udp_label = QLabel("UDP (Raspberry Pi)")
        udp_label.setObjectName("subtitle")
        self.udp_led = LedIndicator("#16ff9a")

        usb_label = QLabel("USB (Arduino)")
        usb_label.setObjectName("subtitle")
        self.usb_led = LedIndicator("#16ff9a")

        conn_row.addWidget(udp_label)
        conn_row.addWidget(self.udp_led)
        conn_row.addSpacing(18)
        conn_row.addWidget(usb_label)
        conn_row.addWidget(self.usb_led)
        conn_row.addStretch(1)

        layout.addLayout(conn_row)
        return panel

    # def _build_tests_panel(self, states_panel: QFrame) -> QFrame:
    #     panel = self._panel_frame()
    #     panel.setProperty("sidebar", "true")
    #     panel.setFixedWidth(300)
    #     layout = QVBoxLayout(panel)

    #     head = QLabel("Painel de Testes")
    #     head.setObjectName("subtitle")
    #     layout.addWidget(head)

    #     self.toggle_pa = ToggleSliderButton("PA Acoplado")
    #     self.toggle_pa.setChecked(self.receiver.pa_active)
    #     self.toggle_pa.setMinimumSize(260, 54)

    #     self.toggle_pa.toggled.connect(self._set_pa_active)

    #     layout.addWidget(self.toggle_pa)
    #     layout.addSpacing(8)
    #     layout.addWidget(states_panel, 1)
    #     layout.addStretch(1)
    #     return panel

    def _build_maneuver_panel(self) -> QFrame:
        panel = self._panel_frame()
        layout = QVBoxLayout(panel)

        head = QLabel("Painel de Manobras (Autopiloto)")
        head.setObjectName("subtitle")
        layout.addWidget(head)

        matrix = QGridLayout()
        matrix.setSpacing(10)

        self.maneuver_buttons: dict[str, QPushButton] = {}
        # Lista de manobras disponíveis
        maneuvers = ["Manobra 1", "Manobra 2", "Manobra 3", "Manobra 4"]
        
        for idx, name in enumerate(maneuvers):
            btn = QPushButton(name)
            btn.setObjectName("matrixTile")
            btn.setProperty("tileKind", "maneuver")
            btn.setProperty("runState", "idle")
            btn.setCheckable(True)
            btn.setMinimumSize(170, 96)
            btn.toggled.connect(lambda checked, mn=name: self._toggle_maneuver_command(mn, checked))
            self.maneuver_buttons[name] = btn
            matrix.addWidget(btn, idx // 2, idx % 2)

        self.pane_tile = QPushButton("Falha Hidraulica")
        self.pane_tile.setObjectName("matrixTile")
        self.pane_tile.setProperty("tileKind", "pane")
        self.pane_tile.setProperty("runState", "idle")
        self.pane_tile.setCheckable(True)
        self.pane_tile.setMinimumSize(170, 96)
        self.pane_tile.toggled.connect(self._send_hydraulic_failure_command)
        matrix.addWidget(self.pane_tile, 1, 1)

        self.maneuver_hint = QLabel("Clique em uma manobra para enviar comando ao Raspberry Pi")
        self.maneuver_hint.setObjectName("subtitle")

        layout.addLayout(matrix)
        layout.addWidget(self.maneuver_hint)
        layout.addStretch(1)
        return panel

    def _build_udp_panel(self) -> QFrame:
        """Painel para monitoramento de dados UDP do Raspberry Pi."""
        panel = self._panel_frame()
        layout = QVBoxLayout(panel)

        head = QLabel("Status UDP - Telemetria do Raspberry Pi")
        head.setObjectName("subtitle")
        layout.addWidget(head)

        # Status de conexão
        conn_row = QHBoxLayout()
        conn_label = QLabel("Status UDP:")
        conn_label.setObjectName("subtitle")
        self.udp_status_led = LedIndicator("#16ff9a")
        conn_row.addWidget(conn_label)
        conn_row.addWidget(self.udp_status_led)
        conn_row.addStretch(1)
        layout.addLayout(conn_row)

        # Display dos dados recebidos
        self.udp_data_info = QLabel("Aguardando dados UDP...")
        self.udp_data_info.setObjectName("subtitle")
        self.udp_data_info.setStyleSheet("color: #82d8ff; font-size: 11px;")
        layout.addWidget(self.udp_data_info)

        self.udp_endpoints_info = QLabel(
            f"Escutando telemetria em 0.0.0.0:5006 | Enviando comandos para {self._command_target_host}:{self._command_target_port}"
        )
        self.udp_endpoints_info.setObjectName("subtitle")
        self.udp_endpoints_info.setStyleSheet("color: #9db3c9; font-size: 10px;")
        layout.addWidget(self.udp_endpoints_info)

        # Contador de pacotes
        self.udp_packet_count = QLabel("Pacotes recebidos: 0")
        self.udp_packet_count.setObjectName("subtitle")
        self.udp_packet_count.setStyleSheet("color: #7f93a8; font-size: 10px;")
        layout.addWidget(self.udp_packet_count)

        # Display do último pacote
        self.udp_last_packet = QLabel("[Últimos dados do sensor]")
        self.udp_last_packet.setObjectName("subtitle")
        self.udp_last_packet.setStyleSheet(
            "color: #16ff9a; font-size: 10px; font-family: 'Courier New'; "
            "background-color: rgba(10, 20, 30, 150); padding: 6px; border-radius: 4px;"
        )
        self.udp_last_packet.setWordWrap(True)
        layout.addWidget(self.udp_last_packet)

        self.toggle_mock_mode = ToggleSliderButton("Simular Raspberry (Local)")
        self.toggle_mock_mode.setMinimumSize(250, 42)
        self.toggle_mock_mode.toggled.connect(self._toggle_mock_mode)
        layout.addWidget(self.toggle_mock_mode)

        layout.addStretch(1)
        return panel

    def _refresh_tile_style(self, tile: QPushButton) -> None:
        tile.style().unpolish(tile)
        tile.style().polish(tile)
        tile.update()

    def _toggle_maneuver_command(self, maneuver_name: str, enabled: bool) -> None:
        """Liga/desliga execução de manobra no Raspberry Pi."""
        if enabled:
            # Apenas uma manobra deve ficar ativa por vez.
            for name, btn in self.maneuver_buttons.items():
                if name != maneuver_name and btn.isChecked():
                    btn.blockSignals(True)
                    btn.setChecked(False)
                    btn.blockSignals(False)
            self.logger.info(f"Enviando comando de manobra: {maneuver_name}")
            self.command_sender.send_maneuver_command(maneuver_name, action="start")
            self._selected_maneuver_name = maneuver_name
            self.maneuver_hint.setText(f"Comando enviado: {maneuver_name} | Executando...")
        else:
            self.logger.info(f"Cancelando manobra: {maneuver_name}")
            self.command_sender.send_maneuver_stop(maneuver_name)
            self.maneuver_hint.setText(f"Comando enviado: {maneuver_name} | Cancelada")

    def _send_hydraulic_failure_command(self, enabled: bool) -> None:
        """Envia comando de simulação de falha hidráulica."""
        status = "ativado" if enabled else "desativado"
        self.logger.info(f"Enviando comando: Pane hidráulica {status}")
        self.command_sender.send_system_command("set_hydraulic_failure", enabled)
        self.maneuver_hint.setText(f"Pane hidráulica {status}")

    def _toggle_mock_mode(self, enabled: bool) -> None:
        if enabled:
            ok = self.mock_raspberry.start(interval_ms=100)
            if ok:
                self.maneuver_hint.setText("Mock Raspberry ativo | Comandos locais habilitados")
            else:
                self.toggle_mock_mode.blockSignals(True)
                self.toggle_mock_mode.setChecked(False)
                self.toggle_mock_mode.blockSignals(False)
        else:
            self.mock_raspberry.stop()
            self.maneuver_hint.setText("Mock Raspberry desativado")

    def _on_mock_status_changed(self, running: bool) -> None:
        if running:
            self.udp_data_info.setText("Mock Raspberry ativo - enviando telemetria")
        else:
            self.udp_data_info.setText("Mock Raspberry parado")

    def _on_mock_error(self, error_msg: str) -> None:
        self.logger.error(f"Mock Raspberry error: {error_msg}")
        self.udp_data_info.setText(f"ERRO MOCK: {error_msg}")

    def _set_state(self, label: QLabel, state: str) -> None:
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)
        label.update()

    def _update_calibration_display(self) -> None:
        """Atualiza o LED e o texto de status da calibragem automática.
        Também força a atualização imediata da barra/label de posição
        (position_bar/position_display) usando a última leitura bruta
        conhecida do joystick (_joystick_raw_position). 
        """
        pos_top = self._calibration_pos_top
        pos_bottom = self._calibration_pos_bottom

        if pos_top is None or pos_bottom is None:
            self.calibration_led.set_on(False)
            self.calibration_status.setText("Aguardando calibragem do Raspberry Pi...")
            return

        if self._is_calibrated:
            lower_bound = min(pos_top, pos_bottom)
            upper_bound = max(pos_top, pos_bottom)
            self.calibration_led.set_on(True)
            self.calibration_status.setText(
                f"Calibrado | Faixa do transdutor: [{lower_bound}, {upper_bound}]"
            )

            calibrated_percent = self._raw_position_to_calibrated_percent(self._joystick_raw_position)
            if calibrated_percent is not None:
                smoothed_percent = self._smooth_calibrated_percent(calibrated_percent)
                self.position_bar.setValue(int(max(0.0, min(100.0, smoothed_percent)) * 10))
                self.position_display.setText(f"{smoothed_percent:.1f}%")
        else:
            self.calibration_led.set_on(False)
            self.calibration_status.setText(
                f"Calibrando... (top={pos_top}, bottom={pos_bottom})"
            )

    def _toggle_alert_flash(self) -> None:
        self._flash_on = not self._flash_on
        self.alert_lbl.setProperty("flash", "true" if self._flash_on else "false")
        self.alert_lbl.style().unpolish(self.alert_lbl)
        self.alert_lbl.style().polish(self.alert_lbl)

    def _get_active_maneuver_name(self) -> str | None:
        for name, btn in self.maneuver_buttons.items():
            if btn.isChecked():
                return name
        return None

    def _maneuver_transducer_profile(self, maneuver_name: str, t: float) -> int:
        if maneuver_name == "Manobra 2":
            return int(12000 + 7000 * math.sin(0.75 * t))
        if maneuver_name == "Manobra 3":
            return int(15000 + 5000 * math.sin(1.15 * t + 0.6))
        if maneuver_name == "Manobra 4":
            phase = int((t / 1.2) % 4)
            levels = [4000, 11000, 18000, 7000]
            return levels[phase]
        return int(10000 + 6000 * math.sin(0.55 * t))

    def _clamp_to_calibration(self, target_position: int) -> int:
        """Restringe um alvo de posição à faixa descoberta pela calibragem
        automática do Raspberry Pi (entre pos_top e pos_bottom).
        """
        if not self._is_calibrated:
            return target_position

        pos_top = self._calibration_pos_top
        pos_bottom = self._calibration_pos_bottom
        if pos_top is None or pos_bottom is None:
            return target_position

        lower_bound = min(pos_top, pos_bottom)
        upper_bound = max(pos_top, pos_bottom)
        return max(lower_bound, min(upper_bound, target_position))

    def _raw_position_to_calibrated_percent(self, raw_value: int) -> float | None:
        """Converte um valor bruto do transdutor para percentual (0-100%)
        usando a faixa [pos_top, pos_bottom] descoberta na calibragem.
        """
        if not self._is_calibrated:
            return None

        low = self._calibration_pos_top
        high = self._calibration_pos_bottom
        if low is None or high is None or high == low:
            return None

        if low > high:
            low, high = high, low

        clipped = max(low, min(high, raw_value))
        percent = ((clipped - high) / (low - high)) * 100.0
        if self.joystick_reader.invert_axis:
            percent = 100.0 - percent
        return max(0.0, min(100.0, percent))

    def _update_control_stream_state(self) -> None:
        active_maneuver = self._get_active_maneuver_name()
        autopilot_active = active_maneuver is not None
        hydraulic_failure = bool(self.pane_tile.isChecked())

        if autopilot_active and active_maneuver is not None:
            self._selected_maneuver_name = active_maneuver

        # Envia leitura atual do transdutor (joystick) para o Raspberry usar como feedback.
        safe_target = self._clamp_to_calibration(self._joystick_raw_position)
        maneuver_id = MANEUVER_IDS.get(self._selected_maneuver_name, 0) if autopilot_active else 0

        self.command_sender.set_control_state(
            autopilot_active=autopilot_active,
            hydraulic_failure=hydraulic_failure,
            transducer_position=safe_target,
            maneuver_id=maneuver_id,
        )

    def _extract_telemetry(self, packet_dict: dict) -> dict:
        """Normaliza o payload UDP para o mesmo formato usado pela GUI."""
        parsed = packet_dict.get("parsed_data", packet_dict)
        if not isinstance(parsed, dict):
            return {}

        def to_float(value: object, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        raw_load_cell = parsed.get("load_cell")

        required_keys = (
            "beep_trim_up", "beep_trim_down", "trim_release",
            "override", "load_cell", "pos_top", "pos_bottom",
        )
        if all(k in parsed for k in required_keys):
            beep_up = int(parsed.get("beep_trim_up", 0))
            beep_down = int(parsed.get("beep_trim_down", 0))
            trim_release = int(parsed.get("trim_release", 0))
            load_cell = int(parsed.get("load_cell", 0))
            pos_top = int(parsed.get("pos_top", 0))
            pos_bottom = int(parsed.get("pos_bottom", 0))

            if beep_up:
                beep_trim = "UP"
            elif beep_down:
                beep_trim = "DOWN"
            else:
                beep_trim = "NEUTRAL"

            pilot_force_kg = max(0.0, to_float(load_cell, 0.0) / 10.0)
            # is_calibrated é inferido por pos_top != pos_bottom. (pos_top e pos_bottom serão enviados com seus valores distintos somente após a calibragem completa)
            is_calibrated = pos_top != pos_bottom
            return {
                "trim_hold": trim_release == 0,
                "beep_trim": beep_trim,
                "pa_active": any(btn.isChecked() for btn in self.maneuver_buttons.values()),
                "hydraulic_failure": bool(self.pane_tile.isChecked()),
                "raw_load_cell": raw_load_cell,
                "pilot_force_kg": pilot_force_kg,
                "pos_top": pos_top,
                "pos_bottom": pos_bottom,
                "is_calibrated": is_calibrated,
                "udp_connected": True,
                "usb_connected": True,
                "selected_maneuver": next((n for n, b in self.maneuver_buttons.items() if b.isChecked()), "Manobra 1"),
                "maneuver_active": any(btn.isChecked() for btn in self.maneuver_buttons.values()),
                "maneuver_state": "RUNNING" if any(btn.isChecked() for btn in self.maneuver_buttons.values()) else "IDLE",
                "timestamp": to_float(packet_dict.get("timestamp", time.time()), time.time()),
            }

        return {}

    def _apply_dashboard_telemetry(self, data: dict, source: str) -> None:
        """Atualiza os widgets principais com a telemetria recebida."""
        raw_load_cell = data.get("raw_load_cell")
        self.raw_load_cell_value.setText("--" if raw_load_cell is None else str(raw_load_cell))
        self.force_gauge.set_force_kg(data["pilot_force_kg"])

        pos_top = data.get("pos_top")
        pos_bottom = data.get("pos_bottom")
        is_calibrated = bool(data.get("is_calibrated", False))
        if pos_top is not None and pos_bottom is not None:
            self._calibration_pos_top = pos_top
            self._calibration_pos_bottom = pos_bottom
            self._is_calibrated = is_calibrated
            self._update_calibration_display()

        trim_hold = data["trim_hold"]
        self._set_state(self.trim_hold_lbl, "ok" if trim_hold else "off")
        self._set_state(self.trim_release_lbl, "ok" if not trim_hold else "off")

        beep_trim = data["beep_trim"]
        self._set_state(self.beep_up_lbl, "ok" if beep_trim == "UP" else "off")
        self._set_state(self.beep_down_lbl, "ok" if beep_trim == "DOWN" else "off")

        pa_active = any(btn.isChecked() for btn in self.maneuver_buttons.values())
        self._set_state(self.pa_active_lbl, "ok" if pa_active else "off")
        self._set_state(self.pa_override_lbl, "ok" if not pa_active else "off")

        self.udp_led.set_on(data["udp_connected"])
        self.usb_led.set_on(data["usb_connected"])

        has_failure = data["hydraulic_failure"]
        self.alert_lbl.setVisible(has_failure)
        if has_failure and not self.flash_timer.isActive():
            self.flash_timer.start()
        if not has_failure and self.flash_timer.isActive():
            self.flash_timer.stop()
            self.alert_lbl.setProperty("flash", "false")

        # Atualizar tiles de manobra com base no estado recebido
        active = data.get("maneuver_active", False)
        selected = data.get("selected_maneuver", "")

        for name, btn in self.maneuver_buttons.items():
            state = "active" if active and name == selected else "idle"
            if btn.property("runState") != state:
                btn.setProperty("runState", state)
                self._refresh_tile_style(btn)

            should_be_checked = active and name == selected
            if btn.isChecked() != should_be_checked:
                btn.blockSignals(True)
                btn.setChecked(should_be_checked)
                btn.blockSignals(False)

        # Atualizar pane_tile
        pane_on = data.get("hydraulic_failure", False)
        if self.pane_tile.isChecked() != pane_on:
            self.pane_tile.blockSignals(True)
            self.pane_tile.setChecked(pane_on)
            self.pane_tile.blockSignals(False)
        pane_state = "active" if pane_on else "idle"
        if self.pane_tile.property("runState") != pane_state:
            self.pane_tile.setProperty("runState", pane_state)
            self._refresh_tile_style(self.pane_tile)

        # Atualizar hint com status de manobra
        state_text_map = {
            "RUNNING": "Em execução",
            "COMPLETED": "Concluída",
            "ABORTED": "Abortada",
            "IDLE": "Pronta",
        }
        state_raw = data.get("maneuver_state", "IDLE")
        state_text = state_text_map.get(state_raw, state_raw)
        self.maneuver_hint.setText(f"{selected} | Status: {state_text}")

    def _smooth_calibrated_percent(self, new_percent: float) -> float:
        """Suaviza o percentual calibrado apenas para fins de exibição"""
        snap_margin = 0.5  # percentual; pequeno o suficiente para não mascarar movimento real

        if new_percent <= snap_margin:
            self._filtered_calibrated_percent = 0.0
            self._has_filtered_calibrated_percent = True
            return 0.0

        if new_percent >= 100.0 - snap_margin:
            self._filtered_calibrated_percent = 100.0
            self._has_filtered_calibrated_percent = True
            return 100.0

        deadband = self.joystick_reader.deadband_percent
        alpha = self.joystick_reader.smoothing_alpha

        if not self._has_filtered_calibrated_percent:
            self._filtered_calibrated_percent = new_percent
            self._has_filtered_calibrated_percent = True
            return new_percent

        delta = new_percent - self._filtered_calibrated_percent
        if abs(delta) <= deadband:
            return self._filtered_calibrated_percent

        self._filtered_calibrated_percent += delta * alpha
        return self._filtered_calibrated_percent

    def _on_joystick_position_changed(self, position_percent: float) -> None:
        # Mantido como fallback de exibição enquanto a calibragem do
        # Raspberry Pi ainda não chegou (usa min_raw/max_raw do ambiente).
        # Uma vez calibrado, _on_joystick_raw_position_changed assume a
        # exibição usando a faixa [pos_top, pos_bottom] real.
        if not self._is_calibrated:
            self.position_bar.setValue(int(max(0.0, min(100.0, position_percent)) * 10))
            self.position_display.setText(f"{position_percent:.1f}%")

    def _on_joystick_raw_position_changed(self, raw_position: int) -> None:
        self._joystick_raw_position = raw_position

        calibrated_percent = self._raw_position_to_calibrated_percent(raw_position)
        if calibrated_percent is not None:
            smoothed_percent = self._smooth_calibrated_percent(calibrated_percent)
            self.position_bar.setValue(int(max(0.0, min(100.0, smoothed_percent)) * 10))
            self.position_display.setText(f"{smoothed_percent:.1f}%")

    def _on_joystick_connection_changed(self, connected: bool) -> None:
        self.joystick_led.set_on(connected)
        if connected:
            self.joystick_status.setText(f"{self.joystick_reader.device_path} ativo")
        else:
            self.joystick_status.setText(
                f"Joystick ausente ({self.joystick_reader.device_path}) — "
                "calibragem falha se transdutor nao variar"
            )

    def _on_joystick_error(self, error_msg: str) -> None:
        self.logger.warning(f"Joystick: {error_msg}")
        self.joystick_status.setText("Falha ao ler joystick")

    def _on_udp_packet_received(self, packet_dict: dict) -> None:
        """Handler para pacotes UDP recebidos do Raspberry Pi."""
        self._udp_packet_num = getattr(self, "_udp_packet_num", 0) + 1
        self._last_udp_packet_time = time.time()

        # Atualizar contador
        self.udp_packet_count.setText(f"Pacotes recebidos: {self._udp_packet_num}")

        # Extrair informações do pacote
        sender = packet_dict.get("sender_address", "?")
        port = packet_dict.get("sender_port", "?")
        fmt = packet_dict.get("parse_format", "?")
        length = packet_dict.get("raw_length", 0)

        # Direcionar comandos para o IP que está enviando telemetria
        if isinstance(sender, str) and sender and sender != "?" and sender != self._command_target_host:
            self._command_target_host = sender
            self.command_sender.set_target(receiver_host=self._command_target_host, receiver_port=self._command_target_port)
            self.logger.info(f"Destino de comandos ajustado para {self._command_target_host}:{self._command_target_port}")
            self.udp_endpoints_info.setText(
                f"Escutando telemetria em 0.0.0.0:5006 | Enviando comandos para {self._command_target_host}:{self._command_target_port}"
            )

        self.udp_data_info.setText(
            f"Sensores: {sender}:{port} | Formato: {fmt} | Tamanho: {length} bytes"
        )

        # Mostrar dados parseados
        parsed = packet_dict.get("parsed_data", {})
        if parsed:
            display_text = str(parsed)
            # Limitar a exibição às últimas 2 linhas
            if len(display_text) > 200:
                display_text = "..." + display_text[-200:]
            self.udp_last_packet.setText(display_text)

        telemetry = self._extract_telemetry(packet_dict)
        if telemetry:
            self._apply_dashboard_telemetry(telemetry, source="udp")

    def _on_udp_error(self, error_msg: str) -> None:
        """Handler para erros UDP."""
        self.logger.error(f"UDP Error: {error_msg}")
        self.udp_data_info.setText(f"ERRO: {error_msg}")
        self.udp_status_led.set_on(False)

    def _on_udp_connection_changed(self, connected: bool) -> None:
        """Handler para mudanças no status de conexão UDP."""
        self.udp_status_led.set_on(connected)
        if connected:
            self.udp_data_info.setText("Servidor UDP ativo - aguardando dados do Raspberry Pi em 5006...")
        else:
            self.udp_data_info.setText("Servidor UDP desconectado")

    def _on_command_sent(self, command_data: dict) -> None:
        """Handler quando comando é enviado para Raspberry Pi."""
        cmd_type = command_data.get("command_type", "unknown")
        if cmd_type != "control_stream":
            self.logger.info(f"Comando enviado: {cmd_type} - {command_data}")
        if cmd_type == "maneuver":
            maneuver_name = command_data.get("maneuver_name", "?")
            self.maneuver_hint.setText(f"Comando enviado: {maneuver_name}")

    def _on_command_error(self, error_msg: str) -> None:
        """Handler para erros ao enviar comandos."""
        self.logger.error(f"Command Error: {error_msg}")
        self.maneuver_hint.setText(f"Erro UDP comando: {error_msg}")
        self.udp_data_info.setText(f"Falha ao enviar comando UDP: {error_msg}")

    def closeEvent(self, event) -> None:
        self.joystick_reader.stop()
        self.mock_raspberry.stop()
        if self.control_timer.isActive():
            self.control_timer.stop()
        self.command_sender.stop_stream()
        self.udp_receiver.stop()
        super().closeEvent(event)


def run_app() -> None:
    app = QApplication([])
    app.setStyleSheet(DASHBOARD_QSS)
    window = SccaDashboard()
    window.show()
    app.exec()