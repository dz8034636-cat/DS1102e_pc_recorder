"""Rigol DS1102Z-E / DS1000Z-E waveform monitor.

Model-specific implementation for an instrument identifying itself as DS1102Z-E.
Waveform sequence follows the DS1000Z/DS1000Z-E Programming Guide:
    :WAV:SOUR CHAN1
    :WAV:MODE NORM
    :WAV:FORM BYTE
    :WAV:PRE?
    :WAV:DATA?

For each channel the preamble gives the real X/Y scaling. Byte samples are
converted exactly as documented:
    time = (point_index - XREF) * XINC + XOR
    voltage = (raw_byte - YOR - YREF) * YINC

Requirements: pip install PySide6 pyqtgraph numpy pyvisa
A VISA backend (NI-VISA or pyvisa-py) is also required.
"""
from __future__ import annotations

import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

try:
    import pyvisa
except ImportError:
    pyvisa = None


@dataclass
class Preamble:
    fmt: int
    waveform_type: int
    points: int
    count: int
    xinc: float
    xorigin: float
    xref: float
    yinc: float
    yorigin: float
    yref: float


@dataclass
class Frame:
    t: np.ndarray
    ch1: np.ndarray
    ch2: np.ndarray
    trigger: str
    timestamp: str
    mode: str


def ieee_block(raw: bytes) -> np.ndarray:
    raw = raw.lstrip(b"\r\n")
    if not raw.startswith(b"#"):
        raise RuntimeError(f"Expected TMC waveform header, got {raw[:80]!r}")
    if len(raw) < 3:
        raise RuntimeError("Incomplete TMC waveform header")
    digits = raw[1] - ord("0")
    if digits < 1 or digits > 9 or len(raw) < 2 + digits:
        raise RuntimeError("Invalid TMC waveform header")
    size = int(raw[2:2 + digits])
    first = 2 + digits
    last = first + size
    if len(raw) < last:
        raise RuntimeError(f"Incomplete waveform transfer: expected {size} data bytes, received {len(raw) - first}")
    return np.frombuffer(raw[first:last], dtype=np.uint8).copy()


def get_preamble(scope) -> Preamble:
    text = scope.query(":WAV:PRE?").strip()
    values = [part.strip() for part in text.split(",")]
    if len(values) != 10:
        raise RuntimeError(f"Invalid :WAV:PRE? reply: {text!r}")
    return Preamble(
        int(values[0]), int(values[1]), int(float(values[2])), int(float(values[3])),
        float(values[4]), float(values[5]), float(values[6]),
        float(values[7]), float(values[8]), float(values[9]),
    )


def convert_waveform(raw: np.ndarray, p: Preamble) -> tuple[np.ndarray, np.ndarray]:
    index = np.arange(len(raw), dtype=float)
    t = (index - p.xref) * p.xinc + p.xorigin
    v = (raw.astype(float) - p.yorigin - p.yref) * p.yinc
    return t, v


def metrics(v: np.ndarray, t: np.ndarray) -> dict[str, float]:
    ok = np.isfinite(v) & np.isfinite(t)
    nan = float("nan")
    if np.count_nonzero(ok) < 3:
        return dict(vmax=nan, vmin=nan, vpp=nan, mean=nan, rms=nan, frequency=nan, period=nan)
    v, t = v[ok], t[ok]
    result = dict(vmax=float(np.max(v)), vmin=float(np.min(v)), vpp=float(np.ptp(v)), mean=float(np.mean(v)), rms=float(np.sqrt(np.mean(v * v))), frequency=nan, period=nan)
    centered = v - np.mean(v)
    cross = np.where((centered[:-1] < 0) & (centered[1:] >= 0))[0]
    if len(cross) < 2:
        return result
    den = centered[cross + 1] - centered[cross]
    valid = den != 0
    cross, den = cross[valid], den[valid]
    if len(cross) < 2:
        return result
    fractional = cross - centered[cross] / den
    tc = np.interp(fractional, np.arange(len(t)), t)
    period = float(np.median(np.diff(tc)))
    if period > 0:
        result["period"] = period
        result["frequency"] = 1.0 / period
    return result


