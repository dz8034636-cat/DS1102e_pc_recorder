"""
DS1102E PC Recorder v3
=======================
A complete Python GUI for RIGOL DS1102E / DS1000E-series scopes:
- USB/VISA discovery and SCPI console
- CH1 / CH2 independent setup pages
- Timebase, acquisition, memory and edge-trigger controls
- Live two-channel display
- PC-side continuous display-frame recording to SQLite
- Automatic CH2 voltage-loss events, manual event marks, replay and CSV export

Install in the SAME PyCharm virtual environment:
    .\.venv\Scripts\python.exe -m pip install PySide6 pyqtgraph pyvisa numpy

For DS1102E USB communication on Windows, install NI-VISA and use the rear USB
DEVICE (USB-B) connector of the scope. The front USB-A port is for USB storage.

Electrical safety:
The ground clip of a normal scope probe is earth-referenced. Do NOT connect it to
an unknown live mains phase or floating power circuit. Use a suitably rated
differential probe, isolated transducer, or an approved PT/VT secondary point.

Important data limitation:
During RUN this program records display-frame waveform data returned by SCPI. It is
useful for delayed trend analysis and PC-side recording, but it is not a lossless
high-speed DAQ or a substitute for a professional fault recorder.
"""
from __future__ import annotations

import csv
import json
import sqlite3
import sys
import traceback
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyvisa
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSlider, QSpinBox, QSplitter, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

APP_NAME = "DS1102E PC Recorder v3"
ADC_CENTER_CODE = 130.0
ADC_CODES_PER_DIV = 25.0
HORIZONTAL_DIVISIONS = 12.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_ieee_block(raw: bytes) -> np.ndarray:
    """Decode an IEEE-488.2 definite-length binary block returned by :WAV:DATA?."""
    raw = raw.lstrip(b"\r\n")
    if not raw.startswith(b"#"):
        raise ValueError(f"Expected IEEE binary block; received {raw[:60]!r}")
    if len(raw) < 3:
        raise ValueError("Incomplete IEEE binary-block header")
    digits = int(chr(raw[1]))
    if digits == 0:
        return np.frombuffer(raw[2:], dtype=np.uint8).copy()
    header_end = 2 + digits
    if len(raw) < header_end:
        raise ValueError("Incomplete IEEE binary-block length")
    length = int(raw[2:header_end].decode("ascii"))
    payload = raw[header_end:header_end + length]
    if len(payload) != length:
        raise ValueError(f"Incomplete waveform payload: {len(payload)}/{length} bytes")
    return np.frombuffer(payload, dtype=np.uint8).copy()


def voltage_from_display_codes(raw: np.ndarray, scale_v_div: float, offset_v: float) -> np.ndarray:
    """DS1000E display-frame byte-code approximation.

    Raw byte data and every relevant scope setting are stored in SQLite. Thus the
    waveform can be recalculated later if a particular firmware needs correction.
    """
    return (raw.astype(np.float64) - ADC_CENTER_CODE) * scale_v_div / ADC_CODES_PER_DIV - offset_v


def build_time_axis(points: int, timebase_s_div: float, time_offset_s: float) -> np.ndarray:
    return np.linspace(
        -HORIZONTAL_DIVISIONS * timebase_s_div / 2,
        HORIZONTAL_DIVISIONS * timebase_s_div / 2,
        points,
        endpoint=False,
    ) - time_offset_s


def calculate_rms(values: np.ndarray) -> float:
    return float(np.sqrt(np.mean(values * values))) if len(values) else float("nan")


def estimate_frequency(values: np.ndarray, time_s: np.ndarray) -> float:
    """Estimate frequency from positive-going, mean-centred zero crossings."""
    if len(values) < 4:
        return float("nan")
    y = values - np.mean(values)
    crossings = np.where((y[:-1] < 0) & (y[1:] >= 0))[0]
    if len(crossings) < 2:
        return float("nan")
    fraction = crossings + (-y[crossings]) / (y[crossings + 1] - y[crossings])
    crossing_times = np.interp(fraction, np.arange(len(time_s)), time_s)
    period = float(np.median(np.diff(crossing_times)))
    return 1.0 / period if period > 0 else float("nan")


@dataclass
class FrameMeta:
    timestamp_utc: str
    timebase_s_div: float
    time_offset_s: float
    ch1_scale_v_div: float
    ch1_offset_v: float
    ch2_scale_v_div: float
    ch2_offset_v: float
    sample_rate_sps: float
    trigger_status: str


