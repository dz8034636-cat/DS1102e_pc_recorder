"""Rigol DS1102Z-E / DS1000Z-E waveform monitor.

Requirements: pip install PySide6 pyqtgraph numpy pyvisa
A VISA backend (NI-VISA or pyvisa-py) is also required.

Revision highlights:
* CH1/CH2 math trace (add, subtract, multiply, divide).
* Clear screen button clears PC display/history only; it does not change scope state.
* Stop sends :STOP to the instrument and asks the acquisition thread to exit.
* Rolling history joins successive acquired frames in a PC-side time record.  Its
  horizontal span is independent of the scope screen timebase.
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
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSplitter, QStatusBar, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

try:
    import pyvisa
except ImportError:
    pyvisa = None


@dataclass
class Preamble:
    fmt: int; waveform_type: int; points: int; count: int
    xinc: float; xorigin: float; xref: float
    yinc: float; yorigin: float; yref: float


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
    if not raw.startswith(b"#") or len(raw) < 3:
        raise RuntimeError(f"Invalid waveform block header: {raw[:80]!r}")
    digits = raw[1] - ord("0")
    if not 1 <= digits <= 9 or len(raw) < 2 + digits:
        raise RuntimeError("Invalid waveform block length header")
    size = int(raw[2:2 + digits]); first = 2 + digits; last = first + size
    if len(raw) < last:
        raise RuntimeError(f"Incomplete waveform transfer: expected {size}, received {len(raw)-first}")
    return np.frombuffer(raw[first:last], dtype=np.uint8).copy()


def get_preamble(scope) -> Preamble:
    values = [x.strip() for x in scope.query(":WAV:PRE?").strip().split(",")]
    if len(values) != 10:
        raise RuntimeError(f"Invalid :WAV:PRE? reply: {values!r}")
    return Preamble(int(values[0]), int(values[1]), int(float(values[2])), int(float(values[3])),
                    float(values[4]), float(values[5]), float(values[6]),
                    float(values[7]), float(values[8]), float(values[9]))


def convert_waveform(raw: np.ndarray, p: Preamble) -> tuple[np.ndarray, np.ndarray]:
    i = np.arange(len(raw), dtype=float)
    return (i - p.xref) * p.xinc + p.xorigin, (raw.astype(float) - p.yorigin - p.yref) * p.yinc


def calc_metrics(v: np.ndarray, t: np.ndarray) -> dict[str, float]:
    good = np.isfinite(v) & np.isfinite(t); nan = float("nan")
    if good.sum() < 3:
        return dict(vmax=nan, vmin=nan, vpp=nan, mean=nan, rms=nan, frequency=nan, period=nan)
    v, t = v[good], t[good]
    out = dict(vmax=float(v.max()), vmin=float(v.min()), vpp=float(np.ptp(v)), mean=float(v.mean()),
               rms=float(np.sqrt(np.mean(v*v))), frequency=nan, period=nan)
    z = v - v.mean(); cross = np.where((z[:-1] < 0) & (z[1:] >= 0))[0]
    if len(cross) >= 2:
        den = z[cross+1] - z[cross]; cross, den = cross[den != 0], den[den != 0]
        if len(cross) >= 2:
            tc = np.interp(cross - z[cross]/den, np.arange(len(t)), t)
            period = float(np.median(np.diff(tc)))
            if period > 0: out["period"], out["frequency"] = period, 1/period
    return out


def math_trace(ch1: np.ndarray, ch2: np.ndarray, operation: str) -> np.ndarray | None:
    if operation == "Off": return None
    if operation == "CH1 + CH2": return ch1 + ch2
    if operation == "CH1 - CH2": return ch1 - ch2
    if operation == "CH1 × CH2": return ch1 * ch2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(ch2) > 1e-15, ch1 / ch2, np.nan)


class ScopeWorker(QThread):
    frame = Signal(object); status = Signal(str); error = Signal(str)
    resources = Signal(tuple); log = Signal(str)

    def __init__(self):
        super().__init__(); self.rm = self.scope = None
        self.running = False; self.single = False; self.freeze = False; self.interval = 0.12

    def scan(self):
        if pyvisa is None: self.error.emit("PyVISA is not installed."); return
        try:
            rm = pyvisa.ResourceManager(); found = tuple(rm.list_resources()); rm.close(); self.resources.emit(found)
        except Exception as exc: self.error.emit(f"VISA scan failed: {exc}")

    def connect_scope(self, resource: str):
        if not resource: self.error.emit("Select a VISA resource first."); return
        self.close_scope()
        try:
            self.rm = pyvisa.ResourceManager(); self.scope = self.rm.open_resource(resource)
            self.scope.timeout = 2500; self.scope.write_termination = "\n"; self.scope.read_termination = "\n"
            identity = self.scope.query("*IDN?").strip()
            if "DS1000Z" not in identity.upper() and "DS1102Z" not in identity.upper():
                raise RuntimeError(f"This monitor supports DS1000Z-E/DS1102Z-E; received: {identity}")
            self.status.emit(identity); self.log.emit(f"> *IDN?\n< {identity}")
        except Exception as exc: self.close_scope(); self.error.emit(f"Connection failed: {exc}")

    def close_scope(self):
        for x in (self.scope, self.rm):
            try:
                if x is not None: x.close()
            except Exception: pass
        self.scope = self.rm = None

    def begin(self, single=False, frozen=False):
        if self.scope is None: self.error.emit("Connect the DS1102Z-E first."); return
        if self.isRunning(): self.error.emit("Acquisition is already running."); return
        self.running, self.single, self.freeze = True, single, frozen
        self.start()

    def begin_live(self): self.begin()
    def begin_single(self): self.begin(single=True)
    def read_frozen(self): self.begin(single=True, frozen=True)

    def stop(self):
        """Thread-safe stop request plus an immediate physical :STOP command."""
        self.running = False; self.requestInterruption()
        if self.scope is not None:
            try:
                self.scope.write(":STOP")
                self.status.emit("Scope stopped")
                self.log.emit("> :STOP")
            except Exception as exc:
                self.log.emit(f"Could not send immediate :STOP (acquisition will exit after current transfer): {exc}")

    def read_channel(self, channel: str):
        if self.isInterruptionRequested(): raise InterruptedError
        s = self.scope; s.write(f":WAV:SOUR {channel}"); s.write(":WAV:MODE NORM"); s.write(":WAV:FORM BYTE")
        p = get_preamble(s)
        if self.isInterruptionRequested(): raise InterruptedError
        s.write(":WAV:DATA?"); raw = ieee_block(s.read_raw())
        if not len(raw): raise RuntimeError(f"{channel}: zero waveform points")
        self.log.emit(f"{channel}: {len(raw)} points; XINC={p.xinc:.9g}; XOR={p.xorigin:.9g}; YINC={p.yinc:.9g}")
        return convert_waveform(raw, p)

    def read_screen(self, mode):
        t1, a = self.read_channel("CHAN1"); t2, b = self.read_channel("CHAN2")
        n = min(len(t1), len(t2))
        if n < 2: raise RuntimeError("Too few waveform points returned")
        trigger = self.scope.query(":TRIG:STAT?").strip()
        return Frame(t1[:n], a[:n], b[:n], trigger, datetime.now().isoformat(timespec="milliseconds"), mode)

    def run(self):
        try:
            if self.freeze:
                self.scope.write(":STOP"); time.sleep(.08)
                if not self.isInterruptionRequested(): self.frame.emit(self.read_screen("FROZEN SCREEN"))
                return
            self.scope.write(":RUN")
            while self.running and not self.isInterruptionRequested():
                self.frame.emit(self.read_screen("LIVE SCREEN"))
                if self.single: break
                self.msleep(max(1, int(self.interval*1000)))
        except InterruptedError:
            pass
        except Exception as exc:
            if not self.isInterruptionRequested(): self.error.emit(f"Acquisition stopped: {exc}")
        finally:
            self.running = self.single = self.freeze = False


class Plot(QWidget):
    def __init__(self):
        super().__init__(); layout = QVBoxLayout(self); self.widget = pg.PlotWidget()
        self.widget.showGrid(x=True, y=True, alpha=.25); self.widget.addLegend()
        self.widget.setLabel("bottom", "Time", "s"); self.widget.setLabel("left", "Voltage", "V")
        self.c1 = self.widget.plot(name="CH1", pen=pg.mkPen("#ffd54f", width=1.3))
        self.c2 = self.widget.plot(name="CH2", pen=pg.mkPen("#40c4ff", width=1.3))
        self.cm = self.widget.plot(name="Math", pen=pg.mkPen("#f48fb1", width=1.2))
        self.a = pg.InfiniteLine(angle=90, movable=True, pen="#ff8a65"); self.b = pg.InfiniteLine(angle=90, movable=True, pen="#80cbc4")
        self.widget.addItem(self.a); self.widget.addItem(self.b); self.a.hide(); self.b.hide(); self.last = None
        self.info = QLabel("Cursors: disabled"); layout.addWidget(self.widget, 1); layout.addWidget(self.info)
        self.a.sigPositionChanged.connect(self.cursor_text); self.b.sigPositionChanged.connect(self.cursor_text)

    def draw(self, t, ch1, ch2, math_v, title, last_frame):
        self.last = last_frame; self.c1.setData(t, ch1); self.c2.setData(t, ch2)
        self.cm.setData(t, math_v) if math_v is not None else self.cm.clear()
        self.widget.setTitle(title)
        if self.a.isVisible(): self.cursor_text()

    def clear(self):
        self.last = None
        for curve in (self.c1, self.c2, self.cm): curve.clear()
        self.widget.setTitle("PC display cleared")
        self.info.setText("Cursors: disabled")

    def set_cursors(self, show):
        self.a.setVisible(show); self.b.setVisible(show)
        if show and self.last is not None:
            t = self.last.t; self.a.setValue(t[len(t)//3]); self.b.setValue(t[2*len(t)//3]); self.cursor_text()

    def cursor_text(self):
        if self.last is None: return
        f=self.last; x,y=self.a.value(),self.b.value(); dt=abs(y-x)
        self.info.setText(f"A={x:.8g}s: CH1={np.interp(x,f.t,f.ch1):.6g}V, CH2={np.interp(x,f.t,f.ch2):.6g}V | "
                          f"B={y:.8g}s: CH1={np.interp(y,f.t,f.ch1):.6g}V, CH2={np.interp(y,f.t,f.ch2):.6g}V | "
                          f"Δt={dt:.8g}s, 1/Δt={1/dt if dt else np.nan:.7g}Hz")


class App(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("Rigol DS1102Z-E Scope Monitor"); self.resize(1550, 920)
        self.current = None; self.history_t=np.array([]); self.history_ch1=np.array([]); self.history_ch2=np.array([]); self.history_math=np.array([])
        self.history_end = None; self.worker=ScopeWorker()
        self.worker.frame.connect(self.on_frame); self.worker.status.connect(self.connected); self.worker.error.connect(self.show_error)
        self.worker.resources.connect(self.set_resources); self.worker.log.connect(self.add_log)
        self.build(); QTimer.singleShot(100, self.worker.scan)

    def build(self):
        root=QWidget(); self.setCentralWidget(root); main=QVBoxLayout(root)
        top=QHBoxLayout(); self.resources=QComboBox(); scan=QPushButton("Scan VISA"); connect=QPushButton("Connect")
        scan.clicked.connect(self.worker.scan); connect.clicked.connect(lambda:self.worker.connect_scope(self.resources.currentText()))
        top.addWidget(QLabel("VISA resource")); top.addWidget(self.resources,1); top.addWidget(scan); top.addWidget(connect); main.addLayout(top)
        split=QSplitter(Qt.Orientation.Horizontal); self.plot=Plot(); split.addWidget(self.plot); split.addWidget(self.tabs()); split.setSizes([1030,500]); main.addWidget(split,1)
        self.bar=QStatusBar(); self.setStatusBar(self.bar); self.bar.showMessage("Disconnected")

    def tabs(self):
        tabs=QTabWidget(); live=QWidget(); l=QVBoxLayout(live); acq=QGroupBox("Acquisition"); g=QGridLayout(acq)
        for i,(label,fn) in enumerate((("Live",self.live),("Stop scope",self.stop),("Single",self.single),("Read frozen",self.frozen),("Clear PC screen",self.clear_screen))):
            b=QPushButton(label); b.clicked.connect(fn); g.addWidget(b,i//2,i%2)
        self.history_enabled=QCheckBox("Connect frames into rolling history"); self.history_enabled.setChecked(True); g.addWidget(self.history_enabled,3,0,1,2)
        self.history_seconds=QDoubleSpinBox(); self.history_seconds.setRange(1,3600); self.history_seconds.setValue(30); self.history_seconds.setSuffix(" s retained"); g.addWidget(self.history_seconds,4,0,1,2)
        cursors=QCheckBox("Show cursors"); cursors.toggled.connect(self.plot.set_cursors); g.addWidget(cursors,5,0,1,2); l.addWidget(acq)
        note=QLabel("Rolling history is a PC-side record made by joining each acquired waveform frame. It preserves all received frames and uses a selectable retention time, independent of the oscilloscope screen scale. It is not a gap-free replacement for the scope's high-speed acquisition memory."); note.setWordWrap(True); l.addWidget(note)
        mg=QGroupBox("Latest-frame measurements"); self.measurements=QTextEdit(); self.measurements.setReadOnly(True); QVBoxLayout(mg).addWidget(self.measurements); l.addWidget(mg,1); tabs.addTab(live,"Live / Measure")
        math=QWidget(); ml=QVBoxLayout(math); box=QGroupBox("Math module"); bg=QGridLayout(box)
        self.math_op=QComboBox(); self.math_op.addItems(["Off","CH1 + CH2","CH1 - CH2","CH1 × CH2","CH1 ÷ CH2"]); self.math_op.currentTextChanged.connect(self.redraw)
        bg.addWidget(QLabel("Expression"),0,0); bg.addWidget(self.math_op,0,1); self.math_note=QLabel("The magenta Math trace is computed point-by-point from the same received samples. Division returns blank points where CH2 is zero."); self.math_note.setWordWrap(True); bg.addWidget(self.math_note,1,0,1,2); ml.addWidget(box); ml.addStretch(); tabs.addTab(math,"Math")
        files=QWidget(); fl=QVBoxLayout(files)
        for label,fn in (("Save current CSV",self.save_csv),("Save current NPZ",self.save_npz),("Save plot PNG",self.save_png)):
            b=QPushButton(label); b.clicked.connect(fn); fl.addWidget(b)
        fl.addStretch(); tabs.addTab(files,"Files")
        logtab=QWidget(); ll=QVBoxLayout(logtab); row=QHBoxLayout(); self.command=QLineEdit("*IDN?"); send=QPushButton("Send SCPI"); send.clicked.connect(self.send_scpi); row.addWidget(self.command,1); row.addWidget(send); self.log=QTextEdit(); self.log.setReadOnly(True); ll.addLayout(row); ll.addWidget(self.log,1); tabs.addTab(logtab,"SCPI / Log")
        return tabs

    def live(self): self.worker.begin_live()
    def single(self): self.worker.begin_single()
    def frozen(self): self.worker.read_frozen()
    def stop(self): self.worker.stop(); self.bar.showMessage("Stop requested: :STOP sent to scope")
    def clear_screen(self):
        self.current=None; self.history_t=np.array([]); self.history_ch1=np.array([]); self.history_ch2=np.array([]); self.history_math=np.array([]); self.history_end=None
        self.plot.clear(); self.measurements.clear(); self.bar.showMessage("PC display and rolling history cleared; scope state unchanged")
    def add_log(self,text): self.log.append(text)
    def connected(self,text): self.add_log("Connected: "+text); self.bar.showMessage("Connected: "+text)
    def set_resources(self,items): self.resources.clear(); self.resources.addItems(items)
    def show_error(self,text): self.add_log("ERROR: "+text); QMessageBox.warning(self,"Scope error",text)

    def append_history(self, f, m):
        dt=float(np.median(np.diff(f.t))) if len(f.t)>1 else 1e-9
        if not np.isfinite(dt) or dt <= 0: dt=1e-9
        start=0.0 if self.history_end is None else self.history_end + dt
        t=start + (f.t-f.t[0])
        self.history_t=np.concatenate((self.history_t,t)); self.history_ch1=np.concatenate((self.history_ch1,f.ch1)); self.history_ch2=np.concatenate((self.history_ch2,f.ch2))
        self.history_math=np.concatenate((self.history_math, m if m is not None else np.full(len(t),np.nan)))
        self.history_end=float(t[-1]); keep=self.history_seconds.value(); cut=max(0.0,self.history_end-keep); ix=np.searchsorted(self.history_t,cut)
        self.history_t=self.history_t[ix:]; self.history_ch1=self.history_ch1[ix:]; self.history_ch2=self.history_ch2[ix:]; self.history_math=self.history_math[ix:]

    def redraw(self):
        if self.current is None: return
        m=math_trace(self.current.ch1,self.current.ch2,self.math_op.currentText())
        if self.history_enabled.isChecked() and len(self.history_t):
            hm = self.history_math if self.math_op.currentText() != "Off" else None
            self.plot.draw(self.history_t,self.history_ch1,self.history_ch2,hm,f"ROLLING HISTORY | {self.history_seconds.value():g} s retention",self.current)
        else: self.plot.draw(self.current.t,self.current.ch1,self.current.ch2,m,f"{self.current.mode} | {self.current.timestamp}",self.current)

    def on_frame(self,f):
        self.current=f; m=math_trace(f.ch1,f.ch2,self.math_op.currentText()); self.append_history(f,m); self.redraw()
        def line(name,v):
            x=calc_metrics(v,f.t); return f"{name}: Vmax={x['vmax']:.6g} V | Vmin={x['vmin']:.6g} V | Vpp={x['vpp']:.6g} V | Mean={x['mean']:.6g} V | RMS={x['rms']:.6g} V | f={x['frequency']:.7g} Hz | T={x['period']:.7g} s"
        text=line("CH1",f.ch1)+"\n"+line("CH2",f.ch2)
        if m is not None: text += "\n"+line(self.math_op.currentText(),m)
        self.measurements.setPlainText(text); self.bar.showMessage(f"{f.mode} | {len(f.t)} points | trigger {f.trigger} | {f.timestamp}")

    def save_csv(self):
        if self.current is None:return
        p,_=QFileDialog.getSaveFileName(self,"Save CSV",f"ds1102ze_{datetime.now():%Y%m%d_%H%M%S}.csv","CSV (*.csv)")
        if p:
            m=math_trace(self.current.ch1,self.current.ch2,self.math_op.currentText())
            rows=zip(self.current.t,self.current.ch1,self.current.ch2, m if m is not None else np.full(len(self.current.t),np.nan))
            with open(p,"w",newline="",encoding="utf8") as h: csv.writer(h).writerows([["time_s","ch1_v","ch2_v","math"],*rows])

    def save_npz(self):
        if self.current is None:return
        p,_=QFileDialog.getSaveFileName(self,"Save NPZ",f"ds1102ze_{datetime.now():%Y%m%d_%H%M%S}.npz","NPZ (*.npz)")
        if p: np.savez_compressed(p,time_s=self.current.t,ch1_v=self.current.ch1,ch2_v=self.current.ch2,math=math_trace(self.current.ch1,self.current.ch2,self.math_op.currentText()),trigger=self.current.trigger,timestamp=self.current.timestamp)

    def save_png(self):
        p,_=QFileDialog.getSaveFileName(self,"Save PNG","scope.png","PNG (*.png)")
        if p:self.plot.widget.grab().save(p)

    def send_scpi(self):
        if self.worker.scope is None or self.worker.isRunning(): self.show_error("Connect the scope and stop acquisition before manual SCPI."); return
        c=self.command.text().strip()
        try:
            if "?" in c:self.add_log(f"> {c}\n< {self.worker.scope.query(c).strip()}")
            else:self.worker.scope.write(c); self.add_log("> "+c)
        except Exception as exc:self.show_error(f"SCPI error: {exc}")

    def closeEvent(self,event):
        self.worker.stop(); self.worker.wait(3000); self.worker.close_scope(); event.accept()


if __name__ == "__main__":
    app=QApplication(sys.argv); app.setStyle("Fusion"); window=App(); window.show(); sys.exit(app.exec())
