# Continuous Live Frame Append Revision

## Required behavior

The live page must not replace the previous waveform each time a new frame is acquired.

- Keep the existing acquired waveform in temporary memory.
- Append every newly acquired frame to that temporary waveform.
- Do not write any automatic frame files.
- Save to disk only when the user selects **Save current CSV** or **Save current NPZ**.
- Provide a manual **Clear temporary trace** command to discard the in-memory data.

## Resulting operation

| User action | Result |
|---|---|
| Run | Starts continuous acquisition and appends each new frame to the temporary trace |
| Stop | Stops live acquisition while retaining the temporary trace |
| Single | Adds one acquired frame to the existing temporary trace |
| Save current CSV | Saves all accumulated temporary samples to CSV |
| Save current NPZ | Saves all accumulated temporary samples to NPZ |
| Clear temporary trace | Removes the temporary waveform from memory; no file is deleted |

## Code changes

### 1. Replace the live-control methods and `on_frame`

Replace the existing block from `def run(self):` through the end of `def on_frame(self, f):` with:

```python
def run(self):
    if not self.worker.isRunning():
        self.worker.run_live()
        self.log.append('Live Run started')

def stop(self):
    self.worker.stop_live()
    self.log.append('Live Stop requested')

def single(self):
    if not self.worker.isRunning():
        self.worker.once()

def toggle(self):
    self.stop() if self.worker.isRunning() else self.run()

def clear_trace(self):
    self.frame = None
    self.wave.c1.setData([], [])
    self.wave.c2.setData([], [])
    self.wave.cm.hide()
    self.meas.clear()
    self.bar.showMessage('Temporary live trace cleared')

def append_frame(self, f):
    if self.frame is None:
        return f

    previous = self.frame
    dt = float(np.median(np.diff(f.t))) if len(f.t) > 1 else 0.0
    start = previous.t[-1] + dt if len(previous.t) else 0.0
    t = start + (f.t - f.t[0])

    return Frame(
        np.concatenate((previous.t, t)),
        np.concatenate((previous.ch1, f.ch1)),
        np.concatenate((previous.ch2, f.ch2)),
        f.sample_rate,
        f.trigger,
        f.timestamp,
    )

def on_frame(self, f):
    self.frame = self.append_frame(f)
    self.wave.draw(self.frame, 'LIVE', self.expr.text().strip())

    a = metrics(f.ch1, f.t)
    b = metrics(f.ch2, f.t)
    self.meas.setPlainText('\n'.join([
        f'{name}: Vmax={d["vmax"]:.4g} V | '
        f'Vmin={d["vmin"]:.4g} V | '
        f'Vpp={d["vpp"]:.4g} V | '
        f'Vavg={d["vavg"]:.4g} V | '
        f'RMS={d["rms"]:.4g} V | '
        f'f={d["freq"]:.6g} Hz | '
        f'T={d["period"]:.6g} s'
        for name, d in [('CH1', a), ('CH2', b)]
    ]))
    self.bar.showMessage(
        f'Connected | sample rate {f.sample_rate:.6g} Sa/s | '
        f'trigger {f.trigger} | latest frame {f.timestamp} | '
        f'temporary trace {len(self.frame.t)} samples'
    )
```

The measurement values above remain those of the latest incoming frame. The graph and the save data contain the full accumulated temporary trace.

### 2. Add the Clear button

In `live_tab()`, replace the control-button list with:

```python
[
    ('▶ Run', self.run),
    ('■ Stop', self.stop),
    ('S Single', self.single),
    ('Auto', self.run),
    ('Clear temporary trace', self.clear_trace),
]
```

Add this informational label below the playback-control group:

```python
hint = QLabel(
    'Each acquired frame is appended to the temporary trace in memory. '
    'Nothing is written to disk unless you use a Save command.'
)
hint.setWordWrap(True)
l.addWidget(hint)
```

### 3. Remove automatic disk saving

In `file_tab()`, remove the `Batch-save every acquired frame as NPZ` checkbox. Replace it with:

```python
l.addWidget(QLabel(
    'Live frames remain temporary in memory. Use a Save command to write '
    'the accumulated trace to disk.'
))
```

Also remove this line from `on_frame()` if it remains in the file:

```python
if self.auto_save.isChecked():
    self.save_npz(auto=True)
```

### 4. Keep replay separate

In `open_npz()`, do not call `self.on_frame(f)`, because that would append replay data to the temporary live trace. Use:

```python
self.frame = f
self.wave.draw(f, 'REPLAY', self.expr.text().strip())
```

## Memory behavior

No temporary waveform file is produced. The retained samples use RAM only, and they remain available until one of the following occurs:

1. The user saves the current data.
2. The user presses **Clear temporary trace**.
3. The application closes.

Because every acquired sample is retained, clear the temporary trace between unrelated or very long tests to prevent unnecessarily high memory use.
