# WallWhisper - 家庭隐形英语外教 - Docker 部署镜像
# 适配 ARM64 (小米7000路由器) 和 x86_64 (PC/服务器)
#
# 构建: docker build -t wallwhisper .
# 运行: docker run -d --name wallwhisper --network host --restart unless-stopped wallwhisper

FROM python:3.11-slim

# 安装系统依赖: FFmpeg (PCM→AAC 转码必需)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 先复制 requirements 以利用 Docker 缓存
COPY requirements-docker.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件 (配置文件通过 volume 挂载，不 bake 进镜像)
COPY run.py config_loader.py emily_v2.py emily.py tts_stream.py ezviz_monitor.py camera_speaker.py camera_mic.py asr_engine.py conversation.py ./

# 创建日志和音频目录
RUN mkdir -p logs audio

# 健康检查: 确认 Python 主进程存活 (slim 镜像无 pgrep，用 /proc/1/cmdline 替代)
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD cat /proc/1/cmdline | tr '\0' ' ' | grep -q "run.py emily" || exit 1

# 默认启动 Emily 主程序
CMD ["python", "run.py", "emily"]
