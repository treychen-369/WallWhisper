# WallWhisper 系统架构详解

## 整体架构

WallWhisper 采用**事件驱动 + 流水线**架构，核心流程：

```
摄像头人形检测 → 告警轮询 → 场景匹配 → AI 内容生成 → TTS 语音合成 → 音频播放
```

## 模块说明

### 1. 感知层：`ezviz_monitor.py`

**职责**：轮询萤石云 API 获取人形检测告警

```
萤石摄像头 → 萤石云 (open.ys7.com) → ezviz_monitor.py 轮询
```

- 使用萤石开放平台 REST API，每 5 秒轮询一次告警列表
- 自动管理 AccessToken 生命周期（过期自动刷新）
- Token 持久化到本地文件，重启后无需重新获取
- 支持过滤告警类型（人体检测、移动侦测等）

### 2. 决策层：`emily_v2.py` — TriggerTracker + 场景匹配

**职责**：根据触发频率和时间段决定交互模式

```
告警事件 → TriggerTracker(冷却判断) → 时间段匹配 → 确定模式(pass_by/interact/scheduled)
```

- **TriggerTracker**：维护触发历史，实现冷却时间和防抖
  - `pass_by_cooldown`：路过打招呼冷却（默认 120s）
  - `interact_cooldown`：互动教学冷却（默认 180s）
  - `dual_window`：双镜头联合判断窗口（广角+云台同时触发 → interact）
- **时间段匹配**：根据 `config.yaml` 中的 `time_scenes` 配置，匹配当前时间段的场景
- **深夜静默**：`quiet_hours` 期间不触发被动模式

### 3. 生成层：DeepSeek API / OpenClaw Emily API

**职责**：生成个性化英语内容

**模式 A — 直连 DeepSeek**：
```
emily_v2.py → OpenAI SDK → DeepSeek API → 英语内容
```
- 使用 OpenAI SDK 兼容接口
- System prompt 内置教学策略和场景约束

**模式 B — OpenClaw Emily API（推荐）**：
```
emily_v2.py → HTTP POST → emily-api.py → openclaw CLI → Emily Agent → 英语内容
```
- emily-api.py 部署在 OpenClaw 服务器上，作为 HTTP 网关
- OpenClaw Emily Agent 拥有完整人设（SOUL.md）、家庭信息（USER.md）、教学记忆（MEMORY.md）
- 长期记忆让 Emily "记住"教过的单词，避免重复

### 4. 语音层：`tts_stream.py`

**职责**：文本转语音（流式）

```
英语文本 → 腾讯云 WebSocket TTS → PCM 音频流 → 播放/推送
```

- WebSocket 长连接，支持流式返回
- 双模式输出：
  - `speak()` — 边接收边播放（PyAudio 本地）
  - `synthesize()` — 接收完整 PCM 数据返回（供摄像头喇叭使用）
- 中英文分段合成，英文用 voice_type=101009，中文用 voice_type=101009

### 5. 播放层：`camera_speaker.py`

**职责**：通过 RTSP Backchannel 将音频推送到摄像头喇叭

```
PCM 数据 → FFmpeg (PCM→AAC 转码) → RTP 封装 → RTSP Backchannel → 摄像头喇叭
```

**RTSP Backchannel 技术细节**：
1. DESCRIBE 时携带 `Require: www.onvif.org/ver20/backchannel` 头
2. SDP 返回额外的 sendonly audio track (trackID=4)
3. SETUP trackID=4，使用 TCP interleaved 模式，`mode=record`
4. 仅支持 AAC 编码 (PT=104, 16kHz)
5. 通过同一 TCP 连接发送 RTP 帧

### 6. 对话层：`conversation.py` + `asr_engine.py` + `camera_mic.py`

**职责**：实现多轮对话（Emily 说 → 聆听回应 → AI 回复）

```
Emily TTS播放 → camera_mic.py 录音 → asr_engine.py 识别 → AI 生成回复 → 循环
```

- `camera_mic.py`：通过 RTSP 从摄像头获取音频流（RTSP → AAC → PCM）
- `asr_engine.py`：腾讯云一句话识别，支持中英粤混合
- `conversation.py`：管理多轮对话状态、轮数限制、降级策略

## 数据流图

```
                    ┌─────────────────────────────────────┐
                    │          config.yaml                 │
                    │  (所有密钥和配置集中管理)              │
                    └──────────┬──────────────────────────┘
                               │
                    ┌──────────▼──────────────────────────┐
                    │       config_loader.py               │
                    │  (加载配置 + 启动前校验)              │
                    └──────────┬──────────────────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                  ▼
    ezviz_monitor.py    scheduled_tasks     quiet_hours
    (告警轮询)          (定时任务)          (静默保护)
            │                  │
            └───────┬──────────┘
                    ▼
            emily_v2.py (TriggerTracker + 场景匹配)
                    │
            ┌───────┴───────┐
            ▼               ▼
       DeepSeek API    OpenClaw API
            │               │
            └───────┬───────┘
                    ▼
            tts_stream.py (WebSocket 流式 TTS)
                    │
            ┌───────┴───────┐
            ▼               ▼
        PyAudio         camera_speaker.py
      (本地播放)      (RTSP Backchannel)
```

## 资源占用

在小米 7000 路由器上的实测数据：

| 指标 | 日常 | 峰值 | 限制 |
|------|------|------|------|
| 内存 | 60-80MB | ~100MB | 128MB (硬限制) |
| CPU | <5% | ~30% (TTS+推送时) | 0.5 核 |
| 进程数 | 8-12 | ~20 | 32 |
| 网络 | ~1KB/s (轮询) | ~50KB/s (TTS流) | 不限 |
