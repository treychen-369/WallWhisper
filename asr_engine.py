#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASR 语音识别引擎 — 腾讯云一句话识别 + 本地麦克风录音 + VAD 静音检测

支持两种输入源:
  1. 本地麦克风（PyAudio）— 用于开发测试
  2. PCM 数据直接传入 — 用于对接摄像头麦克风

核心流程:
  录音/获取PCM → VAD 检测语音活动 → 腾讯云一句话识别 → 返回文字
"""

import base64
import json
import logging
import re
import struct
import time
import threading
from typing import Optional, Tuple

# PyAudio 可选（Docker 环境不需要）
try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False

# 腾讯云 SDK
try:
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.asr.v20190614 import asr_client, models
    HAS_TENCENT_ASR = True
except ImportError:
    HAS_TENCENT_ASR = False


class VADDetector:
    """
    简单的 VAD (Voice Activity Detection) — 基于音量阈值的静音检测。
    用于判断用户是否正在说话、是否已停止说话。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_duration_ms: int = 30,
        silence_threshold: int = 500,
        speech_min_frames: int = 10,
        silence_max_frames: int = 30,
    ):
        """
        Args:
            sample_rate: 采样率
            frame_duration_ms: 每帧时长(ms)
            silence_threshold: 静音判定阈值（16-bit PCM 的 RMS 值）
            speech_min_frames: 至少连续这么多帧有声音才算"开始说话"
            silence_max_frames: 说话后连续这么多帧静音才算"说完了"
        """
        self.sample_rate = sample_rate
        self.frame_size = int(sample_rate * frame_duration_ms / 1000)  # 每帧采样点数
        self.silence_threshold = silence_threshold
        self.speech_min_frames = speech_min_frames
        self.silence_max_frames = silence_max_frames

        self._speech_frames = 0     # 连续有声帧计数
        self._silence_frames = 0    # 连续静音帧计数
        self._is_speaking = False   # 是否正在说话
        self._speech_started = False  # 是否曾经开始说话

    def reset(self):
        """重置状态"""
        self._speech_frames = 0
        self._silence_frames = 0
        self._is_speaking = False
        self._speech_started = False

    @staticmethod
    def rms(pcm_data: bytes) -> float:
        """计算 PCM 数据的 RMS (Root Mean Square) 音量"""
        if len(pcm_data) < 2:
            return 0.0
        n_samples = len(pcm_data) // 2
        samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * 2])
        if not samples:
            return 0.0
        sum_sq = sum(s * s for s in samples)
        return (sum_sq / n_samples) ** 0.5

    def process_frame(self, pcm_frame: bytes) -> Tuple[bool, bool]:
        """
        处理一帧 PCM 数据。

        Returns:
            (is_speech, speech_ended):
                is_speech: 当前帧是否有语音
                speech_ended: 说话是否已结束（先说后停的完整过程）
        """
        volume = self.rms(pcm_frame)
        is_speech = volume > self.silence_threshold

        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0

            if self._speech_frames >= self.speech_min_frames:
                self._is_speaking = True
                self._speech_started = True
        else:
            self._silence_frames += 1
            if self._silence_frames > 3:
                self._speech_frames = 0

        # 判断"说完了"：曾经开始说话 + 现在连续静音超过阈值
        speech_ended = (
            self._speech_started
            and self._silence_frames >= self.silence_max_frames
        )

        return is_speech, speech_ended

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    @property
    def speech_started(self) -> bool:
        return self._speech_started


