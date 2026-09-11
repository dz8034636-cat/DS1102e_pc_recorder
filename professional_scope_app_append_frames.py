"""Professional DS1000E Scope Monitor: PySide6 + PyQtGraph + PyVISA."""
from __future__ import annotations
import csv, json, sys, time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication,QComboBox,QDoubleSpinBox,QFileDialog,QFormLayout,QGridLayout,QGroupBox,QHBoxLayout,QLabel,QLineEdit,QMainWindow,QMessageBox,QPushButton,QScrollArea,QSpinBox,QSplitter,QStatusBar,QTabWidget,QTextEdit,QVBoxLayout,QWidget,QCheckBox)
try:
 import pyvisa
except ImportError: pyvisa=None

@dataclass
class Frame:
 t: np.ndarray; ch1: np.ndarray; ch2: np.ndarray; sample_rate: float; trigger: str; timestamp: str

def metrics(x,t):
 f=float('nan')
 y=x-np.mean(x); ix=np.where((y[:-1]<0)&(y[1:]>=0))[0]
 if len(ix)>1:
  z=ix+(-y[ix])/(y[ix+1]-y[ix]); period=np.median(np.diff(np.interp(z,np.arange(len(t)),t))); f=1/period if period>0 else float('nan')
 return dict(vmax=float(np.max(x)),vmin=float(np.min(x)),vpp=float(np.ptp(x)),vavg=float(np.mean(x)),rms=float(np.sqrt(np.mean(x*x))),freq=f,period=1/f if np.isfinite(f) and f else float('nan'))

def rigol_voltage(raw,scale,offset):
 return (240.0-raw.astype(float))*(scale/25.0)-(offset+4.6*scale)

def block(raw):
 raw=raw.lstrip(b'\r\n')
 if not raw.startswith(b'#'): raise ValueError('Scope did not return an IEEE binary waveform block')
 n=int(chr(raw[1])); count=int(raw[2:2+n]); return np.frombuffer(raw[2+n:2+n+count],dtype=np.uint8).copy()

class AcquisitionWorker(QThread):
 frame=Signal(object); status=Signal(str); failure=Signal(str); resources=Signal(tuple)
 def __init__(self): super().__init__(); self.running=False; self.single=False; self.simulate=True; self.resource=''; self.interval=0.05; self.scope=None; self.rm=None; self.phase=0
 def scan(self):
  if not pyvisa: self.resources.emit(tuple()); return
  try:
   rm=pyvisa.ResourceManager(); r=tuple(rm.list_resources()); rm.close(); self.resources.emit(r)
  except Exception as e:self.failure.emit(str(e))
 def connect_instrument(self,resource):
  if not resource: self.simulate=True; self.status.emit('SIMULATOR connected'); return
  try:
   self.rm=pyvisa.ResourceManager(); self.scope=self.rm.open_resource(resource); self.scope.timeout=3000; self.scope.write_termination='\n'; self.scope.read_termination='\n'; self.scope.write(':WAV:POIN:MODE NORM'); self.scope.write(':WAV:FORM BYTE'); self.simulate=False; self.status.emit(self.scope.query('*IDN?').strip())
  except Exception as e:self.failure.emit(f'Connection failed: {e}'); self.simulate=True
 def run_live(self): self.running=True; self.single=False; self.start()
 def stop_live(self): self.running=False
 def once(self): self.running=True; self.single=True; self.start()
 def read_scope(self):
  s=self.scope
  s.write(':WAV:DATA? CHAN1'); r1=block(s.read_raw()); s.write(':WAV:DATA? CHAN2'); r2=block(s.read_raw())
  tb=float(s.query(':TIM:SCAL?')); off=float(s.query(':TIM:OFFS?')); n=min(len(r1),len(r2)); t=np.linspace(-6*tb,6*tb,n,endpoint=False)-off
  return Frame(t,rigol_voltage(r1[:n],float(s.query(':CHAN1:SCAL?')),float(s.query(':CHAN1:OFFS?'))),rigol_voltage(r2[:n],float(s.query(':CHAN2:SCAL?')),float(s.query(':CHAN2:OFFS?'))),n/(12*tb),s.query(':TRIG:STAT?').strip(),datetime.now().isoformat(timespec='milliseconds'))
 def simulated(self):
  n=2000; fs=50000.; t=np.arange(n)/fs-.02; self.phase+=.03
  a=325*np.sin(2*np.pi*50*t+self.phase)+2*np.random.randn(n); b=320*np.sin(2*np.pi*50*t+self.phase-.05)+2*np.random.randn(n)
  return Frame(t,a,b,fs,'AUTO',datetime.now().isoformat(timespec='milliseconds'))
 def run(self):
  try:
   if not self.simulate: self.scope.write(':TRIG:EDGE:SWE AUTO'); self.scope.write(':RUN')
   while self.running:
    self.frame.emit(self.simulated() if self.simulate else self.read_scope())
    if self.single: self.running=False; break
    time.sleep(self.interval)
   if self.scope: self.scope.write(':STOP')
  except Exception as e:self.running=False; self.failure.emit(f'Acquisition stopped: {e}')

