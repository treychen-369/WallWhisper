#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流式 TTS 引擎 - 腾讯云 WebSocket 实时语音合成 + PyAudio 实时播放
独立模块，可单独测试: python tts_stream.py "Hello, this is a test."
"""

import os
import sys
import time
import json
import hmac
import hashlib
import base64
import struct
import threading
import logging
from uuid import uuid4
from urllib.parse import quote

import websocket  # websocket-client 库

# PyAudio 可选 — Docker/路由器环境中可能不可用（使用摄像头喇叭输出时不需要）
try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False

# ============================================
# 签名生成
# ============================================

def generate_signature(params: dict, secret_key: str) -> str:
    """
    按腾讯云实时语音合成 WebSocket 接口的签名规范生成签名。
    1. 对除 Signature 之外的所有参数按字典序排序拼接
    2. 拼接请求方法 + 域名地址 + 请求参数
    3. 用 HMAC-SHA1 + Base64 编码
    """
    # 按字典序排序参数
    sorted_params = sorted(params.items(), key=lambda x: x[0])
    query_str = "&".join(f"{k}={v}" for k, v in sorted_params)

    # 签名原文: GET + 域名 + 路径 + ? + 参数
    sign_str = f"GETtts.cloud.tencent.com/stream_ws?{query_str}"

    # HMAC-SHA1 加密 + Base64 编码
    hmac_digest = hmac.new(
        secret_key.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha1
    ).digest()
    signature = base64.b64encode(hmac_digest).decode("utf-8")

    return signature


def build_ws_url(text: str, tts_config: dict, session_id: str = None) -> str:
    """
    构建 WebSocket 连接 URL，包含签名。
    """
    if session_id is None:
        session_id = str(uuid4())

    timestamp = int(time.time())
    expired = timestamp + 86400  # 有效期 24 小时

    params = {
        "Action": "TextToStreamAudioWS",
        "AppId": int(tts_config["app_id"]),
        "SecretId": tts_config["secret_id"],
        "Timestamp": timestamp,
        "Expired": expired,
        "SessionId": session_id,
        "Text": text,
        "VoiceType": int(tts_config.get("voice_type", 501009)),
        "Codec": tts_config.get("codec", "pcm"),
        "SampleRate": int(tts_config.get("sample_rate", 16000)),
        "Speed": float(tts_config.get("speed", 0)),
        "Volume": float(tts_config.get("volume", 0)),
    }

    # 生成签名（使用原始参数值，不 urlencode）
    signature = generate_signature(params, tts_config["secret_key"])

    # 对 Signature 进行 urlencode
    params["Signature"] = signature

    # 对 Text 进行 urlencode
    text_encoded = quote(str(params["Text"]), safe="")
    signature_encoded = quote(str(params["Signature"]), safe="")

    # 构建最终 URL（Text 和 Signature 需要 urlencode，其他参数保持原样）
    url_parts = []
    for k, v in sorted(params.items(), key=lambda x: x[0]):
        if k == "Text":
            url_parts.append(f"{k}={text_encoded}")
        elif k == "Signature":
            url_parts.append(f"{k}={signature_encoded}")
        else:
            url_parts.append(f"{k}={v}")

    url = f"wss://tts.cloud.tencent.com/stream_ws?{'&'.join(url_parts)}"
    return url


# ============================================
# 流式播放器
# ============================================

class StreamPlayer:
    """
    流式音频播放器，使用 PyAudio 实时播放 PCM 数据。
    支持边接收边播放，实现极低延迟。
    """

    def __init__(self, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width  # 16bit = 2 bytes
        self.pa = None
        self.stream = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._playing = False
        self._finished = False  # TTS 已完成标记
        self.last_error = None

    def open(self):
        """打开音频流"""
        if not HAS_PYAUDIO:
            raise RuntimeError("PyAudio 未安装，无法使用本地扬声器播放。"
                               "请安装 pyaudio 或使用摄像头喇叭模式 (camera_speaker.enabled: true)")
        self.last_error = None
        self.pa = pyaudio.PyAudio()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.channels,
            rate=self.sample_rate,
            output=True,
            frames_per_buffer=1024,
        )
        self._playing = True
        self._finished = False
        return self

    def feed(self, pcm_data: bytes):
        """往缓冲区喂入 PCM 数据"""
        if pcm_data and self._playing:
            with self._lock:
                self._buffer.extend(pcm_data)

    def play_buffered(self):
        """播放缓冲区中已有的数据（非阻塞调用一次）"""
        if not self._playing or not self.stream:
            return False

        data = None
        with self._lock:
            if len(self._buffer) >= 1024:
                # 每次取一块数据播放
                chunk_size = min(len(self._buffer), 4096)
                data = bytes(self._buffer[:chunk_size])
                del self._buffer[:chunk_size]

        if data:
            try:
                self.stream.write(data)
            except Exception as e:
                self.last_error = str(e)
                self._playing = False
                return False
            return True
        return False


    def drain(self):
        """播放完所有缓冲区中剩余的数据"""
        if not self.stream:
            return

        while True:
            with self._lock:
                if len(self._buffer) == 0:
                    break
                data = bytes(self._buffer)
                self._buffer.clear()

            if data:
                try:
                    self.stream.write(data)
                except Exception as e:
                    self.last_error = str(e)
                    self._playing = False
                    break

    def close(self):
        """关闭音频流并释放资源"""
        self._playing = False
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None



# ============================================
# 流式 TTS 引擎
# ============================================

class StreamTTS:
    """
    腾讯云 WebSocket 流式 TTS 引擎。
    连接 WebSocket → 接收 PCM 数据 → 实时通过 PyAudio 播放。
    """

    def __init__(self, tts_config: dict, logger: logging.Logger = None):
        self.tts_config = tts_config
        self.logger = logger or logging.getLogger("tts_stream")
        self.player = None
        self._error = None
        self._done_event = threading.Event()
        self._first_audio_time = None
        self._start_time = None
        self._synthesize_timeout = float(tts_config.get("synthesize_timeout_seconds", 60))
        self._speak_timeout = float(tts_config.get("speak_timeout_seconds", 90))


    def synthesize(self, text: str) -> bytes:
        """
        合成文本为 PCM 数据（不播放）。阻塞直到合成完成。
        返回 PCM bytes（16-bit LE, 单声道），失败返回空 bytes。
        """
        if not text or not text.strip():
            self.logger.warning("[TTS] 空文本，跳过")
            return b""

        self._error = None
        self._done_event.clear()
        self._start_time = time.time()

        try:
            ws_url = build_ws_url(text, self.tts_config)
        except Exception as e:
            self.logger.error(f"[TTS] 构建 WebSocket URL 失败: {e}")
            return b""

        self.logger.info(f"[TTS] 开始合成 ({len(text)} 字符)...")

        pcm_chunks = []
        received_audio = False

        def on_open(ws):
            self.logger.debug("[TTS] WebSocket 已连接")

        def on_message(ws, message):
            nonlocal received_audio
            if isinstance(message, bytes):
                received_audio = True
                pcm_chunks.append(message)
            else:
                try:
                    resp = json.loads(message)
                    code = resp.get("code", -1)
                    if code != 0:
                        self._error = resp.get("message", "未知错误")
                        self.logger.error(f"[TTS] 服务端错误 code={code}: {self._error}")
                        ws.close()
                        return
                    if resp.get("final") == 1:
                        self.logger.debug("[TTS] 合成完成，收到 final=1")
                        ws.close()
                except json.JSONDecodeError:
                    self.logger.warning(f"[TTS] 无法解析 JSON: {message[:100]}")

        def on_error(ws, error):
            self._error = str(error)
            self.logger.error(f"[TTS] WebSocket 错误: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.logger.debug(f"[TTS] WebSocket 已关闭 (code={close_status_code})")
            self._done_event.set()

        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()

        finished = self._done_event.wait(timeout=self._synthesize_timeout)
        if not finished:
            self._error = f"合成超时 (>{self._synthesize_timeout:.0f}s)"
            self.logger.error(f"[TTS] {self._error}，主动关闭连接")
            ws.close()
            self._done_event.set()

        ws_thread.join(timeout=5)
        if ws_thread.is_alive():
            self.logger.warning("[TTS] WebSocket 线程未在预期时间内退出")

        elapsed = time.time() - self._start_time
        if not self._error and not received_audio:
            self._error = "未收到任何音频数据"

        if self._error:
            self.logger.error(f"[TTS] 合成失败 (耗时 {elapsed:.1f}s): {self._error}")
            return b""

        pcm_data = b"".join(pcm_chunks)
        duration = len(pcm_data) / (int(self.tts_config.get("sample_rate", 16000)) * 2)
        self.logger.info(f"[TTS] 合成完成: {len(pcm_data)} bytes, {duration:.1f}s (耗时 {elapsed:.1f}s)")
        return pcm_data


    def speak(self, text: str) -> bool:
        """
        流式合成并实时播放文本。阻塞直到播放完成。
        返回 True 表示成功，False 表示失败。
        """
        if not text or not text.strip():
            self.logger.warning("[TTS] 空文本，跳过")
            return False

        self._error = None
        self._done_event.clear()
        self._first_audio_time = None
        self._start_time = time.time()

        sample_rate = int(self.tts_config.get("sample_rate", 16000))

        try:
            ws_url = build_ws_url(text, self.tts_config)
        except Exception as e:
            self.logger.error(f"[TTS] 构建 WebSocket URL 失败: {e}")
            return False

        self.logger.info(f"[TTS] 开始流式合成 ({len(text)} 字符)...")

        self.player = StreamPlayer(sample_rate=sample_rate)
        try:
            self.player.open()
        except Exception as e:
            self.logger.error(f"[TTS] 打开音频设备失败: {e}")
            self.logger.error("[TTS] 请确保已安装 PyAudio 及系统音频驱动")
            return False

        received_audio = False

        def on_open(ws):
            self.logger.debug("[TTS] WebSocket 已连接")

        def on_message(ws, message):
            nonlocal received_audio
            if isinstance(message, bytes):
                received_audio = True
                if self._first_audio_time is None:
                    self._first_audio_time = time.time()
                    latency = self._first_audio_time - self._start_time
                    self.logger.info(f"[TTS] 首包延迟: {latency:.2f}s")
                self.player.feed(message)
            else:
                try:
                    resp = json.loads(message)
                    code = resp.get("code", -1)
                    if code != 0:
                        self._error = resp.get("message", "未知错误")
                        self.logger.error(f"[TTS] 服务端错误 code={code}: {self._error}")
                        ws.close()
                        return

                    if resp.get("final") == 1:
                        self.logger.debug("[TTS] 合成完成，收到 final=1")
                        ws.close()
                except json.JSONDecodeError:
                    self.logger.warning(f"[TTS] 无法解析 JSON: {message[:100]}")

        def on_error(ws, error):
            self._error = str(error)
            self.logger.error(f"[TTS] WebSocket 错误: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.logger.debug(f"[TTS] WebSocket 已关闭 (code={close_status_code})")
            self._done_event.set()

        ws = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        ws_thread = threading.Thread(target=ws.run_forever, daemon=True)
        ws_thread.start()

        try:
            while not self._done_event.is_set():
                player_error = getattr(self.player, "last_error", None)
                if player_error:
                    self._error = f"音频播放失败: {player_error}"
                    self.logger.error(f"[TTS] {self._error}")
                    ws.close()
                    break

                if (time.time() - self._start_time) > self._speak_timeout:
                    self._error = f"播放超时 (>{self._speak_timeout:.0f}s)"
                    self.logger.error(f"[TTS] {self._error}，主动关闭连接")
                    ws.close()
                    break

                played = self.player.play_buffered()
                if not played:
                    time.sleep(0.01)

            self.player.drain()
            player_error = getattr(self.player, "last_error", None)
            if player_error and not self._error:
                self._error = f"音频播放失败: {player_error}"


        except KeyboardInterrupt:
            self.logger.info("[TTS] 用户中断播放")
            ws.close()
        finally:
            ws.close()
            self.player.close()
            ws_thread.join(timeout=5)
            if ws_thread.is_alive():
                self.logger.warning("[TTS] WebSocket 线程未在预期时间内退出")

        elapsed = time.time() - self._start_time
        if not self._error and not received_audio:
            self._error = "未收到任何音频数据"

        if self._error:
            self.logger.error(f"[TTS] 流式合成失败 (耗时 {elapsed:.1f}s): {self._error}")
            return False

        self.logger.info(f"[TTS] 流式合成+播放完成 (总耗时 {elapsed:.1f}s)")
        return True



# ============================================
# 独立测试入口
# ============================================

def main():
    """独立测试流式 TTS：python tts_stream.py "Hello, good morning!"  """
    from config_loader import ConfigError, load_and_validate_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("tts_stream")

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = "Hello! Good morning! I am Emily, your friendly English tutor. Let's learn some English today!"

    logger.info(f"[TEST] 测试文本: {text}")

    try:
        config = load_and_validate_config("test_tts")
    except ConfigError as e:
        logger.error(f"[ERROR] {e}")
        sys.exit(1)

    tts_config = config["tts"]

    engine = StreamTTS(tts_config, logger)
    success = engine.speak(text)


    if success:
        logger.info("[TEST] ✅ 流式 TTS 测试成功！")
    else:
        logger.error("[TEST] ❌ 流式 TTS 测试失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
