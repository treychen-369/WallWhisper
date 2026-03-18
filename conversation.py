#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emily 对话管理器 — 实现 Emily ↔ MuMu 多轮对话（支持摄像头麦克风）

对话流程:
  1. Emily 主动说一句（由 emily_v2 触发，只播一遍不重复）
  2. 开启聆听窗口 → 摄像头麦克风录音（通过 CameraMic RTSP）
  3. VAD 检测到语音 → ASR 识别
  4. 将 MuMu 的话发给 OpenClaw Emily → 获取回复
  5. TTS → 播放回复（只播一遍）
  6. 重复 2-5，最多 max_rounds 轮
  7. 第一轮就没有检测到语音 → 降级为单向重复播放 repeat_count 次

降级机制（pass_by / interact 模式）:
  - 对话模式下，Emily 说完第一句后等待 MuMu 回应
  - 如果超时没有回应，说明 MuMu 不在交互状态（可能只是路过或在忙别的）
  - 此时降级为传统的单向重复播放模式（英文+中文 × N遍），确保内容被听到

对话 API:
  OpenClaw Emily API 新增 conversation 模式，将 MuMu 的原话发给 Emily。
  如果 OpenClaw 不可用，fallback 到 Deepseek 直连（简单提示词）。
"""

import json
import logging
import re
import time
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from asr_engine import ASREngine
from camera_mic import CameraMic


class ConversationManager:
    """
    管理 Emily 和 MuMu 之间的多轮对话。

    支持两种音频输入:
      1. 摄像头麦克风（CameraMic, RTSP）— 生产模式，通过摄像头远场拾音
      2. 本地麦克风（PyAudio）— 开发测试

    使用方式 (集成到 emily_v2):
        mgr = ConversationManager(config, tts, logger, camera_speaker=speaker)
        result = mgr.start_conversation(initial_text=content, max_rounds=3)
        # result["mode"] == "conversation" → 完成了对话
        # result["mode"] == "fallback"     → 降级为单向播放
    """

    def __init__(
        self,
        config: dict,
        tts,
        logger: logging.Logger,
        camera_speaker=None,
    ):
        """
        Args:
            config: 完整配置 (config.yaml)
            tts: StreamTTS 实例
            logger: Logger
            camera_speaker: CameraSpeaker 实例 (None = 本地播放)
        """
        self.config = config
        self.tts = tts
        self.logger = logger
        self.camera_speaker = camera_speaker

        # 对话配置
        conv_cfg = config.get("conversation", {})
        self.enabled = conv_cfg.get("enabled", False)
        self.default_max_rounds = int(conv_cfg.get("max_rounds", 3))
        self.listen_timeout = float(conv_cfg.get("listen_timeout", 8))
        self.listen_max_duration = float(conv_cfg.get("listen_max_duration", 15))
        self.repeat_count = int(config.get("playback", {}).get("repeat_count", 3))

        # OpenClaw 配置
        openclaw_cfg = config.get("openclaw_emily", {})
        self.use_openclaw = openclaw_cfg.get("enabled", False)
        self.openclaw_api_url = openclaw_cfg.get("api_url", "")
        self.openclaw_api_token = openclaw_cfg.get("api_token", "")
        self.openclaw_timeout = int(openclaw_cfg.get("timeout", 30))

        # ASR 引擎（用于识别 PCM 数据，不负责录音）
        asr_cfg = config.get("asr", {})
        # 复用 TTS 的腾讯云密钥
        if not asr_cfg.get("secret_id"):
            asr_cfg["secret_id"] = config["tts"]["secret_id"]
        if not asr_cfg.get("secret_key"):
            asr_cfg["secret_key"] = config["tts"]["secret_key"]
        asr_cfg.setdefault("listen_timeout", self.listen_timeout)
        asr_cfg.setdefault("listen_max_duration", self.listen_max_duration)

        self.asr = ASREngine(asr_cfg, logger)

        # 摄像头麦克风（生产模式：通过 RTSP 从摄像头拉取音频）
        speaker_cfg = config.get("camera_speaker", {})
        self.use_camera_speaker = (
            speaker_cfg.get("enabled", False)
            and camera_speaker is not None
        )

        if self.use_camera_speaker:
            self.camera_mic = CameraMic(speaker_cfg, logger)
            logger.info("[对话] 音频输入: 摄像头麦克风 (RTSP)")
        else:
            self.camera_mic = None
            logger.info("[对话] 音频输入: 本地麦克风 (PyAudio)")

    def _listen(self, timeout: float = None, max_duration: float = None) -> Optional[str]:
        """
        聆听并识别 MuMu 说的话。

        优先使用摄像头麦克风（CameraMic RTSP），fallback 到本地麦克风。

        Returns:
            识别出的文本，超时/失败返回 None
        """
        timeout = timeout or self.listen_timeout
        max_duration = max_duration or self.listen_max_duration

        if self.camera_mic:
            # 摄像头麦克风模式：RTSP 拉取音频 → VAD → PCM → ASR
            self.logger.info(f"[对话] 通过摄像头麦克风聆听 (超时{timeout}s)...")
            pcm_data = self.camera_mic.listen(
                timeout=timeout,
                max_duration=max_duration,
            )
            if not pcm_data:
                return None
            return self.asr.recognize_pcm(pcm_data)
        else:
            # 本地麦克风 fallback
            return self.asr.listen_and_recognize(
                timeout=timeout,
                max_duration=max_duration,
            )

    def _speak(self, text: str, repeat: int = 1) -> bool:
        """
        播放文本。

        Args:
            text: 中英双语文本（用 --- 分隔）
            repeat: 重复播放次数（对话模式=1，降级模式=repeat_count）
        """
        # 解析中英双语
        parts = re.split(r'\n---\n', text, maxsplit=1)
        en_text = parts[0].strip()
        cn_text = parts[1].strip() if len(parts) > 1 else ""

        if self.use_camera_speaker and self.camera_speaker:
            # 摄像头喇叭模式：合成 PCM → RTSP 推送
            en_pcm = self.tts.synthesize(en_text) if en_text else b""
            if not en_pcm:
                self.logger.error("[对话] TTS 英文合成失败")
                return False

            cn_pcm = b""
            if cn_text:
                cn_pcm = self.tts.synthesize(cn_text)

            sample_rate = self.tts.tts_config.get("sample_rate", 16000)
            short_pause = b'\x00' * (sample_rate * 2 * 2)   # 2 秒停顿（英→中）
            long_pause = b'\x00' * (sample_rate * 2 * 4)    # 4 秒停顿（轮次之间）

            # 构建单轮 PCM：英文 + 短停顿 + 中文
            single_round = en_pcm
            if cn_pcm:
                single_round += short_pause + cn_pcm

            # 组装完整 PCM：重复 repeat 次
            full_pcm = single_round
            for i in range(1, repeat):
                full_pcm += long_pause + single_round

            duration = len(full_pcm) / (sample_rate * 2)
            mode_label = f"x{repeat}" if repeat > 1 else "对话"
            self.logger.info(f"[对话] 播放 ({mode_label}, {duration:.1f}s)")
            return self.camera_speaker.speak_pcm(full_pcm)
        else:
            # 本地播放模式
            for i in range(repeat):
                if i > 0:
                    time.sleep(4)  # 轮次间隔
                if not self.tts.speak(en_text):
                    return False
                if cn_text:
                    time.sleep(1 if repeat == 1 else 2)
                    self.tts.speak(cn_text)
            return True

    def _generate_reply_via_openclaw(self, user_text: str, round_num: int) -> Optional[str]:
        """
        通过 OpenClaw Emily API 生成对话回复。
        发送 MuMu 说的话给 Emily，让 Emily 用人设回应。
        """
        self.logger.info(f"[对话] OpenClaw 生成回复 (第{round_num}轮)...")
        start = time.time()

        payload = {
            "mode": "conversation",
            "scene": "interactive_dialogue",
            "target": "kid",
            "time": time.strftime("%H:%M"),
            "description": f"MuMu 在和 Emily 对话，这是第 {round_num} 轮。MuMu 刚刚说了：{user_text}",
            "bilingual": True,
            "content_hint": (
                f"MuMu (3岁) just said: \"{user_text}\". "
                f"This is conversation round {round_num}. "
                "Respond naturally and encouragingly. "
                "Keep it very short (1-2 sentences). "
                "If MuMu tried to say an English word, praise her and gently correct if needed. "
                "Ask a simple follow-up question to continue the conversation."
            ),
        }

        try:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                self.openclaw_api_url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.openclaw_api_token}",
                },
                method="POST",
            )
            with urlopen(req, timeout=self.openclaw_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            text = data.get("text", "").strip()
            elapsed = time.time() - start
            self.logger.info(f"[对话] OpenClaw 回复 ({elapsed:.1f}s, {len(text)}字符): {text[:80]}...")
            return text if text else None

        except Exception as e:
            self.logger.error(f"[对话] OpenClaw 回复失败: {e}")
            return None

    def _generate_reply_fallback(self, user_text: str, round_num: int) -> Optional[str]:
        """
        Fallback: 通过 Deepseek 直连生成对话回复。
        """
        try:
            from openai import OpenAI

            ai_cfg = self.config["ai"]
            client = OpenAI(
                api_key=ai_cfg["api_key"],
                base_url=ai_cfg.get("base_url"),
                timeout=float(ai_cfg.get("timeout_seconds", 30)),
            )

            system_prompt = (
                "You are Emily, a warm and patient English tutor for a 3-year-old Chinese girl named MuMu (木木). "
                "MuMu loves Frozen, Elsa, singing and dancing. "
                "You are having a live voice conversation with her. "
                "Rules: "
                "1. Keep responses VERY short (1-2 sentences max). "
                "2. Use only simple words a 3-year-old can understand. "
                "3. If she tries English words, praise her enthusiastically. "
                "4. Ask simple yes/no or choice questions to keep the conversation going. "
                "5. Format: English first, then --- on a new line, then Chinese translation."
            )

            user_prompt = (
                f"Round {round_num}. MuMu just said: \"{user_text}\". "
                "Respond naturally in 1-2 short sentences. End with a simple question."
            )

            self.logger.info(f"[对话] Deepseek 生成回复 (第{round_num}轮)...")
            start = time.time()

            response = client.chat.completions.create(
                model=ai_cfg.get("model", "deepseek-chat"),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.9,
                max_tokens=200,
            )

            text = response.choices[0].message.content.strip()
            elapsed = time.time() - start
            self.logger.info(f"[对话] Deepseek 回复 ({elapsed:.1f}s): {text[:80]}...")
            return text

        except Exception as e:
            self.logger.error(f"[对话] Deepseek 回复失败: {e}")
            return None

    def generate_reply(self, user_text: str, round_num: int) -> Optional[str]:
        """生成 Emily 的回复（OpenClaw 优先 + Deepseek fallback）"""
        reply = None

        if self.use_openclaw:
            reply = self._generate_reply_via_openclaw(user_text, round_num)

        if not reply:
            if self.use_openclaw:
                self.logger.warning("[对话] OpenClaw 失败，fallback 到 Deepseek")
            reply = self._generate_reply_fallback(user_text, round_num)

        return reply

    def start_conversation(
        self,
        initial_text: str = None,
        skip_initial_speak: bool = False,
        max_rounds: int = None,
    ) -> dict:
        """
        启动多轮对话，支持降级为单向重复播放。

        流程:
          1. 播放 Emily 开场白（只播一遍）
          2. 聆听 MuMu 回应
          3a. 如果第一轮就没有回应 → 降级：把开场白再重复播 repeat_count-1 遍
                                    （第一遍已经播过了，补剩余次数）
          3b. 如果有回应 → 进入对话循环，最多 max_rounds 轮
          4. 对话中途任何一轮没回应 → 自然结束对话

        Args:
            initial_text: Emily 的开场白（已由 emily_v2 生成）
            skip_initial_speak: 是否跳过播放开场白
            max_rounds: 最大对话轮数（None 则用配置默认值）

        Returns:
            {
                "mode": "conversation" | "fallback",
                "rounds": int,          # 实际完成的对话轮数
                "texts": list,          # 对话记录
                "total_time": float,    # 总耗时
            }
        """
        max_rounds = max_rounds or self.default_max_rounds

        self.logger.info("=" * 50)
        self.logger.info(f"[对话] >>> 开始对话模式 (最多{max_rounds}轮)")
        self.logger.info("=" * 50)

        start_time = time.time()
        round_texts = []
        rounds_completed = 0

        # Step 1: 播放开场白（只播一遍，为后续对话留时间）
        if initial_text and not skip_initial_speak:
            self.logger.info(f"[对话] Emily 开场: {initial_text[:60]}...")
            ok = self._speak(initial_text, repeat=1)
            if not ok:
                self.logger.error("[对话] 开场白播放失败")
                return {"mode": "fallback", "rounds": 0, "texts": [], "total_time": 0}
            round_texts.append({"role": "emily", "text": initial_text})

        # Step 2: 第一轮聆听 — 关键的分水岭
        self.logger.info(f"\n[对话] --- 第 1/{max_rounds} 轮聆听（决定对话/降级）---")
        user_text = self._listen()

        if not user_text:
            # ========== 降级：单向重复播放 ==========
            #
            # MuMu 没有回应（可能只是路过、或在玩别的），
            # 降级为传统的单向重复播放，确保内容被听到。
            # 开场白已播放 1 遍，再补播 repeat_count - 1 遍。
            #
            remaining = self.repeat_count - 1
            self.logger.info(
                f"[对话] 未检测到回应 → 降级为单向播放 "
                f"(已播1遍, 补播{remaining}遍, 共{self.repeat_count}遍)"
            )

            if remaining > 0 and initial_text:
                ok = self._speak(initial_text, repeat=remaining)
                if not ok:
                    self.logger.warning("[对话] 降级播放失败")

            total_time = time.time() - start_time
            self.logger.info(f"[对话] === 降级播放完成 (耗时{total_time:.1f}s) ===")
            return {
                "mode": "fallback",
                "rounds": 0,
                "texts": round_texts,
                "total_time": total_time,
            }

        # ========== 对话模式：MuMu 有回应 ==========
        self.logger.info(f"[对话] MuMu 说: \"{user_text}\"")
        round_texts.append({"role": "mumu", "text": user_text})

        # Emily 回复第一轮
        reply = self.generate_reply(user_text, 1)
        if reply:
            self.logger.info(f"[对话] Emily 回复: {reply[:60]}...")
            round_texts.append({"role": "emily", "text": reply})
            self._speak(reply, repeat=1)
        rounds_completed = 1

        # 后续对话轮次（第 2 轮到第 max_rounds 轮）
        for round_num in range(2, max_rounds + 1):
            self.logger.info(f"\n[对话] --- 第 {round_num}/{max_rounds} 轮聆听 ---")

            user_text = self._listen()

            if not user_text:
                self.logger.info(
                    f"[对话] 未检测到语音，对话自然结束 (完成{rounds_completed}轮)"
                )
                break

            self.logger.info(f"[对话] MuMu 说: \"{user_text}\"")
            round_texts.append({"role": "mumu", "text": user_text})

            # Emily 回复
            reply = self.generate_reply(user_text, round_num)
            if not reply:
                self.logger.error("[对话] Emily 回复生成失败，对话结束")
                break

            self.logger.info(f"[对话] Emily 回复: {reply[:60]}...")
            round_texts.append({"role": "emily", "text": reply})

            ok = self._speak(reply, repeat=1)
            if not ok:
                self.logger.warning("[对话] 播放回复失败")

            rounds_completed = round_num

        total_time = time.time() - start_time
        self.logger.info(f"\n[对话] === 对话结束: {rounds_completed}轮, 耗时{total_time:.1f}s ===")

        return {
            "mode": "conversation",
            "rounds": rounds_completed,
            "texts": round_texts,
            "total_time": total_time,
        }
