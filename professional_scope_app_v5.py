"""Professional DS1000E Scope Monitor v5: PySide6 + PyQtGraph + PyVISA.

v5 changes vs v3:
 - All VISA I/O runs in ONE worker thread (command queue); GUI never blocks.
 - Binary waveform read without '\n' termination + length validation + retry.
 - CH1/CH2 captured from the SAME acquisition (STOP -> read both -> RUN).
 - Real Single (waits for trigger), user's trigger sweep is restored.
 - Bounded trace buffer (no unbounded memory / O(N^2) growth).
 - eval() replaced by an AST whitelist.
 - Replay no longer pollutes the live trace; clean shutdown; working SCPI console.
 - metrics(): hysteresis zero-crossing, whole-cycle RMS/average.
"""
from __future__ import annotations

import ast
import csv  # noqa: F401  (kept for users extending export)
import logging
import queue
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QSplitter, QStatusBar, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

try:
    import pyvisa
except ImportError:
    pyvisa = None

log = logging.getLogger("scope")

# Rigol DS1000E waveform constants (see DS1000E programming guide)
SCREEN_DIVS_H = 12
ADC_CENTER = 240.0
ADC_PER_DIV = 25.0
ZERO_OFFSET_DIV = 4.6
VISA_TIMEOUT_MS = 5000
SINGLE_TIMEOUT_S = 10.0
DEFAULT_MAX_POINTS = 200_000


# --------------------------------------------------------------------------- data
@dataclass
class Frame:
    t: np.ndarray
    ch1: np.ndarray
    ch2: np.ndarray
    sample_rate: float
    trigger: str
    timestamp: str


class TraceBuffer:
    """Bounded temporary trace; new frames are appended on a continuous time axis."""

    def __init__(self, max_points: int = DEFAULT_MAX_POINTS):
        self.max_points = max_points
        self.clear()

    def clear(self):
        self.t = np.empty(0)
        self.ch1 = np.empty(0)
        self.ch2 = np.empty(0)
        self.sample_rate = 0.0
        self.trigger = ""
        self.timestamp = ""

    def __len__(self):
        return len(self.t)

    def append(self, f: Frame):
        if len(self.t):
            dt = float(np.median(np.diff(f.t))) if len(f.t) > 1 else 0.0
            t = f.t + (self.t[-1] + dt - f.t[0])
        else:
            t = f.t
        m = max(int(self.max_points), 2)
        self.t = np.concatenate((self.t, t))[-m:].copy()
        self.ch1 = np.concatenate((self.ch1, f.ch1))[-m:].copy()
        self.ch2 = np.concatenate((self.ch2, f.ch2))[-m:].copy()
        self.sample_rate, self.trigger, self.timestamp = f.sample_rate, f.trigger, f.timestamp

    def snapshot(self) -> Frame | None:
        if not len(self.t):
            return None
        return Frame(self.t, self.ch1, self.ch2, self.sample_rate, self.trigger, self.timestamp)


