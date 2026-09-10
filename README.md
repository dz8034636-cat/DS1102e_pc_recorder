# DS1102E PC Recorder v3 使用说明

## 1. 项目用途

`DS1102E PC Recorder v3` 是一个面向 **RIGOL DS1102E / DS1000E 系列示波器**的 Python GUI 工具，用于：

- 通过 USB/VISA 使用 SCPI 控制示波器
- 在电脑实时显示 CH1 与 CH2 回传的显示帧波形
- 将电脑收到的波形帧持续记录到 SQLite 文件
- 自动计算每帧 CH1/CH2 RMS 与频率
- 按 CH2 RMS 阈值自动记录失压/恢复事件
- 插入人工事件标记，例如“市电切除”“ATS 开始切换”
- 打开历史录波会话、逐帧回放波形
- 导出 RMS、频率、状态趋势 CSV，供 Excel、Python、Power BI 等分析

> 本程序是 **PC 端显示帧录波与数据分析工具**。它持续记录通过 USB/SCPI 回传到电脑的波形帧，不是无缝高速 DAQ，也不能替代专业电力故障录波器。

---

## 2. 文件说明

| 文件 | 作用 |
|---|---|
| `ds1102e_pc_recorder_v3_complete.py` | 主程序，可直接运行 |
| `DS1102E_PC_Recorder_v3_使用说明.md` | 本使用说明 |
| `sessions/*.sqlite` | 每次录波生成的 SQLite 会话文件 |
| `measurements.csv` | 从回放会话导出的 RMS/频率趋势文件 |

程序会在主程序所在目录自动建立：

```text
sessions/
```

每次开始记录时，程序将在其中创建类似文件：

```text
sessions/
└── 20260910_133000_ATS_Transfer_Test.sqlite
```

---

## 3. 系统要求

### 3.1 操作系统

建议使用：

```text
Windows 10 或 Windows 11，64-bit
```

### 3.2 Python

建议：

```text
Python 3.10、3.11 或 3.12，64-bit
```

### 3.3 必要软件

- Python
- PyCharm Community/Professional（可选，但推荐）
- NI-VISA（推荐，用于稳定识别 DS1102E USB 仪器）
- DS1102E 与电脑之间的 USB-A 至 USB-B 数据线

### 3.4 Python 依赖包

```text
PySide6
pyqtgraph
PyVISA
numpy
```

---

## 4. 安装步骤

### 4.1 在 PyCharm 创建项目

建议项目结构：

```text
C:\Users\DSS\PycharmProjects\ds1102e_pc_recorder\
├── .venv\
├── ds1102e_pc_recorder_v3_complete.py
└── DS1102E_PC_Recorder_v3_使用说明.md
```

PyCharm 的解释器应指向：

```text
C:\Users\DSS\PycharmProjects\ds1102e_pc_recorder\.venv\Scripts\python.exe
```

检查位置：

```text
File
→ Settings
→ Project: ds1102e_pc_recorder
→ Python Interpreter
```

### 4.2 安装 Python 包

在 PyCharm 下方的 **Terminal** 中执行：

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install PySide6 pyqtgraph pyvisa numpy
```

完成后可测试：

```powershell
.\.venv\Scripts\python.exe -c "from PySide6.QtWidgets import QApplication; import pyqtgraph; import pyvisa; import numpy; print('Python dependencies OK')"
```

预期输出：

```text
Python dependencies OK
```

### 4.3 安装 NI-VISA

安装 NI-VISA 后重新启动电脑或至少重新插拔示波器 USB 数据线。

NI-VISA 用于让 Windows 和 PyVISA 识别 DS1102E 这类 USBTMC 测试仪器。程序中显示“找到 0 个 VISA 资源”通常意味着：

- 示波器尚未通过 USB Device 接口接到电脑
- DS1102E 未开机
- 未安装或未正确安装 NI-VISA
- 使用了前面板 USB Host 口，而不是后方 USB Device 口
- Windows 尚未正确识别 USB 测量仪器

---

## 5. 正确连接 DS1102E

DS1102E 的 USB 接口用途不同：

| 接口 | 位置 | 用途 | 能否用于 Python/VISA 控制 |
|---|---|---|---|
| USB Host | 前面板 USB-A | 插 USB 手指保存文件、截图 | 否 |
| USB Device | 后面板 USB-B 方口 | 连接电脑进行远程控制 | 是 |

正确连接方式：

```text
DS1102E 后面板 USB Device（USB-B）
        ↓
USB 数据线
        ↓