class SessionDatabase:
    """Self-contained SQLite recording file; no database server is required."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_info (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                meta_json TEXT NOT NULL,
                ch1_raw BLOB NOT NULL,
                ch2_raw BLOB NOT NULL,
                ch1_rms REAL,
                ch2_rms REAL,
                ch1_hz REAL,
                ch2_hz REAL,
                status TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_frames_time ON frames(timestamp_utc);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                frame_id INTEGER,
                event_type TEXT NOT NULL,
                detail TEXT
            );
            """
        )
        self.connection.commit()

    def set_info(self, **data) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO session_info(key,value_json) VALUES (?,?)",
            [(key, json.dumps(value, ensure_ascii=False)) for key, value in data.items()],
        )
        self.connection.commit()

    def add_frame(self, meta: FrameMeta, ch1: np.ndarray, ch2: np.ndarray, metrics: dict, status: str) -> int:
        cursor = self.connection.execute(
            """INSERT INTO frames(
                timestamp_utc,meta_json,ch1_raw,ch2_raw,ch1_rms,ch2_rms,ch1_hz,ch2_hz,status
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                meta.timestamp_utc,
                json.dumps(asdict(meta)),
                zlib.compress(ch1.tobytes(), 1),
                zlib.compress(ch2.tobytes(), 1),
                metrics["ch1_rms"], metrics["ch2_rms"],
                metrics["ch1_hz"], metrics["ch2_hz"], status,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def add_event(self, event_type: str, detail: str, frame_id: int | None = None) -> None:
        self.connection.execute(
            "INSERT INTO events(timestamp_utc,frame_id,event_type,detail) VALUES (?,?,?,?)",
            (utc_now(), frame_id, event_type, detail),
        )
        self.connection.commit()

    def frame_count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])

    def get_frame(self, index: int):
        row = self.connection.execute(
            """SELECT id,timestamp_utc,meta_json,ch1_raw,ch2_raw,ch1_rms,ch2_rms,ch1_hz,ch2_hz,status
               FROM frames ORDER BY id LIMIT 1 OFFSET ?""",
            (index,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "timestamp": row[1],
            "meta": FrameMeta(**json.loads(row[2])),
            "ch1": np.frombuffer(zlib.decompress(row[3]), dtype=np.uint8).copy(),
            "ch2": np.frombuffer(zlib.decompress(row[4]), dtype=np.uint8).copy(),
            "metrics": {"ch1_rms": row[5], "ch2_rms": row[6], "ch1_hz": row[7], "ch2_hz": row[8]},
            "status": row[9],
        }

    def export_metrics_csv(self, path: Path) -> None:
        rows = self.connection.execute(
            "SELECT id,timestamp_utc,ch1_rms,ch2_rms,ch1_hz,ch2_hz,status FROM frames ORDER BY id"
        )
        with open(path, "w", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            writer.writerow(["frame_id", "timestamp_utc", "ch1_rms_v", "ch2_rms_v", "ch1_hz", "ch2_hz", "status"])
            writer.writerows(rows)

    def close(self) -> None:
        self.connection.close()


class ScopeWorker(QObject):
    """All VISA operations live in a dedicated Qt thread."""

    resources_found = Signal(tuple)
    connected = Signal(str)
    disconnected = Signal()
    waveform_ready = Signal(object, object, object)
    log = Signal(str)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.resource_manager = None
        self.scope = None
        self.timer = None
        self.reading = False

    @Slot()
    def scan_resources(self) -> None:
        try:
            rm = pyvisa.ResourceManager()
            resources = tuple(rm.list_resources())
            rm.close()
            self.resources_found.emit(resources)
        except Exception as exc:
            self.error.emit(f"VISA 扫描失败：{exc}")

    @Slot(str, int)
    def connect_scope(self, resource: str, interval_ms: int) -> None:
        try:
            self.disconnect_scope()
            self.resource_manager = pyvisa.ResourceManager()
            self.scope = self.resource_manager.open_resource(resource)
            self.scope.timeout = 8000
            self.scope.write_termination = "\n"
            self.scope.read_termination = "\n"
            identity = self.scope.query("*IDN?").strip()
            self.scope.write(":WAV:POIN:MODE NORM")
            self.timer = QTimer(self)
            self.timer.setInterval(max(100, interval_ms))
            self.timer.timeout.connect(self.read_waveforms)
            self.connected.emit(identity)
            self.log.emit(f"已连接：{identity}")
        except Exception:
            self.error.emit("连接示波器失败：\n" + traceback.format_exc())
            self.disconnect_scope()

    def _write(self, command: str, log_command: bool = True) -> None:
        if not self.scope:
            raise RuntimeError("未连接示波器")
        self.scope.write(command)
        if log_command:
            self.log.emit(">> " + command)

    @Slot(str)
    def send_scpi(self, command: str) -> None:
        try:
            command = command.strip()
            if not command:
                return
            if command.endswith("?"):
                response = self.scope.query(command).strip()
                self.log.emit(">> " + command)
                self.log.emit("<< " + response)
            else:
                self._write(command)
        except Exception as exc:
            self.error.emit(f"SCPI 命令失败：{command}\n{exc}")

    @Slot()
    def start_stream(self) -> None:
        try:
            self._write(":RUN")
            if self.timer:
                self.timer.start()
            self.log.emit("实时数据回传已启动。")
        except Exception as exc:
            self.error.emit(f"无法启动实时采集：{exc}")

    @Slot()
    def stop_stream(self) -> None:
        if self.timer:
            self.timer.stop()
        try:
            if self.scope:
                self._write(":STOP")
            self.log.emit("实时数据回传已停止。")
        except Exception as exc:
            self.log.emit(f"发送 STOP 时出现异常：{exc}")

    @Slot(int)
    def set_interval(self, interval_ms: int) -> None:
        if self.timer:
            self.timer.setInterval(max(100, interval_ms))

    @Slot(object)
    def apply_settings(self, commands: list[str]) -> None:
        """Apply every command independently so one unsupported command does not abort all setup."""
        if not self.scope:
            self.error.emit("尚未连接示波器。")
            return
        failures = []
        for command in commands:
            try:
                self._write(command)
            except Exception as exc:
                failures.append(f"{command}: {exc}")
        if failures:
            self.log.emit("部分设置未成功：\n" + "\n".join(failures))
        else:
            self.log.emit("全部设置已发送。")

    def _query_float(self, command: str, default: float = 0.0) -> float:
        try:
            return float(self.scope.query(command).strip())
        except Exception:
            return default

    def _read_channel(self, channel: str) -> np.ndarray:
        self.scope.write(f":WAV:DATA? {channel}")
        return parse_ieee_block(self.scope.read_raw())

    @Slot()
    def read_waveforms(self) -> None:
        if not self.scope or self.reading:
            return
        self.reading = True
        try:
            ch1 = self._read_channel("CHAN1")
            ch2 = self._read_channel("CHAN2")
            meta = FrameMeta(
                timestamp_utc=utc_now(),
                timebase_s_div=self._query_float(":TIM:SCAL?"),
                time_offset_s=self._query_float(":TIM:OFFS?"),
                ch1_scale_v_div=self._query_float(":CHAN1:SCAL?"),
                ch1_offset_v=self._query_float(":CHAN1:OFFS?"),
                ch2_scale_v_div=self._query_float(":CHAN2:SCAL?"),
                ch2_offset_v=self._query_float(":CHAN2:OFFS?"),
                sample_rate_sps=self._query_float(":ACQ:SAMP? CHAN1"),
                trigger_status=self.scope.query(":TRIG:STAT?").strip(),
            )
            self.waveform_ready.emit(ch1, ch2, meta)
        except Exception as exc:
            if self.timer:
                self.timer.stop()
            self.error.emit(f"读取波形失败，实时采集已停止：{exc}")
        finally:
            self.reading = False

    @Slot()
    def disconnect_scope(self) -> None:
        if self.timer:
            self.timer.stop()
            self.timer.deleteLater()
            self.timer = None
        if self.scope:
            try:
                self.scope.close()
            except Exception:
                pass
            self.scope = None
        if self.resource_manager:
            try:
                self.resource_manager.close()
            except Exception:
                pass
            self.resource_manager = None
        self.disconnected.emit()


class MainWindow(QMainWindow):
    scan_request = Signal()
    connect_request = Signal(str, int)
    start_request = Signal()
    stop_request = Signal()
    disconnect_request = Signal()
    command_request = Signal(str)
    settings_request = Signal(object)
    interval_request = Signal(int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1600, 960)
        self.recording_db: SessionDatabase | None = None
        self.playback_db: SessionDatabase | None = None
        self.recording = False
        self.previous_status = None
        self._build_ui()
        self._build_worker()
        self.scan_request.emit()

    # ---------- user interface ----------
    def _build_ui(self) -> None:
        pg.setConfigOptions(antialias=True, background="#11151C", foreground="#D8DEE9")
        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        main_layout.addWidget(self._connection_panel())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._plot_panel())
        splitter.addWidget(self._right_panel())
        splitter.setSizes([1060, 500])
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        main_layout.addWidget(splitter, 1)

    def _connection_panel(self) -> QWidget:
        panel = QFrame()
        panel.setFrameShape(QFrame.StyledPanel)
        grid = QGridLayout(panel)
        grid.setColumnStretch(1, 1)

        self.resource_combo = QComboBox()
        self.status_label = QLabel("未连接")
        self.status_label.setStyleSheet("color:#C0392B; font-weight:bold;")
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(100, 10000)
        self.interval_spin.setValue(250)
        self.interval_spin.setSuffix(" ms / 帧")

        scan = QPushButton("扫描 VISA")
        connect = QPushButton("连接")
        disconnect = QPushButton("断开")
        scan.clicked.connect(lambda: self.scan_request.emit())
        connect.clicked.connect(self.connect_scope)
        disconnect.clicked.connect(lambda: self.disconnect_request.emit())
        self.interval_spin.valueChanged.connect(lambda value: self.interval_request.emit(value))

        grid.addWidget(QLabel("VISA 资源"), 0, 0)
        grid.addWidget(self.resource_combo, 0, 1, 1, 3)
        grid.addWidget(scan, 0, 4)
        grid.addWidget(connect, 0, 5)
        grid.addWidget(disconnect, 0, 6)
        grid.addWidget(QLabel("状态"), 1, 0)
        grid.addWidget(self.status_label, 1, 1, 1, 3)
        grid.addWidget(QLabel("PC 轮询周期"), 1, 4)
        grid.addWidget(self.interval_spin, 1, 5, 1, 2)
        return panel

    def _plot_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot = pg.PlotWidget(title="实时 / 回放波形")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.addLegend()
        self.ch1_curve = self.plot.plot(pen=pg.mkPen("#FFD54F", width=1.4), name="CH1")
        self.ch2_curve = self.plot.plot(pen=pg.mkPen("#40C4FF", width=1.4), name="CH2")
        layout.addWidget(self.plot, 1)

        self.metrics_label = QLabel("CH1 RMS: -- V     CH1 f: -- Hz     CH2 RMS: -- V     CH2 f: -- Hz")
        self.metrics_label.setStyleSheet("font: 14px Consolas; padding:7px;")
        layout.addWidget(self.metrics_label)
        return panel

    def _right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._scope_control_tab(), "示波器控制")
        tabs.addTab(self._recording_tab(), "记录与回放")
        tabs.addTab(self._console_tab(), "SCPI 控制台")
        layout.addWidget(tabs, 1)

        layout.addWidget(QLabel("运行日志"))
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(170)
        layout.addWidget(self.log_box)
        return panel

    def _scope_control_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)

        notice = QLabel(
            "设置已分为独立页面，避免控件拥挤。所有修改均需在“触发与采集”页面点击“应用全部设置”。"
        "实际接入 DS1102E 前可以自由调整软件中的数值。"
        )
        notice.setWordWrap(True)
        notice.setStyleSheet("background:#EAF3FF; color:#1F4E79; border:1px solid #9CC2E5; border-radius:5px; padding:8px;")
        layout.addWidget(notice)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._quick_page(), "快捷控制")
        tabs.addTab(self._channel_page(1), "CH1")
        tabs.addTab(self._channel_page(2), "CH2")
        tabs.addTab(self._trigger_acquisition_page(), "触发与采集")
        layout.addWidget(tabs, 1)
        return page

    def _quick_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel("快捷操作")
        title.setStyleSheet("font-size:17px; font-weight:bold;")
        layout.addWidget(title)
        description = QLabel("下列按钮直接向仪器发送 SCPI 命令；所有命令与返回结果都会显示于运行日志。")
        description.setWordWrap(True)
        layout.addWidget(description)

        control_group = QGroupBox("运行控制")
        grid = QGridLayout(control_group)
        buttons = [
            ("自动设置", ":AUTO", "由示波器自动优化显示"),
            ("运行", ":RUN", "连续采集"),
            ("停止", ":STOP", "停止采集、冻结画面"),
            ("单次采集", ":TRIG:EDGE:SWE SING;:RUN", "等待一次触发后停止"),
            ("强制触发", ":FORC", "强制完成当前触发"),
            ("清除显示", ":DISP:CLE", "清除显示余辉"),
        ]
        for index, (text, command, tooltip) in enumerate(buttons):
            button = QPushButton(text)
            button.setMinimumHeight(42)
            button.setToolTip(tooltip)
            button.clicked.connect(lambda _=False, c=command: self.send_multi(c))
            grid.addWidget(button, index // 2, index % 2)
        layout.addWidget(control_group)

        query_group = QGroupBox("快速查询")
        form = QFormLayout(query_group)
        idn = QPushButton("读取仪器信息 (*IDN?)")
        trig = QPushButton("读取触发状态")
        error = QPushButton("读取系统错误")
        idn.clicked.connect(lambda: self.command_request.emit("*IDN?"))
        trig.clicked.connect(lambda: self.command_request.emit(":TRIG:STAT?"))
        error.clicked.connect(lambda: self.command_request.emit(":SYST:ERR?"))
        form.addRow(idn)
        form.addRow(trig)
        form.addRow(error)
        layout.addWidget(query_group)
        layout.addStretch(1)
        return page

    def _channel_page(self, channel: int) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)

        if channel == 1:
            self.ch1_enabled = QCheckBox("启用 CH1 显示")
            self.ch1_enabled.setChecked(True)
            self.ch1_coupling = self._combo(["DC", "AC", "GND"])
            self.ch1_probe = self._double_spin(100, 0.001, 10000, " X")
            self.ch1_scale = self._double_spin(20, 0.002, 1000, " V/div")
            self.ch1_offset = self._double_spin(0, -10000, 10000, " V")
            enabled, coupling, probe, scale, offset = self.ch1_enabled, self.ch1_coupling, self.ch1_probe, self.ch1_scale, self.ch1_offset
            label = "CH1"
        else:
            self.ch2_enabled = QCheckBox("启用 CH2 显示")
            self.ch2_enabled.setChecked(True)
            self.ch2_coupling = self._combo(["DC", "AC", "GND"])
            self.ch2_probe = self._double_spin(100, 0.001, 10000, " X")
            self.ch2_scale = self._double_spin(20, 0.002, 1000, " V/div")
            self.ch2_offset = self._double_spin(0, -10000, 10000, " V")
            enabled, coupling, probe, scale, offset = self.ch2_enabled, self.ch2_coupling, self.ch2_probe, self.ch2_scale, self.ch2_offset
            label = "CH2"

        title = QLabel(f"{label} 垂直系统")
        title.setStyleSheet("font-size:17px; font-weight:bold;")
        layout.addWidget(title)
        warning = QLabel("探头倍率必须与实际探头或差分探头的实体倍率一致。普通接地探头不可任意接入带电交流系统。")
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#8A3B00; background:#FFF4E5; padding:8px; border-radius:4px;")
        layout.addWidget(warning)

        group = QGroupBox("通道设置")
        form = QFormLayout(group)
        form.setVerticalSpacing(12)
        form.addRow(enabled)
        form.addRow("输入耦合", coupling)
        form.addRow("探头倍率", probe)
        form.addRow("垂直量程", scale)
        form.addRow("垂直偏置", offset)
        layout.addWidget(group)
        help_text = QLabel("DC：保留直流和交流分量。\nAC：滤除直流分量，适合交流观察。\nGND：通道接地，仅用于检查基线。")
        help_text.setStyleSheet("color:#555; padding:6px;")
        layout.addWidget(help_text)
        layout.addStretch(1)
        return scroll

    def _trigger_acquisition_page(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("时间基准、采集和边沿触发")
        title.setStyleSheet("font-size:17px; font-weight:bold;")
        layout.addWidget(title)

        horizontal = QGroupBox("水平系统")
        form_h = QFormLayout(horizontal)
        form_h.setVerticalSpacing(12)
        self.timebase = self._double_spin(0.1, 2e-9, 50, " s/div", decimals=9)
        self.time_offset = self._double_spin(0, -1000, 1000, " s")
        form_h.addRow("时基", self.timebase)
        form_h.addRow("水平偏置", self.time_offset)
        layout.addWidget(horizontal)

        acquisition = QGroupBox("采集系统")
        form_a = QFormLayout(acquisition)
        form_a.setVerticalSpacing(12)
        self.acquisition_type = self._combo(["NORM", "PEAK", "AVER"])
        self.memory_depth = self._combo(["NORM", "LONG"])
        form_a.addRow("采集类型", self.acquisition_type)
        form_a.addRow("存储深度", self.memory_depth)
        layout.addWidget(acquisition)

        trigger = QGroupBox("边沿触发")
        form_t = QFormLayout(trigger)
        form_t.setVerticalSpacing(12)
        self.trigger_source = self._combo(["CHAN1", "CHAN2", "EXT", "ACLINE"])
        self.trigger_slope = self._combo(["POS", "NEG"])
        self.trigger_sweep = self._combo(["AUTO", "NORM", "SING"])
        self.trigger_level = self._double_spin(0, -6000, 6000, " V")
        form_t.addRow("触发源", self.trigger_source)
        form_t.addRow("触发边沿", self.trigger_slope)
        form_t.addRow("触发扫描", self.trigger_sweep)
        form_t.addRow("触发电平", self.trigger_level)
        layout.addWidget(trigger)

        apply = QPushButton("应用全部设置到 DS1102E")
        apply.setMinimumHeight(46)
        apply.setStyleSheet("font-weight:bold; background:#1976D2; color:white;")
        apply.clicked.connect(self.apply_all_settings)
        layout.addWidget(apply)
        layout.addStretch(1)
        return scroll

    def _recording_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)

        recording = QGroupBox("PC 端连续录波")
        form = QFormLayout(recording)
        form.setVerticalSpacing(10)
        self.session_name = QLineEdit(datetime.now().strftime("%Y%m%d_%H%M%S_AC_Transfer"))
        self.ch1_name = QLineEdit("CH1")
        self.ch2_name = QLineEdit("CH2")
        self.loss_threshold = self._double_spin(23, 0, 100000, " V RMS")
        self.manual_note = QLineEdit("交流切换操作")
        begin = QPushButton("开始实时显示并记录")
        stop = QPushButton("停止记录")
        mark = QPushButton("写入人工事件标记")
        begin.clicked.connect(self.begin_recording)
        stop.clicked.connect(self.stop_recording)
        mark.clicked.connect(self.add_manual_event)
        form.addRow("会话名称", self.session_name)
        form.addRow("CH1 工程名称", self.ch1_name)
        form.addRow("CH2 工程名称", self.ch2_name)
        form.addRow("CH2 失压阈值", self.loss_threshold)
        form.addRow("人工事件内容", self.manual_note)
        form.addRow(begin)
        form.addRow(stop)
        form.addRow(mark)
        layout.addWidget(recording)

        playback = QGroupBox("数据回放与趋势导出")
        p_layout = QVBoxLayout(playback)
        open_button = QPushButton("打开 SQLite 录波会话")
        self.playback_slider = QSlider(Qt.Horizontal)
        self.playback_slider.setRange(0, 0)
        self.playback_info = QLabel("尚未打开会话")
        export_button = QPushButton("导出 RMS / 频率趋势 CSV")
        open_button.clicked.connect(self.open_playback)
        self.playback_slider.valueChanged.connect(self.show_playback_frame)
        export_button.clicked.connect(self.export_csv)
        p_layout.addWidget(open_button)
        p_layout.addWidget(self.playback_slider)
        p_layout.addWidget(self.playback_info)
        p_layout.addWidget(export_button)
        layout.addWidget(playback)
        layout.addStretch(1)
        return page

    def _console_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        instruction = QLabel("输入单条 SCPI 命令。查询命令必须以 ? 结尾，返回值显示在运行日志。")
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        self.scpi_input = QLineEdit("*IDN?")
        send = QPushButton("发送 SCPI")
        send.clicked.connect(lambda: self.command_request.emit(self.scpi_input.text().strip()))
        layout.addWidget(self.scpi_input)
        layout.addWidget(send)
        examples = QTextEdit()
        examples.setReadOnly(True)
        examples.setPlainText(
            "常用示例：\n"
            "*IDN?\n"
            ":TRIG:STAT?\n"
            ":MEAS:VRMS? CHAN1\n"
            ":MEAS:FREQ? CHAN1\n"
            ":CHAN1:DISP ON\n"
            ":CHAN1:COUP AC\n"
            ":ACQ:TYPE PEAK\n"
            ":WAV:DATA? CHAN1"
        )
        layout.addWidget(QLabel("SCPI 示例"))
        layout.addWidget(examples, 1)
        return page

    def _combo(self, values: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.addItems(values)
        return combo

    def _double_spin(self, value: float, minimum: float, maximum: float, suffix: str = "", decimals: int = 4) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(value)
        box.setDecimals(decimals)
        box.setSuffix(suffix)
        box.setSingleStep(max(abs(value) / 10, 0.001))
        return box

    # ---------- worker wiring ----------
    def _build_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = ScopeWorker()
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.start()

        self.scan_request.connect(self.worker.scan_resources)
        self.connect_request.connect(self.worker.connect_scope)
        self.start_request.connect(self.worker.start_stream)
        self.stop_request.connect(self.worker.stop_stream)
        self.disconnect_request.connect(self.worker.disconnect_scope)
        self.command_request.connect(self.worker.send_scpi)
        self.settings_request.connect(self.worker.apply_settings)
        self.interval_request.connect(self.worker.set_interval)

        self.worker.resources_found.connect(self.on_resources_found)
        self.worker.connected.connect(self.on_connected)
        self.worker.disconnected.connect(self.on_disconnected)
        self.worker.waveform_ready.connect(self.on_waveform)
        self.worker.log.connect(self.append_log)
        self.worker.error.connect(self.show_error)

    # ---------- instrument control ----------
    def connect_scope(self) -> None:
        resource = self.resource_combo.currentText().strip()
        if not resource:
            QMessageBox.warning(
                self,
                APP_NAME,
                "未发现 VISA 资源。请在连接示波器后使用后方 USB Device 口，并点击“扫描 VISA”。",
            )
            return
        self.connect_request.emit(resource, self.interval_spin.value())

    def send_multi(self, commands: str) -> None:
        for command in commands.split(";"):
            command = command.strip()
            if command:
                self.command_request.emit(command)

    def apply_all_settings(self) -> None:
        commands = [
            f":TIM:SCAL {self.timebase.value()}",
            f":TIM:OFFS {self.time_offset.value()}",
            f":ACQ:TYPE {self.acquisition_type.currentText()}",
            f":ACQ:MEMD {self.memory_depth.currentText()}",
            f":CHAN1:DISP {'ON' if self.ch1_enabled.isChecked() else 'OFF'}",
            f":CHAN2:DISP {'ON' if self.ch2_enabled.isChecked() else 'OFF'}",
            f":CHAN1:COUP {self.ch1_coupling.currentText()}",
            f":CHAN2:COUP {self.ch2_coupling.currentText()}",
            f":CHAN1:PROB {self.ch1_probe.value()}",
            f":CHAN2:PROB {self.ch2_probe.value()}",
            f":CHAN1:SCAL {self.ch1_scale.value()}",
            f":CHAN2:SCAL {self.ch2_scale.value()}",
            f":CHAN1:OFFS {self.ch1_offset.value()}",
            f":CHAN2:OFFS {self.ch2_offset.value()}",
            ":TRIG:MODE EDGE",
            f":TRIG:EDGE:SOUR {self.trigger_source.currentText()}",
            f":TRIG:EDGE:SLOP {self.trigger_slope.currentText()}",
            f":TRIG:EDGE:SWE {self.trigger_sweep.currentText()}",
            f":TRIG:EDGE:LEV {self.trigger_level.value()}",
        ]
        self.settings_request.emit(commands)

    # ---------- recording ----------
    def begin_recording(self) -> None:
        if self.recording_db:
            QMessageBox.information(self, APP_NAME, "当前已经在记录。")
            return
        name = self.session_name.text().strip() or datetime.now().strftime("%Y%m%d_%H%M%S_session")
        safe_name = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
        folder = Path.cwd() / "sessions"
        folder.mkdir(exist_ok=True)
        path = folder / f"{safe_name}.sqlite"
        if path.exists():
            QMessageBox.warning(self, APP_NAME, "该会话名称已经存在，请修改会话名称后重试。")
            return
        try:
            self.recording_db = SessionDatabase(path)
            self.recording_db.set_info(
                application=APP_NAME,
                created_utc=utc_now(),
                purpose="PC-side DS1102E display-frame recording",
                ch1_name=self.ch1_name.text(),
                ch2_name=self.ch2_name.text(),
                ch2_loss_threshold_v_rms=self.loss_threshold.value(),
                configured_poll_interval_ms=self.interval_spin.value(),
            )
            self.recording = True
            self.previous_status = None
            self.start_request.emit()
            self.append_log("开始 PC 端连续录波：" + str(path))
        except Exception as exc:
            self.recording_db = None
            QMessageBox.critical(self, APP_NAME, f"无法创建 SQLite 会话：\n{exc}")

    def stop_recording(self) -> None:
        self.stop_request.emit()
        self.recording = False
        if self.recording_db:
            count = self.recording_db.frame_count()
            self.recording_db.set_info(stopped_utc=utc_now(), frame_count=count)
            self.recording_db.close()
            self.recording_db = None
            self.append_log(f"录波会话已停止，共保存 {count} 帧。")

    def add_manual_event(self) -> None:
        if not self.recording_db:
            QMessageBox.information(self, APP_NAME, "请先开始记录，才能写入人工事件。")
            return
        detail = self.manual_note.text().strip() or "MANUAL_MARK"
        self.recording_db.add_event("MANUAL_MARK", detail)
        self.append_log("人工事件：" + detail)

    # ---------- display and analysis ----------
    @Slot(object, object, object)
    def on_waveform(self, ch1_raw: np.ndarray, ch2_raw: np.ndarray, meta: FrameMeta) -> None:
        try:
            t1 = build_time_axis(len(ch1_raw), meta.timebase_s_div, meta.time_offset_s)
            t2 = build_time_axis(len(ch2_raw), meta.timebase_s_div, meta.time_offset_s)
            v1 = voltage_from_display_codes(ch1_raw, meta.ch1_scale_v_div, meta.ch1_offset_v)
            v2 = voltage_from_display_codes(ch2_raw, meta.ch2_scale_v_div, meta.ch2_offset_v)
            metrics = {
                "ch1_rms": calculate_rms(v1),
                "ch2_rms": calculate_rms(v2),
                "ch1_hz": estimate_frequency(v1, t1),
                "ch2_hz": estimate_frequency(v2, t2),
            }
            self.draw_waveforms(t1, v1, t2, v2, meta.timestamp_utc, metrics, "LIVE")

            if self.recording and self.recording_db:
                status = "VOLTAGE_LOSS" if metrics["ch2_rms"] < self.loss_threshold.value() else "NORMAL"
                frame_id = self.recording_db.add_frame(meta, ch1_raw, ch2_raw, metrics, status)
                if status != self.previous_status:
                    self.recording_db.add_event(status, f"CH2 RMS={metrics['ch2_rms']:.3f} V", frame_id)
                    self.append_log(f"自动事件：{status}；CH2 RMS={metrics['ch2_rms']:.3f} V")
                    self.previous_status = status
        except Exception as exc:
            self.append_log(f"帧分析失败：{exc}")

    def draw_waveforms(self, t1, v1, t2, v2, timestamp: str, metrics: dict, mode: str) -> None:
        self.ch1_curve.setData(t1, v1)
        self.ch2_curve.setData(t2, v2)
        self.plot.setTitle(f"{mode} | {timestamp}")
        self.metrics_label.setText(
            f"CH1 RMS: {metrics['ch1_rms']:.3f} V     CH1 f: {metrics['ch1_hz']:.3f} Hz     "
            f"CH2 RMS: {metrics['ch2_rms']:.3f} V     CH2 f: {metrics['ch2_hz']:.3f} Hz"
        )

    # ---------- playback ----------
    def open_playback(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开 SQLite 录波会话", str(Path.cwd() / "sessions"), "SQLite (*.sqlite *.db)")
        if not path:
            return
        try:
            if self.playback_db:
                self.playback_db.close()
            self.playback_db = SessionDatabase(Path(path))
            count = self.playback_db.frame_count()
            self.playback_slider.blockSignals(True)
            self.playback_slider.setRange(0, max(0, count - 1))
            self.playback_slider.setValue(0)
            self.playback_slider.blockSignals(False)
            self.playback_info.setText(f"{Path(path).name}：共 {count} 帧")
            if count:
                self.show_playback_frame(0)
            self.append_log("已打开回放会话：" + Path(path).name)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"无法打开录波会话：\n{exc}")

    def show_playback_frame(self, index: int) -> None:
        if not self.playback_db:
            return
        frame = self.playback_db.get_frame(index)
        if not frame:
            return
        meta = frame["meta"]
        ch1, ch2 = frame["ch1"], frame["ch2"]
        t1 = build_time_axis(len(ch1), meta.timebase_s_div, meta.time_offset_s)
        t2 = build_time_axis(len(ch2), meta.timebase_s_div, meta.time_offset_s)
        v1 = voltage_from_display_codes(ch1, meta.ch1_scale_v_div, meta.ch1_offset_v)
        v2 = voltage_from_display_codes(ch2, meta.ch2_scale_v_div, meta.ch2_offset_v)
        self.draw_waveforms(t1, v1, t2, v2, frame["timestamp"], frame["metrics"], f"REPLAY #{index + 1} [{frame['status']}]")
        self.playback_info.setText(
            f"Frame {index + 1}/{self.playback_db.frame_count()} | ID {frame['id']} | {frame['timestamp']} | {frame['status']}"
        )

    def export_csv(self) -> None:
        if not self.playback_db:
            QMessageBox.information(self, APP_NAME, "请先打开一个 SQLite 录波会话。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出趋势 CSV", "measurements.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            self.playback_db.export_metrics_csv(Path(path))
            self.append_log("已导出趋势 CSV：" + path)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"CSV 导出失败：\n{exc}")

    # ---------- event handlers ----------
    @Slot(tuple)
    def on_resources_found(self, resources: tuple) -> None:
        self.resource_combo.clear()
        self.resource_combo.addItems(resources)
        self.append_log(f"找到 {len(resources)} 个 VISA 资源。")

    @Slot(str)
    def on_connected(self, identity: str) -> None:
        self.status_label.setText(identity)
        self.status_label.setStyleSheet("color:#208A3B; font-weight:bold;")

    @Slot()
    def on_disconnected(self) -> None:
        self.status_label.setText("未连接")
        self.status_label.setStyleSheet("color:#C0392B; font-weight:bold;")

    @Slot(str)
    def append_log(self, message: str) -> None:
        self.log_box.append(f"[{datetime.now():%H:%M:%S}] {message}")

    @Slot(str)
    def show_error(self, message: str) -> None:
        self.append_log(message)
        QMessageBox.warning(self, APP_NAME, message)

    def closeEvent(self, event) -> None:
        self.stop_recording()
        if self.playback_db:
            self.playback_db.close()
        self.disconnect_request.emit()
        self.worker_thread.quit()
        self.worker_thread.wait(3000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