class ASREngine:
    """
    腾讯云一句话识别引擎。

    使用方式：
        engine = ASREngine(config, logger)
        text = engine.recognize_pcm(pcm_data)       # 从 PCM 数据识别
        text = engine.listen_and_recognize(timeout=8)  # 从麦克风录音并识别
    """

    def __init__(self, asr_config: dict, logger: logging.Logger = None):
        """
        Args:
            asr_config: ASR 配置，包含:
                secret_id, secret_key: 腾讯云密钥（复用 TTS 的）
                engine_type: 引擎类型，默认 "16k_zh-PY"（中英粤混合）
                smart_engine: 智能双引擎模式（默认 true）
                    先用混合引擎识别，如果结果看起来是纯英文，
                    再用纯英文引擎重新识别以提高精度
                sample_rate: 采样率，默认 16000
                vad_silence_threshold: VAD 静音阈值，默认 500
                vad_silence_duration_ms: VAD 静音持续时间判定说完(ms)，默认 1500
                listen_timeout: 聆听超时(秒)，默认 8
                listen_max_duration: 最长录音时间(秒)，默认 15
        """
        self.config = asr_config
        self.logger = logger or logging.getLogger("asr_engine")

        self.secret_id = asr_config["secret_id"]
        self.secret_key = asr_config["secret_key"]
        self.engine_type = asr_config.get("engine_type", "16k_en")
        self.smart_engine = asr_config.get("smart_engine", True)
        self.sample_rate = int(asr_config.get("sample_rate", 16000))
        self.listen_timeout = float(asr_config.get("listen_timeout", 8))
        self.listen_max_duration = float(asr_config.get("listen_max_duration", 15))

        # VAD 参数
        vad_silence_threshold = int(asr_config.get("vad_silence_threshold", 500))
        vad_silence_ms = int(asr_config.get("vad_silence_duration_ms", 1500))
        frame_ms = 30  # 每帧 30ms
        silence_max_frames = max(1, vad_silence_ms // frame_ms)

        self.vad = VADDetector(
            sample_rate=self.sample_rate,
            frame_duration_ms=frame_ms,
            silence_threshold=vad_silence_threshold,
            silence_max_frames=silence_max_frames,
        )

        # 初始化腾讯云 ASR 客户端
        if not HAS_TENCENT_ASR:
            self.logger.warning("[ASR] tencentcloud-sdk-python 未安装，ASR 功能不可用")
            self.client = None
        else:
            cred = credential.Credential(self.secret_id, self.secret_key)
            http_profile = HttpProfile()
            http_profile.endpoint = "asr.tencentcloudapi.com"
            client_profile = ClientProfile()
            client_profile.httpProfile = http_profile
            self.client = asr_client.AsrClient(cred, "", client_profile)

    @staticmethod
    def _looks_like_english(text: str) -> bool:
        """
        判断文本是否看起来是纯英文（或几乎全英文）。
        用于决定是否需要用纯英文引擎重新识别以提高精度。
        """
        if not text:
            return False
        # 去掉标点和空格后，检查是否全是 ASCII 字母
        clean = re.sub(r"[^a-zA-Z\u4e00-\u9fff]", "", text)
        if not clean:
            return False
        english_chars = sum(1 for c in clean if c.isascii())
        return english_chars / len(clean) > 0.9  # 90% 以上是英文字母

    def _call_asr_api(self, pcm_data: bytes, engine_type: str) -> Optional[str]:
        """
        调用腾讯云一句话识别 API（底层方法）。

        Args:
            pcm_data: PCM 数据
            engine_type: 引擎类型

        Returns:
            识别文本，失败返回 None
        """
        try:
            data_b64 = base64.b64encode(pcm_data).decode("utf-8")

            req = models.SentenceRecognitionRequest()
            req.ProjectId = 0
            req.SubServiceType = 2
            req.EngSerViceType = engine_type
            req.SourceType = 1  # 1 = 本地上传
            req.VoiceFormat = "pcm"
            req.UsrAudioKey = f"emily_{int(time.time())}"
            req.Data = data_b64
            req.DataLen = len(pcm_data)

            resp = self.client.SentenceRecognition(req)
            result_text = resp.Result
            if result_text:
                result_text = result_text.strip()
            return result_text if result_text else None

        except Exception as e:
            self.logger.error(f"[ASR] API 调用失败 (engine={engine_type}): {e}")
            return None

    def recognize_pcm(self, pcm_data: bytes, engine_type: str = None) -> Optional[str]:
        """
        对 PCM 数据进行一句话识别。

        智能双引擎策略（smart_engine=true 时）:
          1. 先用纯英文引擎 (16k_en) 识别 — Emily 英语教学场景优先
          2. 如果英文引擎无结果，用混合引擎 (16k_zh-PY) 兜底
             （用户偶尔可能说中文，如"暂停"、"下一个"等指令）

        Args:
            pcm_data: 16-bit LE, 单声道, 16kHz PCM 数据
            engine_type: 强制指定引擎类型（跳过智能策略）

        Returns:
            识别出的文本，失败返回 None
        """
        if not self.client:
            self.logger.error("[ASR] ASR 客户端未初始化")
            return None

        if not pcm_data or len(pcm_data) < 1000:
            self.logger.warning(f"[ASR] PCM 数据太短 ({len(pcm_data)} bytes)，跳过识别")
            return None

        duration = len(pcm_data) / (self.sample_rate * 2)
        if duration > 60:
            self.logger.error(f"[ASR] 音频太长 ({duration:.1f}s > 60s)，一句话识别最大支持60秒")
            return None

        self.logger.info(f"[ASR] 开始识别 ({len(pcm_data)} bytes, {duration:.1f}s)...")
        start = time.time()

        # 确定使用的引擎
        use_engine = engine_type or self.engine_type
        use_smart = (
            self.smart_engine
            and engine_type is None  # 没有强制指定引擎
            and use_engine == "16k_en"  # 当前是纯英文引擎
        )

        # 第一轮识别（默认纯英文引擎）
        result = self._call_asr_api(pcm_data, use_engine)
        elapsed = time.time() - start

        if result:
            self.logger.info(f"[ASR] 识别完成 ({elapsed:.1f}s, engine={use_engine}): \"{result}\"")
            return result

        # 智能双引擎：英文引擎无结果时，用混合引擎兜底
        if use_smart:
            self.logger.info(f"[ASR] [SMART] 英文引擎无结果，尝试混合引擎 16k_zh-PY 兜底...")
            start2 = time.time()
            fallback = self._call_asr_api(pcm_data, "16k_zh-PY")
            elapsed2 = time.time() - start2

            if fallback:
                total = time.time() - start
                self.logger.info(
                    f"[ASR] [SMART] 混合引擎识别成功 ({elapsed2:.1f}s): "
                    f"\"{fallback}\" (总耗时 {total:.1f}s)"
                )
                return fallback
            else:
                self.logger.info(f"[ASR] [SMART] 混合引擎也无结果")

        self.logger.info(f"[ASR] 识别完成 ({elapsed:.1f}s): 无结果")
        return None

    def listen_from_microphone(self, timeout: float = None, max_duration: float = None) -> Optional[bytes]:
        """
        从本地麦克风录音，使用 VAD 检测说话开始和结束。

        Args:
            timeout: 等待开始说话的超时时间(秒)
            max_duration: 最长录音时间(秒)

        Returns:
            录到的 PCM 数据，如果超时未检测到语音返回 None
        """
        if not HAS_PYAUDIO:
            self.logger.error("[ASR] PyAudio 未安装，无法使用本地麦克风")
            return None

        timeout = timeout or self.listen_timeout
        max_duration = max_duration or self.listen_max_duration
        frame_ms = 30
        frame_size = int(self.sample_rate * frame_ms / 1000)
        chunk_bytes = frame_size * 2  # 16-bit

        self.vad.reset()
        pcm_chunks = []
        speech_pcm = []

        pa = pyaudio.PyAudio()
        stream = None

        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=frame_size,
            )

            self.logger.info(f"[ASR] [MIC] 开始聆听 (超时{timeout}s, 最长{max_duration}s)...")
            listen_start = time.time()
            speech_start_time = None

            while True:
                elapsed = time.time() - listen_start

                # 超时检查：等待说话超时
                if not self.vad.speech_started and elapsed > timeout:
                    self.logger.info(f"[ASR] 聆听超时 ({timeout}s)，未检测到语音")
                    return None

                # 最长录音时间检查
                if elapsed > max_duration:
                    self.logger.info(f"[ASR] 达到最长录音时间 ({max_duration}s)")
                    break

                # 读取一帧
                try:
                    frame_data = stream.read(frame_size, exception_on_overflow=False)
                except Exception as e:
                    self.logger.warning(f"[ASR] 读取麦克风失败: {e}")
                    continue

                is_speech, speech_ended = self.vad.process_frame(frame_data)

                # 显示音量（调试用）
                volume = self.vad.rms(frame_data)
                if self.vad.speech_started and not speech_ended:
                    # 说话中，收集音频
                    speech_pcm.append(frame_data)
                    if speech_start_time is None:
                        speech_start_time = time.time()
                        self.logger.info(f"[ASR] >>> 检测到语音 (音量:{volume:.0f})")
                elif not self.vad.speech_started and is_speech:
                    # 可能要开始说了，预缓存
                    pcm_chunks.append(frame_data)
                    # 只保留最近 0.5 秒的预缓存
                    max_pre_frames = int(500 / frame_ms)
                    if len(pcm_chunks) > max_pre_frames:
                        pcm_chunks = pcm_chunks[-max_pre_frames:]

                if speech_ended:
                    speech_duration = time.time() - (speech_start_time or listen_start)
                    self.logger.info(f"[ASR] [END] 语音结束 (说话时长:{speech_duration:.1f}s)")
                    break

            # 合并 PCM 数据：预缓存 + 语音部分
            all_pcm = b"".join(pcm_chunks) + b"".join(speech_pcm)

            if len(all_pcm) < self.sample_rate * 2 * 0.3:  # 至少 0.3 秒
                self.logger.info(f"[ASR] 录音太短 ({len(all_pcm)} bytes)，忽略")
                return None

            duration = len(all_pcm) / (self.sample_rate * 2)
            self.logger.info(f"[ASR] 录音完成: {len(all_pcm)} bytes, {duration:.1f}s")
            return all_pcm

        except Exception as e:
            self.logger.error(f"[ASR] 麦克风录音异常: {e}")
            return None
        finally:
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            pa.terminate()

    def listen_and_recognize(
        self,
        timeout: float = None,
        max_duration: float = None,
        engine_type: str = None,
    ) -> Optional[str]:
        """
        从麦克风录音 → VAD 检测 → ASR 识别，一步到位。

        Returns:
            识别出的文本，失败或超时返回 None
        """
        pcm_data = self.listen_from_microphone(timeout, max_duration)
        if not pcm_data:
            return None

        return self.recognize_pcm(pcm_data, engine_type)


# ============================================
# 独立测试入口
# ============================================

def main():
    """独立测试：python asr_engine.py"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import os
    from config_loader import load_and_validate_config

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("asr_test")

    config = load_and_validate_config("test_asr")

    # ASR 配置: 复用 TTS 的密钥
    asr_config = config.get("asr", {})
    if not asr_config.get("secret_id"):
        asr_config["secret_id"] = config["tts"]["secret_id"]
    if not asr_config.get("secret_key"):
        asr_config["secret_key"] = config["tts"]["secret_key"]

    engine = ASREngine(asr_config, logger)

    logger.info("=" * 50)
    logger.info("ASR 语音识别测试")
    logger.info("请对着麦克风说话...")
    logger.info("=" * 50)

    result = engine.listen_and_recognize()
    if result:
        logger.info(f"✅ 识别结果: {result}")
    else:
        logger.warning("❌ 未识别到内容")


if __name__ == "__main__":
    import os
    main()