电脑 USB-A 口
```

不要使用仅供充电的 USB 线。

---

## 6. 启动程序

在 PyCharm 中：

1. 打开 `ds1102e_pc_recorder_v3_complete.py`
2. 右键文件
3. 点击 `Run 'ds1102e_pc_recorder_v3_complete'`

或在 Terminal 中执行：

```powershell
.\.venv\Scripts\python.exe .\ds1102e_pc_recorder_v3_complete.py
```

程序启动后，即使未接示波器也可以正常打开 GUI。此时日志会显示：

```text
找到 0 个 VISA 资源。
```

这是正常现象，表示没有连接可识别的 VISA 仪器。

---

## 7. GUI 使用说明

## 7.1 顶部：仪器连接

| 控件 | 作用 |
|---|---|
| VISA 资源 | 显示电脑发现的 USB/GPIB/LAN VISA 仪器 |
| 扫描 VISA | 重新搜索仪器 |
| 连接 | 连接选中的 DS1102E |
| 断开 | 关闭 VISA 通信 |
| PC 轮询周期 | 设置电脑从示波器读取一帧的目标间隔 |

建议首次使用：

```text
PC 轮询周期：250 ms / 帧
```

连接成功后，状态栏通常显示：

```text
RIGOL TECHNOLOGIES,DS1102E,序列号,固件版本
```

## 7.2 左侧：实时/回放波形

左侧为主要波形显示区：

- 黄色：CH1
- 蓝色：CH2
- 纵轴：电压，单位 V
- 横轴：时间，单位 s
- 下方：CH1/CH2 RMS 与频率估算结果

显示模式包括：

```text
LIVE
```

表示实时从 DS1102E 回传的当前帧。

```text
REPLAY #n [NORMAL]
```

表示正在回放 SQLite 中的第 n 帧。

```text
REPLAY #n [VOLTAGE_LOSS]
```

表示该帧被自动判定为 CH2 失压。

---

## 8. 示波器控制

“示波器控制”页分为四个独立子页，以避免参数过于拥挤。

### 8.1 快捷控制

| 按钮 | 功能 | SCPI 命令 |
|---|---|---|
| 自动设置 | 示波器自动调整显示 | `:AUTO` |
| 运行 | 连续采集 | `:RUN` |
| 停止 | 停止采集 | `:STOP` |
| 单次采集 | 等待一次触发后停止 | `:TRIG:EDGE:SWE SING`、`:RUN` |
| 强制触发 | 立即完成触发 | `:FORC` |
| 清除显示 | 清除显示余辉 | `:DISP:CLE` |
| 读取仪器信息 | 查询型号、序列号、固件 | `*IDN?` |
| 读取触发状态 | 查询 RUN/STOP/WAIT 等状态 | `:TRIG:STAT?` |
| 读取系统错误 | 查询仪器错误队列 | `:SYST:ERR?` |

### 8.2 CH1 / CH2

每个通道独立设置：

- 启用或关闭显示
- 输入耦合：DC / AC / GND
- 探头倍率
- 垂直量程，V/div
- 垂直偏置，V

对应的典型 SCPI 命令：

```text
:CHAN1:DISP ON
:CHAN1:COUP AC
:CHAN1:PROB 100
:CHAN1:SCAL 20
:CHAN1:OFFS 0
```

### 8.3 触发与采集

包含：

- 时基，s/div
- 水平偏置
- 采集类型：NORM、PEAK、AVER
- 内存深度：NORM、LONG
- 触发源：CHAN1、CHAN2、EXT、ACLINE
- 触发边沿：POS、NEG
- 触发扫描：AUTO、NORM、SING
- 触发电平

所有设置完成后，点击：

```text
应用全部设置到 DS1102E
```

程序会将当前设置依次发送给示波器。

> 若某一条 SCPI 指令因 DS1102E 固件、选件或当前状态而不支持，程序会在日志中显示失败命令，但仍会继续尝试其他设置命令。

---

## 9. PC 连续录波

## 9.1 建立一次录波会话

进入：

```text
记录与回放
```

填写：

| 字段 | 示例 |
|---|---|
| 会话名称 | `ATS_Transfer_Run01` |
| CH1 工程名称 | `市电输入` |
| CH2 工程名称 | `ATS负载侧` |
| CH2 失压阈值 | `23 V RMS` |
| 人工事件内容 | `开始ATS切换` |

点击：

```text
开始实时显示并记录
```

程序会：

1. 向示波器发送 `:RUN`
2. 以设定的轮询周期读取 CH1/CH2 显示帧
3. 在左侧实时显示波形
4. 计算 CH1/CH2 RMS 与频率
5. 将原始波形帧、仪器参数、量测结果写入 SQLite
6. 对 CH2 RMS 执行失压状态判断

## 9.2 自动失压事件

程序的初始状态判据：

```text
CH2 RMS < 失压阈值  →  VOLTAGE_LOSS
CH2 RMS ≥ 失压阈值  →  NORMAL
```

例如 230 V 系统可先使用：

```text
23 V RMS
```

即额定电压约 10%。但对于实际工程，应按你的测量比例与设备验收条件设定：

| 测量对象 | 示例起始失压阈值 |
|---|---:|
| 230 V AC 负载侧 | 20–30 V RMS |
| 110 V AC 控制电源 | 10–15 V RMS |
| 24 V DC 回路 | 2–5 V |
| PT/VT 二次侧 | 按二次额定电压的 5–20% |
| 电压变送器输出 | 按满量程、噪声与工程判据确定 |

## 9.3 人工事件标记

在操作切换时，可输入事件说明并点击：

```text
写入人工事件标记
```

示例：

```text
市电断开
开始 ATS 切换
备用源建立
负载恢复
人工复位
```

这些标记会写入 SQLite 的 `events` 表，用于事后分析和报告。

## 9.4 停止记录

点击：

```text
停止记录
```

程序会：

1. 停止实时轮询
2. 向示波器发送 `:STOP`
3. 写入录波停止时间和帧数
4. 关闭 SQLite 数据库文件

---

## 10. 数据回放

### 10.1 打开会话

点击：

```text
打开 SQLite 录波会话
```

选择：

```text
sessions\你的会话名称.sqlite
```

### 10.2 使用回放滑块

打开后：

- 滑块左端：第一帧
- 滑块右端：最后一帧
- 拖动滑块：显示相应帧的 CH1 和 CH2 波形
- 页面显示帧序号、Frame ID、UTC 时间戳和状态

### 10.3 导出趋势 CSV

点击：

```text
导出 RMS / 频率趋势 CSV
```

导出文件的格式：

```csv
frame_id,timestamp_utc,ch1_rms_v,ch2_rms_v,ch1_hz,ch2_hz,status
1,2026-09-10T05:35:00.111+00:00,229.8,228.9,50.01,50.00,NORMAL
2,2026-09-10T05:35:00.367+00:00,229.5,6.4,50.00,0.00,VOLTAGE_LOSS
3,2026-09-10T05:35:00.622+00:00,229.6,226.8,50.02,50.01,NORMAL
```

可用 Excel、Power BI、Pandas 或自建 Web Dashboard 做进一步分析。

---

## 11. 推荐的录波设置

| 应用场景 | 建议时基 | 采集类型 | 轮询周期 | 说明 |
|---|---:|---|---:|---|
| 长时间供电状态监视 | 0.5–10 s/div | NORM | 1–5 s | 建议主要记录 RMS/频率趋势 |
| ATS 切换过程 | 50–200 ms/div | NORM | 100–250 ms | 适合过程级切换分析 |
| UPS/BESS 接管过程 | 10–100 ms/div | NORM 或 PEAK | 100–250 ms | 可观察接管趋势和异常变化 |
| 继电器、接触器动作趋势 | 1–20 ms/div | PEAK | 100–250 ms | 可能漏掉帧间短暂现象 |
| 稳态 50 Hz 波形检查 | 5–20 ms/div | NORM | 250–500 ms | 每帧可覆盖数个周期 |

### 11.1 关于延迟与遗漏

本程序适用于：

- 你接受 USB / Windows / Python 带来的显示延迟
- 你要分析回传后的数据
- 你关注趋势、失压、恢复、频率、波形变化和事件时间线

本程序不保证：

- PC 相邻读取帧之间的高速暂态完全无遗漏
- 微秒级或毫秒级瞬态的准确发生时间
- 连续高速无损记录

若未来需要关键异常的精细暂态证据，建议在保留本程序连续记录的同时，在异常发生后使用 DS1102E 的停止/深存储读取模式，或使用专业电力故障录波器、高速 DAQ。

---

## 12. SQLite 数据结构

每个 `.sqlite` 会话包含三个核心表。

### 12.1 `session_info`

记录会话总体信息：

```text
application
created_utc
stopped_utc
frame_count
ch1_name
ch2_name
ch2_loss_threshold_v_rms
configured_poll_interval_ms
```

### 12.2 `frames`

每一帧记录：

```text
id
timestamp_utc
meta_json
ch1_raw
ch2_raw
ch1_rms
ch2_rms
ch1_hz
ch2_hz
status
```

其中：

- `ch1_raw`、`ch2_raw`：zlib 压缩后的原始波形字节
- `meta_json`：该帧的时基、偏置、通道量程、采样率和触发状态
- `status`：`NORMAL` 或 `VOLTAGE_LOSS`

### 12.3 `events`

记录自动或人工事件：

```text
id
timestamp_utc
frame_id
event_type
detail
```

---

## 13. 常见问题

### Q1：程序显示“找到 0 个 VISA 资源”

请检查：

1. DS1102E 是否已开机
2. 是否使用后方 USB Device（USB-B）口
3. USB 线是否是数据线
4. 是否安装 NI-VISA
5. 是否重新插拔 USB 线
6. 是否重新点击“扫描 VISA”
7. Windows 设备管理器中是否出现 USB 测试测量设备

可在 PyCharm Terminal 中测试：

```powershell
.\.venv\Scripts\python.exe -c "import pyvisa; rm=pyvisa.ResourceManager(); print(rm.visalib); print(rm.list_resources())"
```

### Q2：PyQtGraph 报错找不到 PySide6/PyQt

在项目 `.venv` 中安装：

```powershell
.\.venv\Scripts\python.exe -m pip install PySide6 pyqtgraph
```

检查 PyCharm Interpreter 是否为项目虚拟环境：

```text
.venv\Scripts\python.exe
```

### Q3：连接成功，但点击开始记录后报读取波形失败

建议依次测试：

```text
*IDN?
:TRIG:STAT?
:CHAN1:DISP ON
:CHAN2:DISP ON
:AUTO
```

然后再尝试记录。

### Q4：RMS 电压与示波器显示值有差异

先检查：

1. 软件中的探头倍率是否与探头物理倍率一致
2. 示波器通道探头倍率是否一致
3. 是否误用普通探头测量了需要差分探头的浮地/高压点
4. 交流/直流耦合设置是否正确
5. 电压换算是否已使用已知低压参考信号验证

建议先用函数发生器或安全的低压 50 Hz 交流源验证程序显示值。

### Q5：程序运行正常但没有波形

如果未连接示波器或未开始记录，图形区域为空是正常的。

连接并开始记录后，应确认：

```text
CH1 / CH2 已启用
示波器处于 RUN
探头已接入有效信号
输入量程未设置过大或过小
```

---

## 14. 电气安全要求

本程序不会改变示波器的输入和接地性质。

特别注意：

- 普通示波器探头地夹通常与保护地连接
- 不可将普通探头地夹任意连接至市电相线
- 不可将普通探头地夹连接在不同相线、不同浮地电源或不确定参考点之间
- 测量 ATS、UPS、BESS、逆变器、三相系统或配电盘时，应使用符合被测电压、瞬态和 CAT 条件的差分探头
- 也可采用经工程确认的 PT/VT 二次侧、隔离电压变送器或隔离测量前端
- 探头倍率、额定工作电压、共模范围与 CAT 等级必须满足现场要求

在首次接入真实交流系统前，建议先使用低压、安全隔离信号完成：

```text
USB 通信验证
SCPI 控制验证
实时显示验证
SQLite 记录验证
回放验证
CSV 导出验证
```

---

## 15. 推荐后续升级

后续可继续增加：

- 项目/盘柜/回路/操作者/工作票信息表单
- CH1/CH2 PT、VT、CT、变送器比例换算
- RMS 趋势独立页面
- 频率趋势独立页面
- 事件列表表格，点击自动定位至对应帧
- 自动截图与事件波形 PNG
- PDF 测试报告
- 失压与恢复分别设置不同阈值
- 连续 N 帧确认，减少瞬时噪声误报
- 保存示波器截图
- 停止后读取深存储波形
- 多会话统计与切换时间对比
- 原始数据 SHA-256 完整性校验

---

## 16. 典型操作流程

```text
1. 开机 DS1102E
2. 后方 USB Device 接到电脑
3. 启动本程序
4. 扫描 VISA
5. 选择 DS1102E 并连接
6. 读取 *IDN? 确认通讯
7. 设置 CH1/CH2、时基和采集方式
8. 点击“应用全部设置到 DS1102E”
9. 在“记录与回放”填写会话和工程信息
10. 点击“开始实时显示并记录”
11. 执行交流切换或监视过程
12. 必要时写入人工事件标记
13. 点击“停止记录”
14. 打开 SQLite 会话并使用滑块回放
15. 导出趋势 CSV，完成进一步分析或报告
```