# --------------------------------------------------------------------------- helpers
def metrics(x: np.ndarray, t: np.ndarray):
    """Vmax/Vmin/Vpp always from the whole frame; Vavg/RMS over whole cycles when found."""
    ok = np.isfinite(x)
    if not ok.any():
        return None
    x, t = x[ok], t[ok]
    vmax, vmin = float(x.max()), float(x.min())
    vpp = vmax - vmin
    freq = float("nan")
    i0, i1 = 0, len(x)
    if len(x) > 3 and vpp > 0:
        y = x - (vmax + vmin) / 2
        h = 0.05 * vpp  # hysteresis rejects noise-induced crossings
        state = np.where(y > h, 1, np.where(y < -h, -1, 0))
        strong = np.flatnonzero(state)
        zc = np.flatnonzero((y[:-1] < 0) & (y[1:] >= 0))
        if len(strong) and len(zc):
            # last strong state at/before each crossing must be LOW (-1)
            prev = np.searchsorted(strong, zc, side="right") - 1
            ok_prev = (prev >= 0) & (state[strong[np.clip(prev, 0, None)]] == -1)
            # next strong state after the crossing must be HIGH (+1), or absent (end of record)
            nxt = np.searchsorted(strong, zc + 1, side="left")
            has_next = nxt < len(strong)
            ok_next = ~has_next | (state[strong[np.clip(nxt, 0, len(strong) - 1)]] == 1)
            keep = ok_prev & ok_next
            zc, nxt = zc[keep], nxt[keep]
            if len(zc):  # several crossings before the same strong HIGH sample -> keep the last one
                last = np.r_[nxt[1:] != nxt[:-1], True]
                ix = zc[last]
            else:
                ix = zc
            if len(ix) >= 2:
                z = ix + (-y[ix]) / (y[ix + 1] - y[ix])
                tz = np.interp(z, np.arange(len(t)), t)
                period = float(np.median(np.diff(tz)))
                if period > 0:
                    freq = 1.0 / period
                if ix[-1] > ix[0]:
                    i0, i1 = int(ix[0]) + 1, int(ix[-1]) + 1
    xs = x[i0:i1] if i1 > i0 else x
    return dict(
        vmax=vmax, vmin=vmin, vpp=vpp,
        vavg=float(np.mean(xs)), rms=float(np.sqrt(np.mean(xs * xs))),
        freq=freq, period=1.0 / freq if np.isfinite(freq) and freq else float("nan"),
    )


def fmt_metrics(name: str, d) -> str:
    if d is None:
        return f"{name}: (off / no data)"
    return (f"{name}: Vmax={d['vmax']:.4g} V | Vmin={d['vmin']:.4g} V | Vpp={d['vpp']:.4g} V | "
            f"Vavg={d['vavg']:.4g} V | RMS={d['rms']:.4g} V | f={d['freq']:.6g} Hz | T={d['period']:.6g} s")


def rigol_voltage(raw: np.ndarray, scale: float, offset: float) -> np.ndarray:
    return (ADC_CENTER - raw.astype(float)) * (scale / ADC_PER_DIV) - (offset + ZERO_OFFSET_DIV * scale)


def parse_block(raw: bytes) -> np.ndarray:
    """Parse an IEEE 488.2 definite-length block and validate its length."""
    raw = raw.lstrip(b"\r\n \x00")
    if not raw.startswith(b"#") or len(raw) < 3:
        raise ValueError("Scope did not return an IEEE binary waveform block")
    n = int(chr(raw[1]))
    count = int(raw[2:2 + n])
    payload = raw[2 + n:2 + n + count]
    if len(payload) != count:
        raise ValueError(f"Truncated waveform: expected {count} bytes, got {len(payload)}")
    return np.frombuffer(payload, dtype=np.uint8).copy()


_ALLOWED_FUNCS = {
    "abs": np.abs, "sqrt": np.sqrt, "sin": np.sin, "cos": np.cos, "exp": np.exp,
    "log": np.log, "log10": np.log10, "diff0": lambda a: np.concatenate(([0.0], np.diff(a))),
}
_ALLOWED_NAMES = {"ch1", "ch2", "t", "pi"}
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARY = (ast.UAdd, ast.USub)


def _check(node: ast.AST):
    if isinstance(node, ast.Expression):
        _check(node.body)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARY):
        _check(node.operand)
    elif isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        pass
    elif isinstance(node, ast.Name) and (node.id in _ALLOWED_NAMES or node.id in _ALLOWED_FUNCS):
        pass
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
          and node.func.id in _ALLOWED_FUNCS and not node.keywords):
        for a in node.args:
            _check(a)
    else:
        raise ValueError(f"Unsupported syntax: {type(node).__name__}")


