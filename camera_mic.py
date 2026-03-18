#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头麦克风音频采集模块 — 通过 RTSP 从萤石 C7 摄像头拉取音频。

功能:
  1. 连接摄像头 RTSP 流，拉取实时音频
  2. 用 FFmpeg 解码为 16kHz 16-bit PCM
  3. 提供 VAD 检测 + 录音功能，对接 ASR

用法:
    mic = CameraMic(config, logger)
    # 录一段话（VAD 自动检测开始/结束）
    pcm_data = mic.listen(timeout=8, max_duration=15)
    # 测试摄像头麦克风是否正常
    mic.test()
"""

import logging
import os
import struct
import subprocess
import tempfile
import threading
import time
from typing import Optional


class CameraMic:
    """
    通过 RTSP 从摄像头拉取音频，解码为 PCM。

    底层使用 FFmpeg 拉 RTSP 流并实时输出 PCM 数据（管道模式），
    无需额外安装 GStreamer 或 OpenCV。
    """

    def __init__(self, config: dict, logger: logging.Logger = None):
        """
        config 需包含 camera_speaker 的连接信息（复用）:
            cam_ip, cam_rtsp_port, cam_user, cam_password
            ffmpeg_path (可选)
        以及可选的 camera_mic 专属配置:
            sample_rate: 输出采样率，默认 16000
            vad_silence_threshold: VAD 静音阈值 (RMS)，默认 300
            vad_silence_duration_ms: 连续静音多久判定说完(ms)，默认 1500
        """
        # 从 camera_speaker 或 camera_mic 配置中获取连接信息
        self.cam_ip = config.get("cam_ip", "")
        self.cam_port = config.get("cam_rtsp_port", 554)
        self.cam_user = config.get("cam_user", "admin")
        self.cam_password = config.get("cam_password", "")
        self.ffmpeg_path = config.get("ffmpeg_path", "ffmpeg")
        self.sample_rate = int(config.get("sample_rate", 16000))
        self.logger = logger or logging.getLogger("camera_mic")

        # VAD 参数（摄像头远场拾音，底噪比本地麦克风高很多，阈值需要更大）
        self.vad_threshold = int(config.get("vad_silence_threshold", 800))
        self.vad_silence_ms = int(config.get("vad_silence_duration_ms", 1500))

        # 回声抑制参数
        # Emily 通过同一摄像头喇叭播放后，麦克风会捕获残余回声。
        # echo_guard_ms: 连接后前 N 毫秒内的音频全部忽略（等回声衰减）
        # echo_volume_ceiling: 超过此音量视为回声/噪音而非人声（人声远场RMS通常<3000）
        # echo_sustained_frames: 连续 N 帧超过 ceiling → 判定为回声，丢弃并重置
        self.echo_guard_ms = int(config.get("echo_guard_ms", 1500))
        self.echo_volume_ceiling = int(config.get("echo_volume_ceiling", 5000))
        self.echo_sustained_frames = int(config.get("echo_sustained_frames", 30))

    def _build_rtsp_url(self, channel: int = 1, audio_only: bool = False) -> str:
        """
        构建 RTSP URL（萤石 C7 格式）。

        Args:
            channel: RTSP 通道 (1=广角, 2=云台)
            audio_only: True 使用 audiostream 路径（音质更好，适合 ASR）
                        False 使用 av_stream 路径（音视频混合）
        """
        stream_path = "audiostream" if audio_only else "av_stream"
        return (
            f"rtsp://{self.cam_user}:{self.cam_password}"
            f"@{self.cam_ip}:{self.cam_port}/h264/ch{channel}/main/{stream_path}"
        )

    def _rms(self, pcm_data: bytes) -> float:
        """计算 PCM 16-bit 数据的 RMS 音量"""
        if len(pcm_data) < 2:
            return 0.0
        n_samples = len(pcm_data) // 2
        samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
        if not samples:
            return 0.0
        return (sum(s * s for s in samples) / len(samples)) ** 0.5

    def capture_audio(self, duration: float = 5.0, channel: int = 1) -> Optional[bytes]:
        """
        从摄像头 RTSP 流录制指定时长的音频，返回 PCM 数据。

        Args:
            duration: 录制时长(秒)
            channel: RTSP 通道 (1=广角, 2=云台)

        Returns:
            16-bit LE, 单声道, 16kHz PCM 数据；失败返回 None
        """
        rtsp_url = self._build_rtsp_url(channel, audio_only=True)
        self.logger.info(f"[CAM_MIC] 开始从摄像头录音 ({duration:.1f}s)...")
        self.logger.debug(f"[CAM_MIC] RTSP: rtsp://{self.cam_user}:***@{self.cam_ip}:{self.cam_port}")

        try:
            cmd = [
                self.ffmpeg_path, "-y",
                "-rtsp_transport", "tcp",       # TCP 传输更稳定
                "-i", rtsp_url,
                "-vn",                          # 不要视频
                "-acodec", "pcm_s16le",         # 输出 PCM 16-bit LE
                "-ar", str(self.sample_rate),   # 16kHz
                "-ac", "1",                     # 单声道
                "-t", str(duration),            # 录制时长
                "-f", "s16le",                  # 裸 PCM 格式
                "pipe:1"                        # 输出到 stdout
            ]

            self.logger.debug(f"[CAM_MIC] FFmpeg 命令: {' '.join(cmd[:6])}...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=duration + 15,  # 额外给 15 秒建立连接
            )

            if result.returncode != 0:
                stderr = result.stderr.decode(errors="replace")[:300]
                self.logger.error(f"[CAM_MIC] FFmpeg 失败 (code={result.returncode}): {stderr}")
                return None

            pcm_data = result.stdout
            if not pcm_data or len(pcm_data) < 1000:
                self.logger.warning(f"[CAM_MIC] 获取的音频数据太少 ({len(pcm_data)} bytes)")
                return None

            actual_duration = len(pcm_data) / (self.sample_rate * 2)
            avg_rms = self._rms(pcm_data)
            self.logger.info(
                f"[CAM_MIC] 录音完成: {len(pcm_data)} bytes, "
                f"{actual_duration:.1f}s, 平均音量: {avg_rms:.0f}"
            )
            return pcm_data

        except subprocess.TimeoutExpired:
            self.logger.error(f"[CAM_MIC] FFmpeg 超时 ({duration + 15}s)")
            return None
        except FileNotFoundError:
            self.logger.error(f"[CAM_MIC] FFmpeg 未找到: {self.ffmpeg_path}")
            return None
        except Exception as e:
            self.logger.error(f"[CAM_MIC] 录音异常: {e}")
            return None

    def listen(self, timeout: float = 8.0, max_duration: float = 15.0,
               channel: int = 1) -> Optional[bytes]:
        """
        从摄像头麦克风录音，使用 VAD 检测说话开始和结束。

        流程:
          1. 启动 FFmpeg 进程，持续拉取 RTSP 音频流
          2. 实时读取 PCM 数据，检测音量变化
          3. 检测到语音开始 → 开始录制
          4. 检测到语音结束（连续静音超阈值）→ 停止录制
          5. 返回 PCM 数据

        Args:
            timeout: 等待开始说话的超时(秒)
            max_duration: 最长录音时间(秒)
            channel: RTSP 通道

        Returns:
            PCM 数据，超时/失败返回 None
        """
        rtsp_url = self._build_rtsp_url(channel, audio_only=True)

        cmd = [
            self.ffmpeg_path,
            "-rtsp_transport", "tcp",
            "-i", rtsp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1"
        ]

        self.logger.info(
            f"[CAM_MIC] [LISTEN] 开始聆听 "
            f"(超时{timeout}s, 最长{max_duration}s, 阈值{self.vad_threshold})"
        )

        process = None
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            # 帧参数
            frame_ms = 30
            frame_size = int(self.sample_rate * frame_ms / 1000)  # 480 samples
            chunk_bytes = frame_size * 2  # 960 bytes per frame

            speech_started = False
            speech_start_time = None
            pre_buffer = []       # 预缓存（说话前的音频）
            speech_pcm = []       # 语音数据
            silence_frames = 0    # 连续静音帧数
            silence_frames_needed = int(self.vad_silence_ms / frame_ms)

            listen_start = time.time()

            # 等待 FFmpeg 建立连接（最多等 5 秒出数据）
            self.logger.debug("[CAM_MIC] 等待 RTSP 连接...")
            connect_timeout = 8.0
            first_data_time = None

            while True:
                elapsed = time.time() - listen_start

                # 检查进程是否还活着
                if process.poll() is not None:
                    stderr = process.stderr.read().decode(errors="replace")[:300]
                    self.logger.error(f"[CAM_MIC] FFmpeg 进程退出: {stderr}")
                    return None

                # 读取一帧 PCM 数据
                try:
                    frame_data = process.stdout.read(chunk_bytes)
                except Exception as e:
                    self.logger.warning(f"[CAM_MIC] 读取失败: {e}")
                    continue

                if not frame_data or len(frame_data) < chunk_bytes:
                    if elapsed > connect_timeout and first_data_time is None:
                        self.logger.error(f"[CAM_MIC] RTSP 连接超时 ({connect_timeout}s)")
                        return None
                    time.sleep(0.01)
                    continue

                if first_data_time is None:
                    first_data_time = time.time()
                    connect_delay = first_data_time - listen_start
                    self.logger.info(f"[CAM_MIC] RTSP 连接成功 (延迟 {connect_delay:.1f}s)")
                    # 重置计时器（从收到第一帧数据开始计时）
                    listen_start = time.time()
                    elapsed = 0

                volume = self._rms(frame_data)
                is_speech = volume > self.vad_threshold

                # 等待说话超时
                if not speech_started and elapsed > timeout:
                    self.logger.info(f"[CAM_MIC] 聆听超时 ({timeout}s)，未检测到语音")
                    return None

                # 最长录音时间
                if elapsed > max_duration:
                    self.logger.info(f"[CAM_MIC] 达到最长录音时间 ({max_duration}s)")
                    break

                if not speech_started:
                    if is_speech:
                        speech_started = True
                        speech_start_time = time.time()
                        silence_frames = 0
                        self.logger.info(f"[CAM_MIC] >>> 检测到语音 (音量:{volume:.0f})")
                        # 将预缓存加入语音数据
                        speech_pcm.extend(pre_buffer)
                        speech_pcm.append(frame_data)
                    else:
                        # 预缓存最近 0.5 秒
                        pre_buffer.append(frame_data)
                        max_pre_frames = int(500 / frame_ms)
                        if len(pre_buffer) > max_pre_frames:
                            pre_buffer = pre_buffer[-max_pre_frames:]
                else:
                    # 说话中
                    speech_pcm.append(frame_data)

                    if is_speech:
                        silence_frames = 0
                    else:
                        silence_frames += 1

                    # 连续静音超过阈值 → 说完了
                    if silence_frames >= silence_frames_needed:
                        speech_duration = time.time() - speech_start_time
                        self.logger.info(
                            f"[CAM_MIC] [END] 语音结束 (说话时长:{speech_duration:.1f}s)"
                        )
                        break

            # 合并 PCM
            all_pcm = b"".join(speech_pcm)
            if len(all_pcm) < self.sample_rate * 2 * 0.3:  # 至少 0.3 秒
                self.logger.info(f"[CAM_MIC] 录音太短 ({len(all_pcm)} bytes)，忽略")
                return None

            duration = len(all_pcm) / (self.sample_rate * 2)
            self.logger.info(f"[CAM_MIC] 录音完成: {len(all_pcm)} bytes, {duration:.1f}s")
            return all_pcm

        except Exception as e:
            self.logger.error(f"[CAM_MIC] 聆听异常: {e}")
            return None
        finally:
            if process:
                try:
                    process.kill()
                    process.wait(timeout=3)
                except Exception:
                    pass

    def test(self) -> bool:
        """
        测试摄像头麦克风是否正常工作。

        测试步骤:
          1. 测试 RTSP 连接能否建立
          2. 录制 3 秒音频
          3. 检查音频数据是否有效
          4. 分析音量分布

        Returns:
            True = 麦克风正常, False = 有问题
        """
        self.logger.info("=" * 55)
        self.logger.info("[CAM_MIC] 摄像头麦克风测试")
        self.logger.info(f"[CAM_MIC] 摄像头: {self.cam_ip}:{self.cam_port}")
        self.logger.info(f"[CAM_MIC] FFmpeg: {self.ffmpeg_path}")
        self.logger.info("=" * 55)

        # Step 1: 基础连接测试 — 录 3 秒静音
        self.logger.info("\n[TEST] Step 1: 连接 RTSP 并录制 3 秒...")
        pcm_data = self.capture_audio(duration=3.0)

        if not pcm_data:
            self.logger.error("[TEST] [FAIL] RTSP 音频采集失败! 请检查:")
            self.logger.error("  - 摄像头 IP 和端口是否正确")
            self.logger.error("  - 用户名/密码是否正确")
            self.logger.error("  - 摄像头是否在线")
            self.logger.error("  - FFmpeg 路径是否正确")
            return False

        self.logger.info("[TEST] [OK] RTSP 音频采集成功!")

        # Step 2: 分析音频质量
        self.logger.info("\n[TEST] Step 2: 分析音频质量...")
        duration = len(pcm_data) / (self.sample_rate * 2)
        overall_rms = self._rms(pcm_data)

        # 分段分析音量
        segment_ms = 100  # 100ms 一段
        segment_bytes = int(self.sample_rate * segment_ms / 1000) * 2
        segments = []
        for i in range(0, len(pcm_data) - segment_bytes, segment_bytes):
            seg = pcm_data[i:i + segment_bytes]
            segments.append(self._rms(seg))

        if segments:
            min_vol = min(segments)
            max_vol = max(segments)
            avg_vol = sum(segments) / len(segments)
        else:
            min_vol = max_vol = avg_vol = overall_rms

        self.logger.info(f"[TEST] 录音时长: {duration:.1f}s")
        self.logger.info(f"[TEST] 总体 RMS:  {overall_rms:.0f}")
        self.logger.info(f"[TEST] 音量范围:  {min_vol:.0f} ~ {max_vol:.0f} (平均 {avg_vol:.0f})")

        # Step 3: 判断麦克风状态
        self.logger.info("\n[TEST] Step 3: 诊断结果...")

        if overall_rms < 5:
            self.logger.warning("[TEST] [WARN] 音量极低 (RMS < 5), 可能麦克风被禁用或硬件故障")
            self.logger.warning("  建议: 检查萤石 APP 中的麦克风设置")
            return False
        elif overall_rms < 50:
            self.logger.info("[TEST] [OK] 环境安静, 麦克风工作正常 (低底噪)")
        elif overall_rms < 500:
            self.logger.info("[TEST] [OK] 麦克风工作正常 (有环境音)")
        else:
            self.logger.info("[TEST] [OK] 麦克风工作正常 (环境较嘈杂)")

        # Step 4: 测试 VAD 聆听（可选，让用户说话）
        self.logger.info(f"\n[TEST] Step 4: VAD 聆听测试")
        self.logger.info(f"[TEST] 请对着摄像头说话 (5秒内开口, 阈值={self.vad_threshold})...")
        vad_pcm = self.listen(timeout=5.0, max_duration=8.0)

        if vad_pcm:
            vad_duration = len(vad_pcm) / (self.sample_rate * 2)
            vad_rms = self._rms(vad_pcm)
            self.logger.info(f"[TEST] [OK] VAD 检测成功! 录到 {vad_duration:.1f}s, 音量 {vad_rms:.0f}")
        else:
            self.logger.info("[TEST] [INFO] VAD 未检测到语音 (可能没有对着摄像头说话)")
            self.logger.info("    这不影响麦克风硬件功能判断")

        # 总结
        self.logger.info("\n" + "=" * 55)
        self.logger.info("[TEST] [PASS] 摄像头麦克风测试通过!")
        self.logger.info(f"[TEST] 环境底噪: RMS {avg_vol:.0f}")
        self.logger.info(f"[TEST] VAD 阈值建议: {max(int(avg_vol * 2), 200)} ~ {max(int(avg_vol * 3), 500)}")
        self.logger.info("=" * 55)
        return True


# ============================================
# 独立测试
# ============================================

def main():
    """独立运行测试"""
    import yaml
    from pathlib import Path

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("camera_mic")

    config_path = Path(__file__).parent / "config.yaml"
    if not config_path.exists():
        logger.error(f"找不到 config.yaml: {config_path}")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    speaker_cfg = config.get("camera_speaker", {})
    if not speaker_cfg.get("cam_ip"):
        logger.error("config.yaml 中未配置 camera_speaker.cam_ip")
        return

    mic = CameraMic(speaker_cfg, logger)
    mic.test()


if __name__ == "__main__":
    main()