class ScopeWorker(QThread):
    frame = Signal(object)
    status = Signal(str)
    error = Signal(str)
    resources = Signal(tuple)
    log = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.rm = None
        self.scope = None
        self.running = False
        self.single = False
        self.freeze = False
        self.interval = 0.15

    def scan(self) -> None:
        if pyvisa is None:
            self.error.emit("PyVISA is not installed.")
            return
        try:
            rm = pyvisa.ResourceManager()
            found = tuple(rm.list_resources())
            rm.close()
            self.resources.emit(found)
        except Exception as exc:
            self.error.emit(f"VISA scan failed: {exc}")

    def connect_scope(self, resource: str) -> None:
        self.close_scope()
        try:
            self.rm = pyvisa.ResourceManager()
            self.scope = self.rm.open_resource(resource)
            self.scope.timeout = 8000
            self.scope.write_termination = "\n"
            self.scope.read_termination = "\n"
            identity = self.scope.query("*IDN?").strip()
            if "DS1000Z" not in identity.upper() and "DS1102Z" not in identity.upper():
                raise RuntimeError(f"This file is for DS1000Z-E/DS1102Z-E, but scope identifies as: {identity}")
            self.status.emit(identity)
            self.log.emit(f"> *IDN?\n< {identity}")
        except Exception as exc:
            self.close_scope()
            self.error.emit(f"Connection failed: {exc}")

    def close_scope(self) -> None:
        if self.scope is not None:
            try: self.scope.close()
            except Exception: pass
        if self.rm is not None:
            try: self.rm.close()
            except Exception: pass
        self.scope, self.rm = None, None

    def begin_live(self) -> None:
        if self.scope is None:
            self.error.emit("Connect the DS1102Z-E first.")
        elif not self.isRunning():
            self.running, self.single, self.freeze = True, False, False
            self.start()

    def begin_single(self) -> None:
        if self.scope is None:
            self.error.emit("Connect the DS1102Z-E first.")
        elif not self.isRunning():
            self.running, self.single, self.freeze = True, True, False
            self.start()

    def read_frozen(self) -> None:
        if self.scope is None:
            self.error.emit("Connect the DS1102Z-E first.")
        elif not self.isRunning():
            self.running, self.single, self.freeze = True, True, True
            self.start()

    def stop(self) -> None:
        self.running = False

    def read_channel(self, channel: str) -> tuple[np.ndarray, np.ndarray]:
        s = self.scope
        # Required DS1000Z-E sequence: source is set separately from DATA?.
        s.write(f":WAV:SOUR {channel}")
        s.write(":WAV:MODE NORM")
        s.write(":WAV:FORM BYTE")
        p = get_preamble(s)
        s.write(":WAV:DATA?")
        raw = ieee_block(s.read_raw())
        if len(raw) == 0:
            raise RuntimeError(f"{channel}: zero-length waveform. Check that {channel} is enabled and visible on the scope.")
        self.log.emit(f"{channel}: {len(raw)} BYTE points; XINC={p.xinc:.9g}, XOR={p.xorigin:.9g}, XREF={p.xref:.9g}; YINC={p.yinc:.9g}, YOR={p.yorigin:.9g}, YREF={p.yref:.9g}")
        return convert_waveform(raw, p)

    def read_screen(self, mode: str) -> Frame:
        t1, ch1 = self.read_channel("CHAN1")
        t2, ch2 = self.read_channel("CHAN2")
        n = min(len(t1), len(t2))
        if n < 2:
            raise RuntimeError("Too few waveform points returned")
        trigger = self.scope.query(":TRIG:STAT?").strip()
        # The same NORM mode and time parameters should apply to both channels.
        return Frame(t1[:n], ch1[:n], ch2[:n], trigger, datetime.now().isoformat(timespec="milliseconds"), mode)

    def run(self) -> None:
        try:
            if self.freeze:
                self.scope.write(":STOP")
                time.sleep(0.10)
                self.frame.emit(self.read_screen("FROZEN SCREEN"))
                return
            self.scope.write(":RUN")
            while self.running:
                self.frame.emit(self.read_screen("LIVE SCREEN"))
                if self.single:
                    break
                time.sleep(self.interval)
        except Exception as exc:
            self.error.emit(f"Acquisition stopped: {exc}")
        finally:
            self.running = self.single = self.freeze = False