def safe_eval(expr: str, f: Frame) -> np.ndarray:
    tree = ast.parse(expr, mode="eval")
    _check(tree)
    env = {"ch1": f.ch1, "ch2": f.ch2, "t": f.t, "pi": np.pi, **_ALLOWED_FUNCS}
    with np.errstate(all="ignore"):
        out = eval(compile(tree, "<math>", "eval"), {"__builtins__": {}}, env)
    out = np.asarray(out, dtype=float)
    if out.ndim == 0:
        out = np.full(len(f.t), float(out))
    if out.shape != f.t.shape:
        raise ValueError("Result shape does not match time axis")
    return out


# --------------------------------------------------------------------------- worker
class AcquisitionWorker(QThread):
    """Owns every VISA object. GUI talks to it only via post()."""

    frame = Signal(object)
    status = Signal(str)
    failure = Signal(str)
    resources = Signal(tuple)
    reply = Signal(str)
    live_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self._cmds: queue.Queue = queue.Queue()
        self._live = False
        self._quit = False
        self._rm = None
        self._scope = None
        self._phase = 0.0
        self.interval = 0.05
        self.manual_fs = 0.0  # float write is atomic; read by worker thread

    # ---- public (thread-safe) API
    def post(self, name: str, arg=None):
        self._cmds.put((name, arg))

    def shutdown(self):
        self._quit = True
        self._cmds.put(("quit", None))

    # ---- thread main
    def run(self):
        try:
            while not self._quit:
                try:
                    cmd = self._cmds.get(timeout=self.interval if self._live else 0.2)
                except queue.Empty:
                    cmd = None
                if cmd is not None:
                    self._handle(*cmd)
                elif self._live:
                    self._acquire(single=False)
        finally:
            self._close()

    def _handle(self, name, arg):
        try:
            if name == "scan":
                self._scan()
            elif name == "connect":
                self._connect(arg)
            elif name == "live":
                self._set_live(bool(arg))
            elif name == "single":
                self._set_live(False)
                self._acquire(single=True)
            elif name == "scpi":
                self._scpi(str(arg))
            elif name == "autoscale":
                self._scpi(":AUT")
        except Exception as e:  # noqa: BLE001
            log.exception("command %s failed", name)
            self.failure.emit(f"{name} failed: {e}")

    def _set_live(self, on: bool):
        if on != self._live:
            self._live = on
            self.live_changed.emit(on)

    # ---- VISA primitives (worker thread only)
    def _q(self, cmd: str) -> str:
        self._scope.write(cmd)
        return self._scope.read_raw().decode("ascii", "replace").strip()

    def _scan(self):
        if pyvisa is None:
            self.resources.emit(tuple())
            return
        own = self._rm is None
        rm = pyvisa.ResourceManager() if own else self._rm
        try:
            self.resources.emit(tuple(rm.list_resources()))
        finally:
            if own:
                rm.close()

    def _close(self):
        for obj in (self._scope, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                log.exception("close failed")
        self._scope = self._rm = None

    def _connect(self, resource: str):
        self._set_live(False)
        self._close()
        if not resource:
            self.status.emit("SIMULATOR")
            return
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed")
        try:
            self._rm = pyvisa.ResourceManager()
            self._scope = self._rm.open_resource(resource)
            self._scope.timeout = VISA_TIMEOUT_MS
            self._scope.write_termination = "\n"
            self._scope.read_termination = None  # binary-safe: a 0x0A data byte must not end the read
            self._scope.write(":WAV:POIN:MODE NORM")
            self._scope.write(":WAV:FORM BYTE")
            self.status.emit(self._q("*IDN?"))
        except Exception as e:  # noqa: BLE001
            self._close()
            self.failure.emit(f"Connection failed: {e}")
            self.status.emit("SIMULATOR")

    def _scpi(self, cmd: str):
        cmd = cmd.strip()
        if not cmd:
            return
        if self._scope is None:
            self.reply.emit(f"> {cmd}\n< (simulator: no instrument connected)")
            return
        if cmd.endswith("?"):
            self.reply.emit(f"> {cmd}\n< {self._q(cmd)}")
        else:
            self._scope.write(cmd)
            self.reply.emit(f"> {cmd}")

    # ---- acquisition
    def _acquire(self, single: bool):
        try:
            fr = self._simulated() if self._scope is None else self._read_real(single)
            if fr is not None and len(fr.t) > 0:
                self.frame.emit(fr)
        except Exception as e:  # noqa: BLE001
            log.exception("acquisition failed")
            self._set_live(False)
            self.failure.emit(f"Acquisition stopped: {e}")

    def _read_real(self, single: bool) -> Frame:
        s = self._scope
        trig = self._q(":TRIG:STAT?")
        sweep = None
        if single:
            sweep = self._q(":TRIG:EDGE:SWE?")
            s.write(":TRIG:EDGE:SWE SING")
            s.write(":RUN")
            deadline = time.monotonic() + SINGLE_TIMEOUT_S
            while self._q(":TRIG:STAT?") != "STOP":
                if time.monotonic() > deadline or self._quit:
                    s.write(f":TRIG:EDGE:SWE {sweep}")
                    raise TimeoutError("Single: no trigger within timeout")
                time.sleep(0.05)
            trig = "STOP"
        else:
            s.write(":STOP")  # freeze so CH1 and CH2 come from the same acquisition
        try:
            return self._read_frame(trig)
        finally:
            if single:
                s.write(f":TRIG:EDGE:SWE {sweep}")  # stays stopped, showing the captured shot
            else:
                s.write(":RUN")

    def _read_wave(self, ch: int) -> np.ndarray:
        last = None
        for _ in range(3):
            try:
                self._scope.write(f":WAV:DATA? CHAN{ch}")
                return parse_block(self._scope.read_raw())
            except ValueError as e:
                last = e
                log.warning("CH%d read retry: %s", ch, e)
        raise last

    def _read_frame(self, trig: str) -> Frame:
        tb = float(self._q(":TIM:SCAL?"))
        off = float(self._q(":TIM:OFFS?"))
        data = []
        for ch in (1, 2):
            if self._q(f":CHAN{ch}:DISP?").upper() not in ("1", "ON"):
                data.append(None)
                continue
            scale = float(self._q(f":CHAN{ch}:SCAL?"))
            offs = float(self._q(f":CHAN{ch}:OFFS?"))
            data.append((self._read_wave(ch), scale, offs))
        lens = [len(d[0]) for d in data if d is not None]
        if not lens:
            raise RuntimeError("Both channels are off")
        n = min(lens)
        if n == 0:
            raise RuntimeError("Empty waveform")
        if self.manual_fs > 0:
            fs = self.manual_fs
            t = (np.arange(n) - n / 2) / fs - off
        else:
            fs = n / (SCREEN_DIVS_H * tb)
            t = np.linspace(-SCREEN_DIVS_H / 2 * tb, SCREEN_DIVS_H / 2 * tb, n, endpoint=False) - off
        volts = [np.full(n, np.nan) if d is None else rigol_voltage(d[0][:n], d[1], d[2]) for d in data]
        return Frame(t, volts[0], volts[1], fs, trig, datetime.now().isoformat(timespec="milliseconds"))

    def _simulated(self) -> Frame:
        n, fs = 2000, 50000.0
        t = np.arange(n) / fs - 0.02
        self._phase += 0.03
        a = 325 * np.sin(2 * np.pi * 50 * t + self._phase) + 2 * np.random.randn(n)
        b = 320 * np.sin(2 * np.pi * 50 * t + self._phase - 0.05) + 2 * np.random.randn(n)
        return Frame(t, a, b, fs, "AUTO", datetime.now().isoformat(timespec="milliseconds"))


# --------------------------------------------------------------------------- plot widget
class WaveformWidget(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.addLegend()
        self.plot.setLabel("bottom", "Time", "s")
        self.plot.setLabel("left", "Voltage", "V")
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.c1 = self.plot.plot(name="CH1", pen=pg.mkPen("#ffd54f", width=1))
        self.c2 = self.plot.plot(name="CH2", pen=pg.mkPen("#40c4ff", width=1))
        self.cm = self.plot.plot(name="MATH", pen=pg.mkPen("#e040fb", width=1))
        self.cm.hide()
        self.line_a = pg.InfiniteLine(angle=90, movable=True, pen="#ff8a65")
        self.line_b = pg.InfiniteLine(angle=90, movable=True, pen="#80cbc4")
        for ln in (self.line_a, self.line_b):
            self.plot.addItem(ln)
            ln.hide()
            ln.sigPositionChanged.connect(self.update_cursor_info)
        self.last: Frame | None = None
        self.plot.scene().sigMouseMoved.connect(self.on_mouse)
        self.info = QLabel("Mouse: -- | Cursors: disabled")
        self.info.setWordWrap(True)
        lay.addWidget(self.plot, 1)
        lay.addWidget(self.info)

    def cursors_visible(self) -> bool:
        return self.line_a.isVisible()

    def set_cursors(self, on: bool):
        self.line_a.setVisible(on)
        self.line_b.setVisible(on)
        self.update_cursor_info()

    def on_mouse(self, pos):
        if self.plot.sceneBoundingRect().contains(pos):
            p = self.plot.getViewBox().mapSceneToView(pos)
            self.info.setText(f"Mouse: t={p.x():.7g} s, V={p.y():.6g} V | {self.cursor_text()}")

    def cursor_text(self) -> str:
        if not self.cursors_visible():
            return "Cursors: disabled"
        f = self.last
        if f is None:
            return "Cursors: no frame"
        a, b = self.line_a.value(), self.line_b.value()
        va1, va2 = np.interp(a, f.t, f.ch1), np.interp(a, f.t, f.ch2)
        vb1, vb2 = np.interp(b, f.t, f.ch1), np.interp(b, f.t, f.ch2)
        dt = abs(b - a)
        inv = 1 / dt if dt else float("nan")
        return (f"A {a:.7g}s: CH1 {va1:.5g}V CH2 {va2:.5g}V | B {b:.7g}s: CH1 {vb1:.5g}V CH2 {vb2:.5g}V | "
                f"Δt={dt:.7g}s, 1/Δt={inv:.6g}Hz")

    def update_cursor_info(self):
        self.info.setText(self.cursor_text())

    def clear(self):
        self.last = None
        for c in (self.c1, self.c2, self.cm):
            c.setData([], [])
        self.cm.hide()
        self.plot.setTitle("")

    def draw(self, f: Frame, mode: str, math_expr: str = "") -> str:
        """Draw frame; returns a math error message ('' if fine)."""
        self.last = f
        self.c1.setData(f.t, f.ch1)
        self.c2.setData(f.t, f.ch2)
        self.plot.setTitle(f"{mode} | {f.timestamp}")
        err = ""
        if math_expr:
            try:
                self.cm.setData(f.t, safe_eval(math_expr, f))
                self.cm.show()
            except Exception as e:  # noqa: BLE001
                self.cm.hide()
                err = f"Math error: {e}"
        else:
            self.cm.hide()
        return err


# --------------------------------------------------------------------------- main window
class OscilloscopeApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Professional DS1000E Scope Monitor v5")
        self.resize(1600, 950)
        self.buffer = TraceBuffer()
        self.replay: Frame | None = None
        self.last_n = 600
        self.dev_text = "Disconnected"
        self.live = False

        self.worker = AcquisitionWorker()
        self.worker.frame.connect(self.on_frame)
        self.worker.status.connect(self.on_status)
        self.worker.failure.connect(self.on_error)
        self.worker.resources.connect(self.on_resources)
        self.worker.reply.connect(lambda s: self.log.append(s))
        self.worker.live_changed.connect(self.on_live_changed)
        self.build()
        self.make_shortcuts()
        self.worker.start()
        QTimer.singleShot(100, lambda: self.worker.post("scan"))

    # ---- UI construction
    def build(self):
        self.setStyleSheet(
            "QWidget{background:#171b22;color:#d8dee9} QGroupBox{border:1px solid #46505f;margin-top:8px;padding-top:9px}"
            " QPushButton{padding:6px;min-height:25px} QPushButton:disabled{color:#6b7380}"
            " QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTextEdit{background:#242b35}")
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        top = QHBoxLayout()
        self.resource = QComboBox()
        self.resource.addItem("Simulator", "")
        scan = QPushButton("Scan VISA")
        scan.clicked.connect(lambda: self.worker.post("scan"))
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.clicked.connect(lambda: self.worker.post("connect", self.resource.currentData() or ""))
        for w in (QLabel("VISA resource"),):
            top.addWidget(w)
        top.addWidget(self.resource, 1)
        top.addWidget(scan)
        top.addWidget(self.btn_connect)
        main.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.wave = WaveformWidget()
        split.addWidget(self.wave)
        split.addWidget(self.controls())
        split.setSizes([1050, 520])
        main.addWidget(split, 1)
        self.bar = QStatusBar()
        self.setStatusBar(self.bar)
        self.bar.showMessage("Disconnected | sample rate -- | trigger -- | --")

    def controls(self):
        tabs = QTabWidget()
        tabs.addTab(self.live_tab(), "Live / measure")
        tabs.addTab(self.file_tab(), "Files / replay")
        tabs.addTab(self.log_tab(), "SCPI / log")
        return tabs

    def live_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("Playback control")
        r = QGridLayout(g)
        self.btn_run = QPushButton("▶ Run")
        self.btn_stop = QPushButton("■ Stop")
        self.btn_single = QPushButton("S Single")
        self.btn_auto = QPushButton("Autoscale")
        self.btn_clear = QPushButton("Clear temporary trace")
        self.btn_run.clicked.connect(self.run_live)
        self.btn_stop.clicked.connect(self.stop_live)
        self.btn_single.clicked.connect(self.single)
        self.btn_auto.clicked.connect(lambda: self.worker.post("autoscale"))
        self.btn_clear.clicked.connect(self.clear_trace)
        for i, b in enumerate((self.btn_run, self.btn_stop, self.btn_single, self.btn_auto, self.btn_clear)):
            r.addWidget(b, i // 2, i % 2)
        self.chk_cursor = QCheckBox("Show tracking cursors")
        self.chk_cursor.toggled.connect(self.wave.set_cursors)
        r.addWidget(self.chk_cursor, 3, 0, 1, 2)

        self.chk_fs = QCheckBox("Manual sample rate")
        self.fs_spin = QDoubleSpinBox()
        self.fs_spin.setRange(0.001, 1e6)
        self.fs_spin.setDecimals(3)
        self.fs_spin.setValue(50)
        self.fs_spin.setEnabled(False)
        self.fs_unit = QComboBox()
        self.fs_unit.addItems(["Sa/s", "kSa/s", "MSa/s"])
        self.fs_unit.setCurrentText("kSa/s")
        self.fs_info = QLabel("Auto: sample rate from screen timebase (N/12/tb)")
        self.fs_info.setWordWrap(True)
        self.chk_fs.toggled.connect(self.update_fs)
        self.fs_spin.valueChanged.connect(self.update_fs)
        self.fs_unit.currentIndexChanged.connect(self.update_fs)
        r.addWidget(self.chk_fs, 4, 0, 1, 2)
        r.addWidget(self.fs_spin, 5, 0)
        r.addWidget(self.fs_unit, 5, 1)
        r.addWidget(self.fs_info, 6, 0, 1, 2)
        r.addWidget(QLabel("Max trace points"), 7, 0)
        self.max_pts = QSpinBox()
        self.max_pts.setRange(1000, 5_000_000)
        self.max_pts.setSingleStep(10000)
        self.max_pts.setValue(DEFAULT_MAX_POINTS)
        self.max_pts.valueChanged.connect(lambda v: setattr(self.buffer, "max_points", v))
        r.addWidget(self.max_pts, 7, 1)
        lay.addWidget(g)

        hint = QLabel("Frames are appended to a bounded in-memory trace (oldest points dropped). "
                      "Consecutive frames are NOT time-continuous on the real signal. "
                      "Nothing is written to disk unless you use a Save command.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        m = QGroupBox("Live measurements (latest frame)")
        self.meas = QTextEdit()
        self.meas.setReadOnly(True)
        QVBoxLayout(m).addWidget(self.meas)
        lay.addWidget(m, 1)
        math = QGroupBox("Custom Math  (ch1, ch2, t, pi; + - * / **; abs sqrt sin cos exp log log10 diff0)")
        fr = QFormLayout(math)
        self.expr = QLineEdit("ch1 - ch2")
        self.expr.editingFinished.connect(self.redraw)
        self.math_err = QLabel("")
        self.math_err.setStyleSheet("color:#ff8a80")
        fr.addRow("Expression", self.expr)
        fr.addRow(self.math_err)
        lay.addWidget(math)
        self.on_live_changed(False)
        return w

    def file_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        for text, fn in [("Save current CSV", self.save_csv), ("Save current NPZ", self.save_npz),
                         ("Save plot PNG", self.save_png), ("Open NPZ waveform (replay)", self.open_npz),
                         ("Back to live", self.back_to_live)]:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: f())
            lay.addWidget(b)
        lay.addWidget(QLabel("Replay is shown separately and never mixed into the live trace."))
        lay.addStretch()
        return w

    def log_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.command = QLineEdit("*IDN?")
        self.command.returnPressed.connect(self.send_scpi)
        send = QPushButton("Send SCPI (query if it ends with ?)")
        send.clicked.connect(self.send_scpi)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        for x in (self.command, send):
            lay.addWidget(x)
        lay.addWidget(self.log, 1)
        return w

    def make_shortcuts(self):
        for key, fn in [("F5", self.run_live), ("F6", self.stop_live), ("F7", self.single),
                        ("Ctrl+S", self.save_npz), ("Ctrl+O", self.open_npz)]:
            a = QAction(self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(lambda _=False, f=fn: f())
            self.addAction(a)

    # ---- actions
    def run_live(self):
        if self.replay is not None:
            self.back_to_live()
        self.worker.post("live", True)
        self.log.append("Live Run requested")

    def stop_live(self):
        self.worker.post("live", False)
        self.log.append("Live Stop requested")

    def single(self):
        if self.replay is not None:
            self.back_to_live()
        self.worker.post("single")

    def send_scpi(self):
        self.worker.post("scpi", self.command.text())

    def clear_trace(self):
        self.buffer.clear()
        self.wave.clear()
        self.meas.clear()
        self.bar.showMessage("Temporary live trace cleared")

    def update_fs(self):
        mult = {"Sa/s": 1.0, "kSa/s": 1e3, "MSa/s": 1e6}[self.fs_unit.currentText()]
        fs = self.fs_spin.value() * mult
        on = self.chk_fs.isChecked()
        self.fs_spin.setEnabled(on)
        self.worker.manual_fs = fs if on else 0.0
        n = self.last_n
        self.fs_info.setText(
            f"dt = {1e6 / fs:.4g} us | N = {n} | Span = {n / fs * 1e3:.5g} ms ({n / fs:.6g} s)" if on
            else "Auto: sample rate from screen timebase (N/12/tb)")

    # ---- worker callbacks (GUI thread)
    def on_live_changed(self, on: bool):
        self.live = on
        self.btn_run.setEnabled(not on)
        self.btn_stop.setEnabled(on)
        self.btn_single.setEnabled(not on)
        self.btn_connect.setEnabled(not on)

    def on_status(self, msg: str):
        self.dev_text = msg
        self.log.append("Connected: " + msg)
        self.bar.showMessage(f"{msg} | sample rate -- | trigger -- | --")

    def on_resources(self, res: tuple):
        cur = self.resource.currentData()
        self.resource.clear()
        self.resource.addItem("Simulator", "")
        for r in res:
            self.resource.addItem(r, r)
        i = self.resource.findData(cur)
        self.resource.setCurrentIndex(max(i, 0))

    def on_error(self, msg: str):
        self.log.append(msg)
        QMessageBox.warning(self, "Scope error", msg)

    def on_frame(self, f: Frame):
        if f is None or len(f.t) == 0 or self.replay is not None:
            return
        if f.sample_rate != 0 and len(f.t) != self.last_n:
            self.last_n = len(f.t)
            self.update_fs()
        self.buffer.append(f)
        self.redraw()
        self.meas.setPlainText("\n".join([fmt_metrics("CH1", metrics(f.ch1, f.t)),
                                          fmt_metrics("CH2", metrics(f.ch2, f.t))]))
        self.bar.showMessage(f"{self.dev_text} | sample rate {f.sample_rate:.6g} Sa/s | trigger {f.trigger} | "
                             f"latest frame {f.timestamp} | temporary trace {len(self.buffer)} samples")

    def current(self) -> Frame | None:
        return self.replay if self.replay is not None else self.buffer.snapshot()

    def redraw(self):
        f = self.current()
        if f is None:
            return
        err = self.wave.draw(f, "REPLAY" if self.replay is not None else "LIVE", self.expr.text().strip())
        self.math_err.setText(err)
        self.wave.update_cursor_info()

    # ---- files
    def save_csv(self):
        f = self.current()
        if f is None:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save CSV", f"wave_{datetime.now():%Y%m%d_%H%M%S}.csv", "CSV (*.csv)")
        if not p:
            return
        cols, head = [f.t, f.ch1, f.ch2], ["time_s", "ch1_v", "ch2_v"]
        expr = self.expr.text().strip()
        if expr:
            try:
                cols.append(safe_eval(expr, f))
                head.append("math")
            except Exception:  # noqa: BLE001
                pass
        try:
            np.savetxt(p, np.column_stack(cols), delimiter=",", header=",".join(head), comments="", fmt="%.9g")
            self.bar.showMessage(f"Saved {p}")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def save_npz(self):
        f = self.current()
        if f is None:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save NPZ", f"wave_{datetime.now():%Y%m%d_%H%M%S}.npz", "NPZ (*.npz)")
        if not p:
            return
        try:
            np.savez_compressed(p, time_s=f.t, ch1_v=f.ch1, ch2_v=f.ch2, sample_rate_sps=f.sample_rate,
                                trigger=f.trigger, timestamp=f.timestamp)
            self.bar.showMessage(f"Saved {p}")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", str(e))

    def save_png(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save plot image", "scope.png", "PNG (*.png)")
        if p and not self.wave.plot.grab().save(p):
            QMessageBox.warning(self, "Save failed", f"Could not write {p}")

    def open_npz(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open NPZ", "", "NPZ (*.npz)")
        if not p:
            return
        try:
            with np.load(p) as z:
                f = Frame(z["time_s"], z["ch1_v"], z["ch2_v"],
                          float(z["sample_rate_sps"]) if "sample_rate_sps" in z else 0.0,
                          str(z["trigger"]) if "trigger" in z else "REPLAY",
                          str(z["timestamp"]) if "timestamp" in z else "REPLAY")
        except (OSError, KeyError, ValueError) as e:
            QMessageBox.warning(self, "Open failed", str(e))
            return
        self.worker.post("live", False)
        self.replay = f
        self.redraw()
        self.bar.showMessage(f"Replay: {p} ({len(f.t)} samples)")

    def back_to_live(self):
        self.replay = None
        if len(self.buffer):
            self.redraw()
        else:
            self.wave.clear()
        self.bar.showMessage("Back to live view")

    # ---- shutdown
    def closeEvent(self, e):
        self.worker.shutdown()
        if not self.worker.wait(VISA_TIMEOUT_MS + 3000):
            log.warning("worker did not stop in time")
        e.accept()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = OscilloscopeApp()
    win.show()
    sys.exit(app.exec())