class WaveformWidget(QWidget):
 def __init__(self):
  super().__init__(); l=QVBoxLayout(self); self.plot=pg.PlotWidget(); self.plot.showGrid(x=True,y=True,alpha=.25); self.plot.addLegend(); self.plot.setLabel('bottom','Time','s'); self.plot.setLabel('left','Voltage','V')
  self.c1=self.plot.plot(name='CH1',pen=pg.mkPen('#ffd54f',width=1.4)); self.c2=self.plot.plot(name='CH2',pen=pg.mkPen('#40c4ff',width=1.4)); self.cm=self.plot.plot(name='MATH',pen=pg.mkPen('#e040fb')); self.cm.hide()
  self.a=pg.InfiniteLine(angle=90,movable=True,pen='#ff8a65'); self.b=pg.InfiniteLine(angle=90,movable=True,pen='#80cbc4'); self.plot.addItem(self.a);self.plot.addItem(self.b);self.a.hide();self.b.hide();self.last=None
  self.a.sigPositionChanged.connect(self.cursor);self.b.sigPositionChanged.connect(self.cursor);self.plot.scene().sigMouseMoved.connect(self.mouse)
  self.info=QLabel('Mouse: -- | Cursors: disabled');self.info.setWordWrap(True); l.addWidget(self.plot,1);l.addWidget(self.info)
 def mouse(self,pos):
  if self.plot.sceneBoundingRect().contains(pos):
   p=self.plot.getViewBox().mapSceneToView(pos); self.info.setText(f'Mouse: t={p.x():.7g} s, V={p.y():.6g} V | '+self.cursor_text())
 def cursor_text(self):
  if self.last is None:return 'Cursors: no frame'
  t,x,y=self.last; a,b=self.a.value(),self.b.value(); av=np.interp(a,t,x);ay=np.interp(a,t,y);bv=np.interp(b,t,x);by=np.interp(b,t,y);dt=abs(b-a)
  return f'A {a:.7g}s: CH1 {av:.5g}V CH2 {ay:.5g}V | B {b:.7g}s: CH1 {bv:.5g}V CH2 {by:.5g}V | Δt={dt:.7g}s, 1/Δt={1/dt if dt else np.nan:.6g}Hz'
 def cursor(self): self.info.setText(self.cursor_text())
 def draw(self,f,mode,math_expr=''):
  self.last=(f.t,f.ch1,f.ch2);self.c1.setData(f.t,f.ch1);self.c2.setData(f.t,f.ch2);self.plot.setTitle(f'{mode} | {f.timestamp}')
  if math_expr:
   try:self.cm.setData(f.t,eval(math_expr,{'__builtins__':{}},{'ch1':f.ch1,'ch2':f.ch2,'t':f.t,'np':np,'abs':np.abs,'sqrt':np.sqrt}));self.cm.show()
   except Exception:self.cm.hide()
  else:self.cm.hide()