class Plot(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.widget = pg.PlotWidget()
        self.widget.showGrid(x=True, y=True, alpha=0.25)
        self.widget.addLegend()
        self.widget.setLabel("bottom", "Time", "s")
        self.widget.setLabel("left", "Voltage", "V")
        self.c1 = self.widget.plot(name="CH1", pen=pg.mkPen("#ffd54f", width=1.4))
        self.c2 = self.widget.plot(name="CH2", pen=pg.mkPen("#40c4ff", width=1.4))
        self.a = pg.InfiniteLine(angle=90, movable=True, pen="#ff8a65")
        self.b = pg.InfiniteLine(angle=90, movable=True, pen="#80cbc4")
        self.widget.addItem(self.a); self.widget.addItem(self.b)
        self.a.hide(); self.b.hide()
        self.last = None
        self.info = QLabel("Cursors: disabled")
        layout.addWidget(self.widget, 1); layout.addWidget(self.info)
        self.a.sigPositionChanged.connect(self.cursor_text)
        self.b.sigPositionChanged.connect(self.cursor_text)

    def draw(self, frame: Frame) -> None:
        self.last = frame
        self.c1.setData(frame.t, frame.ch1)
        self.c2.setData(frame.t, frame.ch2)
        self.widget.setTitle(f"{frame.mode} | {frame.timestamp}")
        if self.a.isVisible(): self.set_cursors(True)

    def set_cursors(self, visible: bool) -> None:
        self.a.setVisible(visible); self.b.setVisible(visible)
        if visible and self.last is not None:
            n = len(self.last.t)
            self.a.setValue(self.last.t[n // 3]); self.b.setValue(self.last.t[2 * n // 3])
            self.cursor_text()

    def cursor_text(self) -> None:
        if self.last is None: return
        f = self.last; a, b = self.a.value(), self.b.value()
        dt = abs(b-a)
        self.info.setText(f"A={a:.8g}s: CH1={np.interp(a,f.t,f.ch1):.6g}V, CH2={np.interp(a,f.t,f.ch2):.6g}V | B={b:.8g}s: CH1={np.interp(b,f.t,f.ch1):.6g}V, CH2={np.interp(b,f.t,f.ch2):.6g}V | Δt={dt:.8g}s, 1/Δt={1/dt if dt else np.nan:.7g}Hz")


class App(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Rigol DS1102Z-E Scope Monitor")
        self.resize(1500, 900)
        self.current: Frame | None = None
        self.worker = ScopeWorker()
        self.worker.frame.connect(self.on_frame); self.worker.status.connect(self.connected)
        self.worker.error.connect(self.show_error); self.worker.resources.connect(self.set_resources); self.worker.log.connect(self.add_log)
        self.build(); QTimer.singleShot(100, self.worker.scan)

    def build(self) -> None:
        root=QWidget(); self.setCentralWidget(root); main=QVBoxLayout(root)
        top=QHBoxLayout(); self.resources=QComboBox(); scan=QPushButton("Scan VISA"); connect=QPushButton("Connect")
        scan.clicked.connect(self.worker.scan); connect.clicked.connect(lambda:self.worker.connect_scope(self.resources.currentText()))
        top.addWidget(QLabel("VISA resource")); top.addWidget(self.resources,1); top.addWidget(scan); top.addWidget(connect); main.addLayout(top)
        splitter=QSplitter(Qt.Orientation.Horizontal); self.plot=Plot(); splitter.addWidget(self.plot); splitter.addWidget(self.tabs()); splitter.setSizes([1000,500]); main.addWidget(splitter,1)
        self.bar=QStatusBar(); self.setStatusBar(self.bar); self.bar.showMessage("Disconnected")

    def tabs(self) -> QTabWidget:
        tabs=QTabWidget(); live=QWidget(); layout=QVBoxLayout(live); group=QGroupBox("Acquisition"); grid=QGridLayout(group)
        for i,(label,fn) in enumerate([(" Live",self.live),("Stop",self.stop),("Single",self.single),("Read frozen screen",self.frozen)]):
            b=QPushButton(label); b.clicked.connect(fn); grid.addWidget(b,i//2,i%2)
        cursors=QCheckBox("Show cursors"); cursors.toggled.connect(self.plot.set_cursors); grid.addWidget(cursors,2,0,1,2); layout.addWidget(group)
        note=QLabel("For an exact PC-to-scope comparison, stabilise the scope waveform then use Read frozen screen. Every PC update replaces the preceding frame; it is never appended as a false continuous trace."); note.setWordWrap(True); layout.addWidget(note)
        mg=QGroupBox("Latest frame measurements"); self.measurements=QTextEdit(); self.measurements.setReadOnly(True); QVBoxLayout(mg).addWidget(self.measurements); layout.addWidget(mg,1); tabs.addTab(live,"Live / Measure")
        files=QWidget(); fl=QVBoxLayout(files)
        for label,fn in [("Save CSV",self.save_csv),("Save NPZ",self.save_npz),("Save plot PNG",self.save_png)]: b=QPushButton(label); b.clicked.connect(fn); fl.addWidget(b)
        fl.addStretch(); tabs.addTab(files,"Files")
        logger=QWidget(); ll=QVBoxLayout(logger); row=QHBoxLayout(); self.command=QLineEdit("*IDN?"); send=QPushButton("Send SCPI"); send.clicked.connect(self.send_scpi); row.addWidget(self.command,1); row.addWidget(send); self.log=QTextEdit(); self.log.setReadOnly(True); ll.addLayout(row); ll.addWidget(self.log,1); tabs.addTab(logger,"SCPI / Log")
        return tabs

    def live(self): self.worker.begin_live()
    def stop(self): self.worker.stop()
    def single(self): self.worker.begin_single()
    def frozen(self): self.worker.read_frozen()
    def add_log(self,text): self.log.append(text)
    def connected(self,text): self.add_log("Connected: "+text); self.bar.showMessage("Connected: "+text)
    def set_resources(self,resources): self.resources.clear(); self.resources.addItems(resources)
    def show_error(self,text): self.add_log("ERROR: "+text); QMessageBox.warning(self,"Scope error",text)

    def on_frame(self,f:Frame):
        self.current=f; self.plot.draw(f); a,b=metrics(f.ch1,f.t),metrics(f.ch2,f.t)
        def line(name,m): return f"{name}: Vmax={m['vmax']:.6g} V | Vmin={m['vmin']:.6g} V | Vpp={m['vpp']:.6g} V | Mean={m['mean']:.6g} V | RMS={m['rms']:.6g} V | f={m['frequency']:.7g} Hz | T={m['period']:.7g} s"
        self.measurements.setPlainText(line("CH1",a)+"\n"+line("CH2",b)); self.bar.showMessage(f"{f.mode} | {len(f.t)} points | trigger {f.trigger} | {f.timestamp}")

    def save_csv(self):
        if self.current is None:return
        p,_=QFileDialog.getSaveFileName(self,"Save CSV",f"ds1102ze_{datetime.now():%Y%m%d_%H%M%S}.csv","CSV (*.csv)")
        if p:
            with open(p,"w",newline="",encoding="utf8") as h: csv.writer(h).writerows([["time_s","ch1_v","ch2_v"],*zip(self.current.t,self.current.ch1,self.current.ch2)])

    def save_npz(self):
        if self.current is None:return
        p,_=QFileDialog.getSaveFileName(self,"Save NPZ",f"ds1102ze_{datetime.now():%Y%m%d_%H%M%S}.npz","NPZ (*.npz)")
        if p: np.savez_compressed(p,time_s=self.current.t,ch1_v=self.current.ch1,ch2_v=self.current.ch2,trigger=self.current.trigger,timestamp=self.current.timestamp)

    def save_png(self):
        p,_=QFileDialog.getSaveFileName(self,"Save PNG","scope.png","PNG (*.png)")
        if p:self.plot.widget.grab().save(p)

    def send_scpi(self):
        if self.worker.scope is None or self.worker.isRunning(): self.show_error("Connect the scope and stop acquisition before sending manual SCPI."); return
        c=self.command.text().strip()
        try:
            if "?" in c:self.add_log(f"> {c}\n< {self.worker.scope.query(c).strip()}")
            else:self.worker.scope.write(c); self.add_log("> "+c)
        except Exception as exc:self.show_error(f"SCPI error: {exc}")

    def closeEvent(self,event): self.worker.stop(); self.worker.wait(2000); self.worker.close_scope(); event.accept()

if __name__ == "__main__":
    app=QApplication(sys.argv); app.setStyle("Fusion"); window=App(); window.show(); sys.exit(app.exec())
