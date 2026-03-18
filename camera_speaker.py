#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头喇叭推送模块 - 通过 RTSP Backchannel 将音频推送到萤石 C7 摄像头喇叭

核心发现（经测试验证）:
- 必须在 DESCRIBE 时加 Require: www.onvif.org/ver20/backchannel 头
- SDP 会返回额外的 sendonly audio track (trackID=4) 作为 backchannel
- 只支持 AAC 编码 (PT=104, 16kHz)，PCMU 不出声
- 使用 TCP interleaved 方式发送 RTP 数据

流程: DESCRIBE(+backchannel) → SETUP(sendonly track, TCP) → PLAY → 发送 RTP AAC

独立测试: python camera_speaker.py
"""

import os
import re
import socket
import struct
import time
import hashlib
import subprocess
import logging
import tempfile


# ============================================
# RTSP 客户端（精简版，专用于 backchannel）
# ============================================

class RTSPClient:
    """轻量级 RTSP 客户端，专注 backchannel 对讲"""

    def __init__(self, host: str, port: int, user: str, password: str,
                 timeout: int = 10, logger: logging.Logger = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.timeout = timeout
        self.logger = logger or logging.getLogger("rtsp")
        self.sock = None
        self.cseq = 0
        self.session_id = None
        self.realm = None
        self.nonce = None

    def connect(self):
        """建立 TCP 连接"""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.host, self.port))
        self.logger.debug(f"[RTSP] 已连接 {self.host}:{self.port}")

    def close(self):
        """关闭连接"""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _make_auth_header(self, method: str, uri: str) -> str | None:
        """生成 Digest 认证头"""
        if not self.realm:
            return None
        h1 = hashlib.md5(f"{self.user}:{self.realm}:{self.password}".encode()).hexdigest()
        h2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{h1}:{self.nonce}:{h2}".encode()).hexdigest()
        return (f'Digest username="{self.user}", realm="{self.realm}", '
                f'nonce="{self.nonce}", uri="{uri}", response="{response}"')

    def request(self, method: str, uri: str, headers: dict = None,
                retry_auth: bool = True) -> tuple:
        """
        发送 RTSP 请求，返回 (status_code, response_text)
        自动处理 401 认证挑战
        """
        self.cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}"]

        auth = self._make_auth_header(method, uri)
        if auth:
            lines.append(f"Authorization: {auth}")
        if self.session_id:
            lines.append(f"Session: {self.session_id}")
        if headers:
            for k, v in headers.items():
                lines.append(f"{k}: {v}")

        lines += ["", ""]
        msg = "\r\n".join(lines)

        self.sock.sendall(msg.encode())

        # 接收响应
        data = b""
        while True:
            try:
                chunk = self.sock.recv(8192)
                if not chunk:
                    break
                data += chunk
                # 跳过 interleaved 数据帧 ($)
                while data and data[0:1] == b'$' and len(data) >= 4:
                    frame_len = struct.unpack('!H', data[2:4])[0]
                    if len(data) >= 4 + frame_len:
                        data = data[4 + frame_len:]
                    else:
                        break
                # 检查是否收到完整的 RTSP 响应
                if b"RTSP/1." in data and b"\r\n\r\n" in data:
                    header_end = data.index(b"\r\n\r\n")
                    header_text = data[:header_end].decode(errors='replace')
                    cl_match = re.search(r'Content-Length:\s*(\d+)', header_text, re.I)
                    if cl_match:
                        body_needed = int(cl_match.group(1))
                        if len(data) - header_end - 4 >= body_needed:
                            break
                    else:
                        break
            except socket.timeout:
                break

        text = data.decode(errors='replace')
        status_match = re.search(r'RTSP/1\.\d\s+(\d+)', text)
        status = int(status_match.group(1)) if status_match else 0

        # 处理 401 认证
        if status == 401 and retry_auth:
            for line in text.split('\r\n'):
                if 'www-authenticate' in line.lower():
                    rm = re.search(r'realm="([^"]*)"', line)
                    nm = re.search(r'nonce="([^"]*)"', line)
                    if rm:
                        self.realm = rm.group(1)
                    if nm:
                        self.nonce = nm.group(1)
            return self.request(method, uri, headers, retry_auth=False)

        # 提取 Session ID
        sess_match = re.search(r'Session:\s*([^;\r\n]+)', text)
        if sess_match:
            self.session_id = sess_match.group(1).strip()

        return status, text

    def send_interleaved(self, channel: int, data: bytes):
        """发送 TCP interleaved RTP 数据"""
        self.sock.sendall(struct.pack('!BBH', 0x24, channel, len(data)) + data)


# ============================================
# 摄像头喇叭推送器
# ============================================

class CameraSpeaker:
    """
    通过 RTSP Backchannel 将音频推送到摄像头喇叭。

    用法:
        speaker = CameraSpeaker(config, logger)
        speaker.speak_pcm(pcm_data)   # 直接推送 PCM 数据（内部转 AAC）
        speaker.speak_aac(aac_data)   # 推送 AAC ADTS 数据
    """

    def __init__(self, config: dict, logger: logging.Logger = None):
        """
        config 需包含:
            cam_ip, cam_rtsp_port, cam_user, cam_password
            ffmpeg_path (可选, 默认系统 PATH 中的 ffmpeg)
            sample_rate (可选, 默认 16000)
        """
        self.cam_ip = config["cam_ip"]
        self.cam_port = config.get("cam_rtsp_port", 554)
        self.cam_user = config.get("cam_user", "admin")
        self.cam_password = config["cam_password"]
        self.ffmpeg_path = config.get("ffmpeg_path", "ffmpeg")
        self.sample_rate = config.get("sample_rate", 16000)
        self.logger = logger or logging.getLogger("camera_speaker")

    def speak_pcm(self, pcm_data: bytes, sample_rate: int = None) -> bool:
        """
        将 PCM 音频数据转为 AAC 后推送到摄像头喇叭。

        Args:
            pcm_data: PCM 16-bit little-endian 单声道音频数据
            sample_rate: 采样率，默认使用配置中的值

        Returns:
            True 成功, False 失败
        """
        if not pcm_data:
            self.logger.warning("[Speaker] 空 PCM 数据，跳过")
            return False

        sr = sample_rate or self.sample_rate
        duration = len(pcm_data) / (sr * 2)
        self.logger.info(f"[Speaker] PCM → AAC: {len(pcm_data)} bytes, {duration:.1f}s")

        # PCM → AAC 转码
        aac_data = self._pcm_to_aac(pcm_data, sr)
        if not aac_data:
            return False

        return self.speak_aac(aac_data)

    def speak_aac(self, aac_data: bytes) -> bool:
        """
        将 AAC ADTS 数据通过 RTSP backchannel 推送到摄像头喇叭。

        Args:
            aac_data: AAC ADTS 格式音频数据

        Returns:
            True 成功, False 失败
        """
        if not aac_data:
            self.logger.warning("[Speaker] 空 AAC 数据，跳过")
            return False

        # 解析 ADTS 帧
        frames = self._parse_adts_frames(aac_data)
        if not frames:
            self.logger.error("[Speaker] 无法解析 AAC ADTS 帧")
            return False

        self.logger.info(f"[Speaker] AAC: {len(aac_data)} bytes, {len(frames)} 帧")

        # 建立 RTSP backchannel 并发送
        return self._rtsp_push(frames)

    def _pcm_to_aac(self, pcm_data: bytes, sample_rate: int) -> bytes:
        """用 FFmpeg 将 PCM 转为 AAC ADTS"""
        pcm_file = None
        aac_file = None
        try:
            # 使用临时文件
            fd_pcm, pcm_file = tempfile.mkstemp(suffix=".pcm")
            fd_aac, aac_file = tempfile.mkstemp(suffix=".aac")
            os.close(fd_pcm)
            os.close(fd_aac)

            with open(pcm_file, "wb") as f:
                f.write(pcm_data)

            cmd = [
                self.ffmpeg_path, "-y",
                "-f", "s16le",
                "-ar", str(sample_rate),
                "-ac", "1",
                "-i", pcm_file,
                "-c:a", "aac",
                "-b:a", "32k",
                "-ar", "16000",  # backchannel 需要 16kHz
                "-f", "adts",
                aac_file
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                self.logger.error(f"[Speaker] FFmpeg 转码失败: {result.stderr[:200]}")
                return b""

            with open(aac_file, "rb") as f:
                aac_data = f.read()

            self.logger.debug(f"[Speaker] PCM→AAC: {len(pcm_data)} → {len(aac_data)} bytes")
            return aac_data

        except Exception as e:
            self.logger.error(f"[Speaker] PCM→AAC 异常: {e}")
            return b""
        finally:
            for f in (pcm_file, aac_file):
                if f:
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    def _parse_adts_frames(self, aac_data: bytes) -> list:
        """解析 AAC ADTS 帧，返回裸帧数据列表"""
        frames = []
        pos = 0
        while pos < len(aac_data) - 7:
            # ADTS sync word: 0xFFF
            if aac_data[pos] != 0xFF or (aac_data[pos + 1] & 0xF0) != 0xF0:
                pos += 1
                continue
            # 帧长度 (13 bits)
            frame_len = (
                ((aac_data[pos + 3] & 0x03) << 11) |
                (aac_data[pos + 4] << 3) |
                ((aac_data[pos + 5] >> 5) & 0x07)
            )
            if frame_len < 7 or pos + frame_len > len(aac_data):
                break
            # Header size: 7 (no CRC) or 9 (with CRC)
            header_size = 7 if (aac_data[pos + 1] & 0x01) else 9
            frames.append(aac_data[pos + header_size:pos + frame_len])
            pos += frame_len

        return frames

    def _rtsp_push(self, frames: list) -> bool:
        """通过 RTSP backchannel 推送 AAC 帧"""
        base_uri = f"rtsp://{self.cam_ip}:{self.cam_port}"
        client = RTSPClient(
            self.cam_ip, self.cam_port, self.cam_user, self.cam_password,
            logger=self.logger
        )

        try:
            client.connect()

            # 1. DESCRIBE (with backchannel header)
            status, resp = client.request("DESCRIBE", base_uri, {
                "Accept": "application/sdp",
                "Require": "www.onvif.org/ver20/backchannel"
            })
            if status != 200:
                self.logger.error(f"[Speaker] DESCRIBE 失败: {status}")
                return False

            # 解析 backchannel track (sendonly audio)
            bc_uri = self._find_backchannel_uri(resp, base_uri)
            if not bc_uri:
                self.logger.error("[Speaker] 未找到 backchannel track (sendonly audio)")
                return False

            self.logger.debug(f"[Speaker] Backchannel URI: {bc_uri}")

            # 2. SETUP (TCP interleaved)
            status, resp = client.request("SETUP", bc_uri, {
                "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1;mode=record"
            })
            if status != 200:
                # 退而求其次，不带 mode=record
                status, resp = client.request("SETUP", bc_uri, {
                    "Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"
                })
                if status != 200:
                    self.logger.error(f"[Speaker] SETUP 失败: {status}")
                    return False

            # 提取 interleaved channel 和 SSRC
            il_match = re.search(r'interleaved=(\d+)-(\d+)', resp)
            rtp_channel = int(il_match.group(1)) if il_match else 0

            ssrc_match = re.search(r'ssrc=([0-9a-fA-F]+)', resp)
            ssrc = int(ssrc_match.group(1), 16) if ssrc_match else 0x420DF1BA

            # 3. PLAY
            status, resp = client.request("PLAY", base_uri, {
                "Range": "npt=0.000-"
            })
            if status != 200:
                self.logger.error(f"[Speaker] PLAY 失败: {status}")
                return False

            # 4. 发送 RTP AAC 帧
            pt = 104  # AAC payload type
            sample_rate = 16000
            samples_per_frame = 1024  # AAC-LC
            frame_duration = samples_per_frame / sample_rate  # ~64ms

            seq = 0
            timestamp = 0
            start_time = time.time()
            errors = 0

            for i, frame in enumerate(frames):
                # RTP Header (12 bytes)
                marker = 0x80 if i == 0 else 0x00
                rtp_header = struct.pack('!BBHII',
                    0x80,                      # V=2, P=0, X=0, CC=0
                    marker | pt,               # M + PT
                    seq & 0xFFFF,
                    timestamp & 0xFFFFFFFF,
                    ssrc
                )

                # AU-Header (AAC-hbr mode)
                au_size = len(frame)
                au_header = struct.pack('!HH', 16, (au_size << 3) & 0xFFF8)

                packet = rtp_header + au_header + frame

                try:
                    client.send_interleaved(rtp_channel, packet)
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        self.logger.warning(f"[Speaker] 发送错误 frame {i}: {e}")
                    if errors > 5:
                        self.logger.error("[Speaker] 错误过多，停止发送")
                        break

                seq += 1
                timestamp += samples_per_frame

                # 实时节奏控制
                expected_time = (i + 1) * frame_duration
                actual_time = time.time() - start_time
                if expected_time > actual_time:
                    time.sleep(expected_time - actual_time)

            elapsed = time.time() - start_time
            self.logger.info(
                f"[Speaker] 推送完成: {seq} 帧, {elapsed:.1f}s, 错误: {errors}"
            )

            # 5. TEARDOWN
            try:
                client.request("TEARDOWN", base_uri)
            except Exception:
                pass

            return errors == 0

        except Exception as e:
            self.logger.error(f"[Speaker] RTSP 推送异常: {e}")
            return False
        finally:
            client.close()

    def _find_backchannel_uri(self, describe_resp: str, base_uri: str) -> str | None:
        """从 DESCRIBE 响应的 SDP 中找到 backchannel (sendonly audio) track 的 URI"""
        sdp_start = describe_resp.find("v=0")
        if sdp_start < 0:
            return None

        sdp = describe_resp[sdp_start:]
        sections = re.split(r'(?=m=)', sdp)

        for section in sections:
            if 'sendonly' in section and 'audio' in section:
                ctrl_match = re.search(r'a=control:(\S+)', section)
                if ctrl_match:
                    return ctrl_match.group(1)
                return f"{base_uri}/trackID=4"  # fallback

        return None


# ============================================
# 独立测试
# ============================================

def main():
    """独立测试: 用 TTS 合成一句话并推送到摄像头"""
    import yaml
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = logging.getLogger("camera_speaker_test")

    # 加载配置
    script_dir = Path(__file__).parent
    config_path = script_dir / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    speaker_cfg = config.get("camera_speaker", {})
    if not speaker_cfg.get("cam_ip"):
        logger.error("请在 config.yaml 中配置 camera_speaker 部分")
        return

    speaker = CameraSpeaker(speaker_cfg, logger)

    # 生成测试音频 (用 TTS)
    text = "Hello! I am Emily. Can you hear me through the camera speaker? This is amazing!"
    logger.info(f"[TEST] 测试文本: {text}")

    # 尝试用 TTS 生成 PCM
    try:
        from tts_stream import build_ws_url
        import websocket
        import json
        import threading

        tts_cfg = config["tts"]
        ws_url = build_ws_url(text, tts_cfg)

        pcm_chunks = []
        done = threading.Event()

        def on_message(ws, message):
            if isinstance(message, bytes):
                pcm_chunks.append(message)
            else:
                try:
                    resp = json.loads(message)
                    if resp.get("code", -1) != 0:
                        logger.error(f"TTS error: {resp.get('message')}")
                        ws.close()
                        return
                    if resp.get("final") == 1:
                        ws.close()
                except:
                    pass

        def on_close(ws, *args):
            done.set()

        ws = websocket.WebSocketApp(ws_url,
            on_message=on_message, on_close=on_close,
            on_error=lambda ws, e: logger.error(f"TTS ws error: {e}"))

        t = threading.Thread(target=ws.run_forever, daemon=True)
        t.start()
        done.wait(timeout=30)

        pcm_data = b"".join(pcm_chunks)
        duration = len(pcm_data) / (16000 * 2)
        logger.info(f"[TEST] TTS 合成完成: {len(pcm_data)} bytes, {duration:.1f}s")

        # 推送到摄像头
        logger.info("[TEST] >>> 推送到摄像头喇叭，请去客厅听！<<<")
        ok = speaker.speak_pcm(pcm_data)

        if ok:
            logger.info("[TEST] ✅ 摄像头喇叭推送成功！")
        else:
            logger.error("[TEST] ❌ 推送失败")

    except ImportError as e:
        logger.error(f"[TEST] 依赖缺失: {e}")
        logger.info("[TEST] 使用 FFmpeg 生成测试音频...")

        # 备选: 用 FFmpeg 生成正弦波测试
        import tempfile
        fd, aac_file = tempfile.mkstemp(suffix=".aac")
        os.close(fd)

        cmd = [
            speaker.ffmpeg_path, "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=5:sample_rate=16000",
            "-ar", "16000", "-ac", "1",
            "-c:a", "aac", "-b:a", "32k",
            "-f", "adts", aac_file
        ]
        subprocess.run(cmd, capture_output=True)

        with open(aac_file, "rb") as f:
            aac_data = f.read()
        os.remove(aac_file)

        ok = speaker.speak_aac(aac_data)
        if ok:
            logger.info("[TEST] ✅ 测试音频推送成功！")
        else:
            logger.error("[TEST] ❌ 推送失败")


if __name__ == "__main__":
    main()
