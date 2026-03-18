# WallWhisper 部署指南

WallWhisper 支持三种部署方式，根据你的设备和需求选择。

## 前置准备

### 1. 获取 API 密钥

| 服务 | 用途 | 获取地址 |
|------|------|----------|
| DeepSeek API | AI 内容生成 | https://platform.deepseek.com |
| 腾讯云 TTS | 文本转语音 | https://console.cloud.tencent.com/tts |
| 萤石开放平台 | 摄像头告警 | https://open.ys7.com |
| OpenClaw（可选）| Emily 人设+记忆 | https://openclaw.com |

### 2. 准备萤石摄像头

1. 在萤石云视频 App 中添加摄像头
2. 开启"活动检测提醒"（检测人或动物活动）
3. 记录设备序列号（设备标签上）
4. 记录设备验证码（如需使用摄像头喇叭功能）
5. 确认摄像头局域网 IP（路由器管理页面可查看）

## 方式一：本地运行（开发/测试）

适合在 PC/Mac 上开发测试，使用本地扬声器播放。

### 环境要求
- Python 3.9+
- 麦克风/扬声器（可选，用于本地播放）
- 与摄像头在同一局域网

### 步骤

```bash
# 1. 克隆项目
git clone https://github.com/treychen-369/WallWhisper.git
cd WallWhisper

# 2. 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 创建配置文件
cp config.example.yaml config.yaml

# 5. 编辑 config.yaml，至少填入:
#    - ai.api_key (DeepSeek)
#    - tts.app_id / tts.secret_id / tts.secret_key (腾讯云)
#    - ezviz.app_key / ezviz.app_secret / ezviz.device_serial (萤石)

# 6. 逐步测试
python run.py test_tts                    # 测试 TTS
python run.py test_tts "Hello! Can you say cat?"  # 测试自定义文本
python run.py test_ezviz                   # 测试萤石连接
python run.py test_speaker                 # 测试摄像头喇叭
python run.py test_full                    # 端到端测试

# 7. 启动 WallWhisper
python run.py emily
```

### 常见问题

**Q: PyAudio 安装失败？**
```bash
# Windows
pip install pipwin
pipwin install pyaudio

# Mac
brew install portaudio
pip install pyaudio

# Linux
sudo apt-get install portaudio19-dev
pip install pyaudio
```

**Q: 没有麦克风/扬声器？**

启用 `camera_speaker` 模式，音频直接推到摄像头喇叭：
```yaml
camera_speaker:
  enabled: true
  cam_ip: "YOUR_CAMERA_IP"
  cam_password: "YOUR_CAMERA_PASSWORD"
```

## 方式二：Docker 部署（推荐）

适合长期运行在服务器、NAS、或路由器上。

### 环境要求
- Docker 20+
- docker-compose (可选)
- 与摄像头在同一局域网

### 步骤

```bash
# 1. 克隆项目
git clone https://github.com/treychen/WallWhisper.git
cd WallWhisper

# 2. 准备配置文件
cp config.example.yaml config.docker.yaml
# 编辑 config.docker.yaml，注意：
#   - camera_speaker.ffmpeg_path 改为 "ffmpeg"（Docker 镜像自带）
#   - camera_speaker.enabled 设为 true（Docker 中无本地扬声器）

# 3. 本地构建并启动
docker-compose up -d --build

# 4. 查看日志
docker-compose logs -f emily

# 5. 停止
docker-compose down
```

### 使用预构建镜像

如果你使用 CNB.cool 或其他 CI 构建了镜像：

```bash
IMAGE=your-registry/wallwhisper:latest docker-compose up -d
```

### 资源限制

`docker-compose.yml` 已配置了适合路由器的资源限制：

| 参数 | 值 | 说明 |
|------|------|------|
| `mem_limit` | 128MB | 内存硬限制 |
| `mem_reservation` | 64MB | 内存软限制 |
| `cpus` | 0.5 | CPU 限制 |
| `pids_limit` | 32 | 进程数限制 |
| `oom_score_adj` | 500 | OOM 优先级（优先被杀，保护宿主机）|

> 如果你的设备内存充裕（如 NAS/服务器），可以适当调高这些限制。

## 方式三：小米路由器部署（极客玩法 🔧）

在支持 Docker 的小米路由器（如 7000/BE系列）上直接运行 WallWhisper。

### 前提条件

1. 路由器已安装 Docker（参考社区教程）
2. 有 USB 存储设备（Docker 镜像和数据存储在 USB 上）
3. 路由器能访问外网（拉取镜像和调用 API）

### 步骤

```bash
# 1. 在路由器上创建 WallWhisper 目录
ssh router
mkdir -p /opt/emily
mkdir -p /opt/emily/ezviz_token
mkdir -p /opt/emily/logs

# 2. 上传配置文件（从本地 PC）
# 使用 sync_router.py 或手动 scp
scp config.docker.yaml router:/opt/emily/

# 3. 上传部署脚本
scp deploy.sh router:/opt/emily/

# 4. 设置环境变量（根据你的实际路径）
export EMILY_IMAGE="your-registry/wallwhisper:latest"
export EMILY_DIR="/opt/emily"

# 5. 执行部署
ssh router sh /opt/emily/deploy.sh

# 6. 查看日志
ssh router docker logs --tail 30 wallwhisper
```

### deploy.sh 安全特性

部署脚本包含 7 步安全流程：

1. **预检** — 检查内存（<100MB 拒绝部署）和配置文件
2. **快照** — 保存当前镜像 ID 和配置备份
3. **拉取** — 下载新镜像
4. **停止** — 停止旧容器
5. **启动** — 启动新容器（含资源限制）
6. **健康检查** — 容器状态 + 日志检查 + 内存检查
7. **记录** — 保存成功部署状态

```bash
# 常用命令
sh deploy.sh              # 正常部署
sh deploy.sh --force      # 强制重新部署
sh deploy.sh --rollback   # 回滚到上一个版本
```

### 使用 sync_router.py 管理

在本地 PC 上运行 `sync_router.py`，安全地管理路由器上的 WallWhisper：

```bash
# 查看路由器 Emily 状态
python sync_router.py status

# 同步配置文件（带校验+备份+原子替换）
python sync_router.py config

# 查看配置差异
python sync_router.py config --diff

# 同步并重启
python sync_router.py config --restart
```

## 验证部署

不管使用哪种方式部署，可以通过以下方式验证：

1. **查看日志**：确认 WallWhisper 启动成功并开始轮询告警
2. **走过摄像头**：触发人形检测，等待 Emily 说英语
3. **检查资源**：确认内存和 CPU 使用正常

```bash
# Docker 日志
docker logs --tail 30 wallwhisper

# 资源监控
docker stats wallwhisper
```