class OscilloscopeApp(QMainWindow):
 def __init__(self):
  super().__init__();self.setWindowTitle('Professional DS1000E Scope Monitor');self.resize(1600,950);self.frame=None;self.recording=False;self.batch=False;self.worker=AcquisitionWorker();self.worker.frame.connect(self.on_frame);self.worker.status.connect(self.connected);self.worker.failure.connect(self.error);self.worker.resources.connect(self.resources);self.build();self.shortcuts();QTimer.singleShot(100,self.worker.scan)
 def build(self):
  self.setStyleSheet('QWidget{background:#171b22;color:#d8dee9} QGroupBox{border:1px solid #46505f;margin-top:8px;padding-top:9px} QPushButton{padding:6px;min-height:25px} QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTextEdit{background:#242b35}')
  root=QWidget();self.setCentralWidget(root);main=QVBoxLayout(root);top=QHBoxLayout();self.resource=QComboBox();scan=QPushButton('Scan VISA');scan.clicked.connect(self.worker.scan);connect=QPushButton('Connect');connect.clicked.connect(lambda:self.worker.connect_instrument(self.resource.currentText()));top.addWidget(QLabel('VISA resource'));top.addWidget(self.resource,1);top.addWidget(scan);top.addWidget(connect);main.addLayout(top)
  split=QSplitter(Qt.Orientation.Horizontal);self.wave=WaveformWidget();split.addWidget(self.wave);split.addWidget(self.controls());split.setSizes([1050,520]);main.addWidget(split,1);self.bar=QStatusBar();self.setStatusBar(self.bar);self.bar.showMessage('Disconnected | sample rate -- | trigger -- | --')
 def controls(self):
  tabs=QTabWidget();tabs.addTab(self.live_tab(),'Live / measure');tabs.addTab(self.channel_tab(),'Channels');tabs.addTab(self.trigger_tab(),'Horizontal / trigger');tabs.addTab(self.file_tab(),'Files / replay');tabs.addTab(self.log_tab(),'SCPI / log');return tabs
 def live_tab(self):
  w=QWidget();l=QVBoxLayout(w);g=QGroupBox('Playback control');r=QGridLayout(g)
  for i,(text,fn) in enumerate([('▶ Run',self.run),('■ Stop',self.stop),('S Single',self.single),('Auto',self.run),('Clear temporary trace',self.clear_trace)]):b=QPushButton(text);b.clicked.connect(fn);r.addWidget(b,i//2,i%2)
  self.cursor=QCheckBox('Show tracking cursors');self.cursor.toggled.connect(lambda v:(self.wave.a.setVisible(v),self.wave.b.setVisible(v)));r.addWidget(self.cursor,3,0,1,2);l.addWidget(g)
  hint=QLabel('Each acquired frame is appended to the temporary trace in memory. Nothing is written to disk unless you use a Save command.');hint.setWordWrap(True);l.addWidget(hint)
  m=QGroupBox('Live measurements');self.meas=QTextEdit();self.meas.setReadOnly(True);QVBoxLayout(m).addWidget(self.meas);l.addWidget(m,1)
  math=QGroupBox('Custom Math');fr=QFormLayout(math);self.expr=QLineEdit('ch1 - ch2');fr.addRow('Expression',self.expr);l.addWidget(math);return w
 def channel_tab(self):
  s=QScrollArea();s.setWidgetResizable(True);w=QWidget();s.setWidget(w);l=QVBoxLayout(w)
  for ch in ('CH1','CH2'):
   g=QGroupBox(ch+' vertical');f=QFormLayout(g);on=QCheckBox('Channel enabled');on.setChecked(True);scale=QDoubleSpinBox();scale.setRange(.001,10);scale.setValue(1);scale.setSuffix(' V/div');pos=QDoubleSpinBox();pos.setRange(-100,100);pos.setSuffix(' V');couple=QComboBox();couple.addItems(['DC','AC','GND']);probe=QComboBox();probe.addItems(['1X','10X','100X']);inv=QCheckBox('Invert');f.addRow(on);f.addRow('Volts/div',scale);f.addRow('Position',pos);f.addRow('Coupling',couple);f.addRow('Probe',probe);f.addRow(inv);l.addWidget(g)
  l.addStretch();return s
 def trigger_tab(self):
  w=QWidget();l=QVBoxLayout(w)
  for title,rows in [('Horizontal',[('Sec/div','0.001'),('Record length','10000')]),('Trigger',[('Source','CH1'),('Slope','Rising'),('Mode','Auto'),('Level','0.0 V')])]:
   g=QGroupBox(title);f=QFormLayout(g)
   for a,b in rows:f.addRow(a,QLineEdit(b))
   l.addWidget(g)
  l.addStretch();return w
 def file_tab(self):
  w=QWidget();l=QVBoxLayout(w)
  for text,fn in [('Save current CSV',self.save_csv),('Save current NPZ',self.save_npz),('Save plot PNG',self.save_png),('Open NPZ waveform',self.open_npz)]:b=QPushButton(text);b.clicked.connect(fn);l.addWidget(b)
  l.addWidget(QLabel('Live frames remain temporary in memory. Use a Save command to write the accumulated trace to disk.'));l.addStretch();return w
 def log_tab(self):
  w=QWidget();l=QVBoxLayout(w);self.command=QLineEdit('*IDN?');self.log=QTextEdit();self.log.setReadOnly(True);l.addWidget(self.command);l.addWidget(QPushButton('SCPI command (manual log only)'));l.addWidget(self.log,1);return w
 def shortcuts(self):
  for key,fn in [('F5',self.run),('F6',self.stop),('F7',self.single),('Ctrl+S',self.save_npz),('Ctrl+O',self.open_npz),('Space',self.toggle)]:a=QAction(self);a.setShortcut(QKeySequence(key));a.triggered.connect(fn);self.addAction(a)
 def run(self):
  if not self.worker.isRunning():self.worker.run_live();self.log.append('Live Run started')
 def stop(self):self.worker.stop_live();self.log.append('Live Stop requested')
 def single(self):
  if not self.worker.isRunning():self.worker.once()
 def toggle(self):self.stop() if self.worker.isRunning() else self.run()
 def clear_trace(self):
  self.frame=None;self.wave.c1.setData([],[]);self.wave.c2.setData([],[]);self.wave.cm.hide();self.meas.clear();self.bar.showMessage('Temporary live trace cleared')
 def append_frame(self,f):
  if self.frame is None:
   return f
  previous=self.frame
  dt=float(np.median(np.diff(f.t))) if len(f.t)>1 else 0.0
  start=previous.t[-1]+dt if len(previous.t) else 0.0
  t=start+(f.t-f.t[0])
  return Frame(np.concatenate((previous.t,t)),np.concatenate((previous.ch1,f.ch1)),np.concatenate((previous.ch2,f.ch2)),f.sample_rate,f.trigger,f.timestamp)
 def on_frame(self,f):
  self.frame=self.append_frame(f);self.wave.draw(self.frame,'LIVE',self.expr.text().strip());a,b=metrics(f.ch1,f.t),metrics(f.ch2,f.t);self.meas.setPlainText('\n'.join([f'{name}: Vmax={d["vmax"]:.4g} V | Vmin={d["vmin"]:.4g} V | Vpp={d["vpp"]:.4g} V | Vavg={d["vavg"]:.4g} V | RMS={d["rms"]:.4g} V | f={d["freq"]:.6g} Hz | T={d["period"]:.6g} s' for name,d in [('CH1',a),('CH2',b)]]));self.bar.showMessage(f'Connected | sample rate {f.sample_rate:.6g} Sa/s | trigger {f.trigger} | latest frame {f.timestamp} | temporary trace {len(self.frame.t)} samples')
 def save_csv(self):
  if not self.frame:return
  p,_=QFileDialog.getSaveFileName(self,'Save CSV',f'wave_{datetime.now():%Y%m%d_%H%M%S}.csv','CSV (*.csv)')
  if p:
   with open(p,'w',newline='',encoding='utf8') as h:csv.writer(h).writerows([['time_s','ch1_v','ch2_v'],*zip(self.frame.t,self.frame.ch1,self.frame.ch2)])
 def save_npz(self,auto=False):
  if not self.frame:return
  p=str(Path.cwd()/f'wave_{datetime.now():%Y%m%d_%H%M%S_%f}.npz') if auto else QFileDialog.getSaveFileName(self,'Save NPZ',f'wave_{datetime.now():%Y%m%d_%H%M%S}.npz','NPZ (*.npz)')[0]
  if p:np.savez_compressed(p,time_s=self.frame.t,ch1_v=self.frame.ch1,ch2_v=self.frame.ch2,sample_rate_sps=self.frame.sample_rate,trigger=self.frame.trigger,timestamp=self.frame.timestamp)
 def save_png(self):
  p,_=QFileDialog.getSaveFileName(self,'Save plot image','scope.png','PNG (*.png)');
  if p:self.wave.plot.grab().save(p)
 def open_npz(self):
  p,_=QFileDialog.getOpenFileName(self,'Open NPZ','','NPZ (*.npz)')
  if p:
   z=np.load(p);f=Frame(z['time_s'],z['ch1_v'],z['ch2_v'],float(z.get('sample_rate_sps',0)),str(z.get('trigger','REPLAY')),str(z.get('timestamp','REPLAY')));self.on_frame(f);self.wave.draw(f,'REPLAY',self.expr.text().strip())
 def connected(self,msg):self.log.append('Connected: '+msg)
 def resources(self,r):self.resource.clear();self.resource.addItems(r);self.resource.insertItem(0,'');self.resource.setCurrentIndex(0)
 def error(self,msg):self.log.append(msg);QMessageBox.warning(self,'Scope error',msg)
 def closeEvent(self,e):self.worker.stop_live();self.worker.wait(2000);e.accept()
if __name__=='__main__':
 app=QApplication(sys.argv);app.setStyle('Fusion');w=OscilloscopeApp();w.show();sys.exit(app.exec())
