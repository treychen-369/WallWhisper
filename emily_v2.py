#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emily V2.1 - 家庭隐形英语外教（多模式交互升级版）

三种触发模式：
1. pass_by    — 有人路过摄像头，简短打招呼 + 自我介绍
2. interact   — 双镜头联合检测(广角+云台同时触发)或当天偶数次触发，深入英语教学
3. scheduled  — 定时播报预制任务（每日英语晨播、词汇复习、睡前故事等）

通过 run.py emily 启动
"""

import os
import re
import sys
import time
import json
import random
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import yaml
from openai import OpenAI

from tts_stream import StreamTTS
from ezviz_monitor import EzvizTokenManager, EzvizMonitor, load_ezviz_cached_token
from camera_speaker import CameraSpeaker
from conversation import ConversationManager



# ============================================
# 时间段场景匹配（V2.1：支持 pass_by / interact 子场景）
# ============================================

def parse_hhmm_to_minutes(value: str) -> int:
    """将 HH:MM 字符串转为当天分钟数。"""
    dt = datetime.strptime(value, "%H:%M")
    return dt.hour * 60 + dt.minute


def is_time_in_range(current_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    """判断当前时间是否落在指定区间内，支持跨午夜。"""
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def match_time_scene(time_scenes: list, now: datetime = None) -> dict | None:
    """
    根据当前时间匹配 config.yaml 中的 time_scenes 区间。
    返回匹配的场景 dict 或 None。
    """
    if now is None:
        now = datetime.now()

    current_minutes = now.hour * 60 + now.minute

    for scene in time_scenes:
        try:
            start_minutes = parse_hhmm_to_minutes(scene.get("start", "00:00"))
            end_minutes = parse_hhmm_to_minutes(scene.get("end", "23:59"))
        except (TypeError, ValueError):
            continue

        if is_time_in_range(current_minutes, start_minutes, end_minutes):
            return scene

    return None



# ============================================
# Prompt 构建（V2.1：根据交互模式构建不同 Prompt）
# ============================================

# ---- 路过模式 System Prompt ----
SYSTEM_PROMPT_PASS_BY = """You are Emily, a gentle and playful English friend for MuMu, a 3-year-old toddler.
MuMu just walked past the camera. Give a quick, cheerful hello to MuMu!

Current scene: {scene_type} ({scene_desc})
Target audience: {target_desc}
Current time: {current_time}

Family members:
{family_info}

CRITICAL RULES:
1. Keep it SUPER SHORT — the English part should be under 60 characters. Just a quick hello!
2. ALWAYS say "MuMu" — address her by name. "Hello MuMu!" or "Hi MuMu!"
3. Use ONE simple word (cat, dog, sun, red, ball, etc.) — single syllable words only!
4. Speak slowly and clearly — this is for a 3-year-old's ears
5. DO NOT use any formatting, markdown, or labels
6. Use BILINGUAL format: English first, then "---" on a new line, then simple Chinese
7. The Chinese part should be toddler-friendly — like a mom talking to her baby

Example outputs:
Hello MuMu! Cat! Meow! Can you say cat?
---
木木你好！Cat是小猫咪！喵！你能说cat吗？

Hi MuMu! Red! Look, red apple!
---
木木！Red是红色！看，红苹果！
"""

# ---- 互动模式 System Prompt ----
SYSTEM_PROMPT_INTERACT = """You are Emily, a gentle, playful English friend for MuMu, a 3-year-old toddler.
MuMu is right here in front of the camera! Time for a fun mini English play session!

Current scene: {scene_type} ({scene_desc})
Target audience: {target_desc}
Current time: {current_time}

Family members:
{family_info}

CRITICAL RULES:
1. Keep the English part UNDER 150 characters (will be spoken aloud by TTS very slowly)
2. ALWAYS address MuMu by name — "MuMu look!", "Good job MuMu!"
3. Teach ONE simple word — use only single-syllable or very simple two-syllable words (cat, dog, sun, apple, Elsa)
4. Make it playful — use animal sounds, singing, or Frozen references
5. ALWAYS end with "Can you say ___?" to encourage MuMu to repeat
6. Speak slowly and clearly — short sentences, simple words
7. DO NOT use any formatting, markdown, or labels
8. Use BILINGUAL format: English first, then "---" on a new line, then simple Chinese
9. The Chinese part should be toddler-friendly and warm

Example outputs:
Hi MuMu! Look, a dog! Dog! Woof woof! Dogs are so cute! Can you say dog? Good job MuMu!
---
木木你好！看，小狗！Dog！汪汪汪！小狗好可爱！你能说dog吗？木木真棒！

Hello MuMu! Do you like Elsa? Elsa wears a blue dress! Blue! Can you say blue?
---
木木！你喜欢艾莎吗？艾莎穿蓝色裙子！Blue是蓝色！你能说blue吗？
"""

# ---- 定时播报 System Prompt ----
SYSTEM_PROMPT_SCHEDULED = """You are Emily, a gentle and playful English friend for MuMu, a 3-year-old toddler.
This is a scheduled session just for MuMu — make it fun like a little show!

Task type: {scene_type} ({scene_desc})
Content hint: {content_hint}
Target audience: {target_desc}
Current time: {current_time}

Family members:
{family_info}

CRITICAL RULES:
1. Keep the English part UNDER 200 characters (will be spoken aloud very slowly)
2. ALWAYS address MuMu by name — this is HER special time
3. Follow the content hint but keep vocabulary at toddler level (cat, dog, sun, red, ball, Elsa, etc.)
4. Use simple one-syllable words. NO complex vocabulary.
5. Make it playful — songs, animal sounds, Frozen references, clapping games
6. Speak slowly and clearly — short sentences for little ears
7. DO NOT use any formatting, markdown, or labels
8. Use BILINGUAL format: English first, then "---" on a new line, then simple Chinese
9. The Chinese part should be toddler-friendly and warm

Example (morning hello):
Good morning MuMu! Let's learn a word! Sun! The sun is big and bright! Like Elsa's magic! Can you say sun?
---
木木早上好！来学一个词！Sun是太阳！太阳又大又亮！像艾莎的魔法一样！你能说sun吗？
"""

TARGET_DESCRIPTIONS = {
    "family": "MuMu, a 3-year-old toddler (English beginner, loves Frozen, Elsa, animals, singing)",
    "adult": "MuMu, a 3-year-old toddler (English beginner, loves Frozen, Elsa, animals, singing)",
    "kid": "MuMu, a 3-year-old toddler (English beginner, loves Frozen, Elsa, animals, singing)",
}

# ---- 路过模式的 User Prompt 映射 ----
PASS_BY_PROMPTS = {
    "morning_greeting": "Say 'Good morning MuMu!' and teach one simple word like sun, bird, or milk.",
    "pass_by_hello": "Say 'Hi MuMu!' and share one fun simple word with an animal sound or Frozen reference.",
    "lunch_greeting": "Say 'Hi MuMu!' with a yummy food word like rice, egg, or juice.",
    "welcome_home": "Say 'Hi MuMu! Welcome home!' with one fun word.",
    "dinner_greeting": "Say 'Hi MuMu!' with a simple word about food or evening.",
    "goodnight_greeting": "Say 'Good night MuMu!' with a gentle word like moon or star.",
}

# ---- 互动模式的 User Prompt 映射 ----
INTERACT_PROMPTS = {
    "morning_chat": "Greet MuMu warmly and teach one simple morning word (sun, bird, milk, egg) with fun sounds.",
    "daily_lesson": "Teach MuMu ONE simple word (animal, color, or object) with a playful game — use animal sounds, Frozen, or 'Can you say ___?'",
    "lunch_chat": "Teach MuMu a fun food word (rice, egg, juice, apple, cake) in a playful way.",
    "homework_help": "Play a fun English word game with MuMu — teach a simple word (animal, color, body part) using sounds, songs, or Frozen characters. Always say 'Can you say ___?'",
    "dinner_funfact": "Teach MuMu a simple word about dinner time or evening with fun sounds and encouragement.",
    "bedtime_story": "Tell MuMu a VERY short (2-3 sentences) gentle bedtime mini-story using only simple words. Maybe about Elsa, a bunny, or the moon.",
}

# ---- 定时任务的 User Prompt 映射 ----
SCHEDULED_PROMPTS = {
    "daily_briefing": "Say 'Good morning MuMu!' and teach one simple word for today — a color, animal, or object. Make it fun with sounds!",
    "vocab_review": "Review a simple word MuMu learned recently. Say it slowly, repeat it, use it in a short fun sentence. 'Can you say ___?'",
    "kid_challenge": "Give MuMu a fun, super simple English game — like 'Point to something red!' or 'Can you clap and say cat?' Use Frozen characters if you can!",
    "bedtime_story": "Tell MuMu a very short, gentle bedtime story (2-3 sentences) using only simple words. Maybe about Elsa going to sleep, or a bunny looking at the moon. End with 'Good night MuMu!'",
}


def _build_family_info(family: dict) -> str:
    """构建家庭成员信息字符串"""
    members = family.get("members", [])
    lines = []
    for m in members:
        line = f"- {m['name']} ({m.get('role', 'member')}): level={m.get('english_level', 'beginner')}"
        if m.get("age"):
            line += f", age={m['age']}"
        if m.get("interests"):
            line += f", interests: {m['interests']}"
        lines.append(line)
    return "\n".join(lines) or "- No specific info"


def build_prompt_v2(scene: dict, family: dict, current_time: str,
                    mode: str = "pass_by") -> tuple:
    """
    构建 V2.1 版本的 system + user prompt。
    mode: "pass_by" | "interact" | "scheduled"
    """
    target = scene.get("target", "family")
    family_info = _build_family_info(family)

    if mode == "pass_by":
        sub = scene.get("pass_by", {})
        scene_type = sub.get("type", "pass_by_hello")
        scene_desc = sub.get("description", "路过打招呼")
        system_prompt = SYSTEM_PROMPT_PASS_BY.format(
            scene_type=scene_type, scene_desc=scene_desc,
            target_desc=TARGET_DESCRIPTIONS.get(target, TARGET_DESCRIPTIONS["family"]),
            current_time=current_time, family_info=family_info,
        )
        user_prompt = PASS_BY_PROMPTS.get(scene_type, PASS_BY_PROMPTS["pass_by_hello"])

    elif mode == "interact":
        sub = scene.get("interact", {})
        scene_type = sub.get("type", "daily_lesson")
        scene_desc = sub.get("description", "互动教学")
        system_prompt = SYSTEM_PROMPT_INTERACT.format(
            scene_type=scene_type, scene_desc=scene_desc,
            target_desc=TARGET_DESCRIPTIONS.get(target, TARGET_DESCRIPTIONS["family"]),
            current_time=current_time, family_info=family_info,
        )
        user_prompt = INTERACT_PROMPTS.get(scene_type, INTERACT_PROMPTS["daily_lesson"])

    elif mode == "scheduled":
        scene_type = scene.get("type", "daily_briefing")
        scene_desc = scene.get("description", "定时播报")
        content_hint = scene.get("content_hint", "Share something useful in English")
        system_prompt = SYSTEM_PROMPT_SCHEDULED.format(
            scene_type=scene_type, scene_desc=scene_desc,
            content_hint=content_hint,
            target_desc=TARGET_DESCRIPTIONS.get(target, TARGET_DESCRIPTIONS["family"]),
            current_time=current_time, family_info=family_info,
        )
        user_prompt = SCHEDULED_PROMPTS.get(scene_type,
                                            f"Deliver the scheduled content: {content_hint}")
    else:
        # fallback
        return build_prompt_v2(scene, family, current_time, mode="pass_by")

    return system_prompt, user_prompt


# ============================================
# AI 内容生成
# ============================================

def normalize_spoken_content(content: str, max_chars: int) -> str | None:
    """清洗 AI 生成结果，支持中英双语格式（用 --- 分隔）。
    返回清洗后的完整内容（包含中英文两部分）。
    max_chars 限制的是英文部分的长度。
    """
    content = (content or "").strip()
    if not content:
        return None

    content = re.sub(r'\[(?:English|Chinese|Key\s*Words?)\]\s*', '', content, flags=re.IGNORECASE)

    # 按 --- 分隔符拆分英文和中文部分
    parts = re.split(r'\n\s*---\s*\n', content, maxsplit=1)

    en_part = parts[0].strip()
    cn_part = parts[1].strip() if len(parts) > 1 else ""

    # 清洗英文部分
    en_lines = [line.strip() for line in en_part.splitlines() if line.strip()]
    en_text = re.sub(r'\s+', ' ', ' '.join(en_lines)).strip()

    # 截断英文部分（如果超长）
    if len(en_text) > max_chars:
        sentence_endings = [en_text.rfind(mark, 0, max_chars) for mark in ('.', '!', '?')]
        cut = max(sentence_endings)
        if cut >= max_chars // 2:
            en_text = en_text[:cut + 1]
        else:
            en_text = en_text[:max_chars].rstrip(' ,;:')

    if not en_text:
        return None

    # 清洗中文部分
    if cn_part:
        cn_lines = [line.strip() for line in cn_part.splitlines() if line.strip()]
        cn_text = ''.join(cn_lines)  # 中文不加空格
        return f"{en_text}\n---\n{cn_text}"
    else:
        return en_text


def generate_english_content(ai_client: OpenAI, model: str,
                             system_prompt: str, user_prompt: str,
                             logger: logging.Logger,
                             max_chars: int = 300) -> str:
    """调用 AI 生成英语口语内容"""
    logger.info("[AI] 正在生成英语内容...")
    start = time.time()

    try:
        resp = ai_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.9,
            max_tokens=300,
        )
        raw_content = (resp.choices[0].message.content or "").strip()
        elapsed = time.time() - start
        logger.info(f"[AI] 生成完成 ({elapsed:.1f}s, {len(raw_content)} 字符)")

        content = normalize_spoken_content(raw_content, max_chars=max_chars)
        if not content:
            logger.warning("[AI] 返回内容为空或清洗后不可播报")
            return None

        return content

    except Exception:
        logger.exception("[AI] 生成失败")
        return None




# ============================================
# OpenClaw Emily API 内容生成（方案 A：统一人设 + 记忆）
# ============================================

def generate_content_via_openclaw(
    api_url: str, api_token: str,
    mode: str, scene: dict, target: str,
    current_time: str, logger: logging.Logger,
    content_hint: str = "",
    timeout: int = 30,
    max_chars: int = 300,
) -> str:
    """
    通过 OpenClaw Emily API 生成英语内容。
    优先使用此方式（统一人设、有记忆、可通过 QQ 和摄像头共享同一个 Emily）。
    如果 API 不可用，调用者应 fallback 到 generate_english_content()。
    """
    logger.info("[OpenClaw] 正在通过 Emily API 生成内容...")
    start = time.time()

    if mode == "scheduled":
        scene_type = scene.get("type", "daily_briefing")
        description = scene.get("description", "")
    else:
        sub = scene.get(mode, scene.get("pass_by", {}))
        scene_type = sub.get("type", "pass_by_hello")
        description = sub.get("description", "")

    payload = {
        "mode": mode,
        "scene": scene_type,
        "target": target,
        "time": current_time,
        "description": description,
        "bilingual": True,  # 要求生成中英双语格式（英文 + --- + 中文解释）
    }
    if content_hint:
        payload["content_hint"] = content_hint

    try:
        body = json.dumps(payload).encode("utf-8")
        req = Request(
            api_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_token}",
            },
            method="POST",
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_text = data.get("text", "").strip()
        elapsed = time.time() - start
        api_elapsed = data.get("elapsed", 0)
        model = data.get("model", "unknown")

        logger.info(f"[OpenClaw] 生成完成 (本地 {elapsed:.1f}s, API {api_elapsed}s, "
                     f"model={model}, {len(raw_text)} 字符)")

        content = normalize_spoken_content(raw_text, max_chars=max_chars)
        if not content:
            logger.warning("[OpenClaw] API 返回空内容或清洗后不可播报")
            return None

        return content


    except HTTPError as e:
        logger.error(f"[OpenClaw] HTTP 错误 {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
        return None
    except URLError as e:
        logger.error(f"[OpenClaw] 连接失败: {e.reason}")
        return None
    except Exception:
        logger.exception("[OpenClaw] 生成失败")
        return None



# ============================================
# 触发记录器（双镜头联合判断 + 当天偶数次升级）
# ============================================

class TriggerTracker:
    """
    智能判断用户是"路过"还是"停留互动"。

    判断逻辑（双通道优先 + 当天计数兜底）：

    1. 双镜头联合判断（主要信号）:
       萤石 C7 有广角(ch1) + 云台(ch2) 两个镜头。
       如果 dual_window 秒内两个通道都产生了告警 → 判定为 interact（停留互动）
       原理：人快速路过通常只被一个镜头捕到，停留/正面面对时两个镜头都会检测到。

    2. 当天偶数次触发自动升级（兜底逻辑）:
       当天第 1 次触发 → pass_by
       当天第 2 次触发 → interact
       当天第 3 次 → pass_by
       ...交替进行
       原理：越活跃互动越深入，内容丰富不单调。

    最终结果：两个信号取 OR —— 任一判定为 interact 就用 interact。
    """

    def __init__(self, dual_window: int = 60):
        self.dual_window = dual_window         # 双镜头联合判断时间窗口（秒）
        self._recent_channels = deque()        # (timestamp, channel_no) 队列
        self._today_str = ""                   # 当天日期字符串
        self._today_trigger_count = 0          # 当天累计触发次数

    def record_trigger(self, channel_no: int = 0) -> str:
        """
        记录一次触发，返回判定的模式: "pass_by" | "interact"

        Args:
            channel_no: 告警通道号 (1=广角, 2=云台, 0=未知)
        """
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")

        # 每日重置计数
        if today != self._today_str:
            self._today_str = today
            self._today_trigger_count = 0

        self._today_trigger_count += 1

        # === 信号 1：双镜头联合判断 ===
        dual_triggered = False
        if channel_no > 0:
            self._recent_channels.append((now, channel_no))
            # 清理窗口外的记录
            cutoff = now - self.dual_window
            while self._recent_channels and self._recent_channels[0][0] < cutoff:
                self._recent_channels.popleft()
            # 检查窗口内是否同时出现了不同通道
            channels_in_window = set(ch for _, ch in self._recent_channels)
            if len(channels_in_window) >= 2:
                dual_triggered = True

        # === 信号 2：当天偶数次触发 → interact ===
        even_triggered = (self._today_trigger_count % 2 == 0)

        # === 综合判定：任一信号为 interact 就升级 ===
        if dual_triggered or even_triggered:
            reason = []
            if dual_triggered:
                reason.append("双镜头联合")
            if even_triggered:
                reason.append(f"当天第{self._today_trigger_count}次(偶数)")
            return "interact", ", ".join(reason)
        else:
            return "pass_by", f"当天第{self._today_trigger_count}次(奇数)"

    def clear(self):
        """清空触发记录"""
        self._recent_channels.clear()
        self._today_trigger_count = 0
        self._today_str = ""

    @property
    def today_count(self) -> int:
        """当天已触发次数"""
        return self._today_trigger_count


# ============================================
# 定时任务调度器
# ============================================

class ScheduledTaskRunner:
    """
    定时任务调度器 — 按配置的时间点自动触发播报。
    使用独立线程检查时间，到点时回调。
    """

    def __init__(self, tasks: list, callback, logger: logging.Logger):
        """
        tasks: config 中的 scheduled_tasks.tasks 列表
        callback: 触发时回调函数 callback(task_config)
        """
        self.tasks = tasks or []
        self.callback = callback
        self.logger = logger
        self._thread = None
        self._running = False
        self._today_done = set()  # 今天已执行的任务 (time+type)
        self._done_date = None

    def start(self):
        """启动调度线程"""
        if not self.tasks:
            self.logger.info("[定时] 无定时任务配置")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.logger.info(f"[定时] 调度器启动, {len(self.tasks)} 个任务:")
        for t in self.tasks:
            self.logger.info(f"  {t['time']} - {t.get('description', t.get('type', '?'))}"
                             f" ({t.get('repeat', 'daily')})")

    def stop(self):
        self._running = False

    def _run(self):
        """调度主循环"""
        while self._running:
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            current_weekday = now.weekday()  # 0=Monday
            today_str = now.strftime("%Y-%m-%d")

            if self._done_date != today_str:
                self._today_done.clear()
                self._done_date = today_str

            for task in self.tasks:
                task_time = str(task.get("time", "")).strip()
                task_key = f"{today_str}_{task_time}_{task.get('type', '')}"

                if task_key in self._today_done:
                    continue

                if current_time != task_time:
                    continue

                # 检查重复规则
                repeat = str(task.get("repeat", "daily")).strip().lower()
                if repeat == "weekday" and current_weekday >= 5:  # 周末跳过
                    continue
                if repeat == "weekend" and current_weekday < 5:  # 工作日跳过
                    continue

                # 触发
                self._today_done.add(task_key)
                self.logger.info(f"[定时] 触发定时任务: {task.get('description', task.get('type'))}")

                try:
                    self.callback(task)
                except Exception:
                    self.logger.exception("[定时] 任务执行失败")

            time.sleep(30)  # 每 30 秒检查一次



# ============================================
# Emily V2.1 主引擎
# ============================================

class EmilyV2:
    """
    Emily V2.1 主引擎：
    - 路过模式：短打招呼
    - 互动模式：深度英语教学
    - 定时模式：预制任务自动播报
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.config = config
        self.logger = logger

        # OpenClaw Emily API 配置（方案 A：统一人设 + 记忆）
        openclaw_cfg = config.get("openclaw_emily", {})
        self.use_openclaw = openclaw_cfg.get("enabled", False)
        self.openclaw_api_url = openclaw_cfg.get("api_url", "")
        self.openclaw_api_token = openclaw_cfg.get("api_token", "")
        self.openclaw_timeout = openclaw_cfg.get("timeout", 30)

        if self.use_openclaw:
            logger.info("[Emily] 🧠 AI 模式: OpenClaw Emily API (统一人设 + 记忆)")
            logger.info(f"[Emily]    API URL: {self.openclaw_api_url}")
        else:
            logger.info("[Emily] 🧠 AI 模式: 本地 Deepseek 直连")

        # AI 客户端（作为 fallback，或 OpenClaw 未启用时使用）
        ai_cfg = config["ai"]
        self.ai_client = OpenAI(
            api_key=ai_cfg["api_key"],
            base_url=ai_cfg.get("base_url", "https://api.deepseek.com"),
            timeout=float(ai_cfg.get("timeout_seconds", 30)),
        )

        self.ai_model = ai_cfg.get("model", "deepseek-chat")

        # TTS 引擎
        self.tts = StreamTTS(config["tts"], logger)

        # 摄像头喇叭推送（如果配置了 camera_speaker）
        speaker_cfg = config.get("camera_speaker", {})
        if speaker_cfg.get("enabled", False) and speaker_cfg.get("cam_ip"):
            self.camera_speaker = CameraSpeaker(speaker_cfg, logger)
            self.use_camera_speaker = True
            logger.info("[Emily] 音频输出: 摄像头喇叭 (RTSP Backchannel)")
        else:
            self.camera_speaker = None
            self.use_camera_speaker = False
            logger.info("[Emily] 音频输出: 本地扬声器 (PyAudio)")

        # 重复播放配置（默认 3 次）
        playback_cfg = config.get("playback", {})
        self.repeat_count = playback_cfg.get("repeat_count", 3)
        logger.info(f"[Emily] 重复播放: {self.repeat_count}次 (英文+中文解释)")

        # 萤石监控
        ezviz_cfg = config.get("ezviz", {})
        initial_token, token_expires, token_file = load_ezviz_cached_token(self.logger)

        self.token_mgr = EzvizTokenManager(
            app_key=ezviz_cfg["app_key"],
            app_secret=ezviz_cfg["app_secret"],
            initial_token=initial_token,
            expires_str=token_expires,
            token_file=token_file,
            logger=logger,
        )

        self.monitor = EzvizMonitor(ezviz_cfg, self.token_mgr, logger)

        # 家庭配置
        self.family = config.get("family", {})
        self.time_scenes = config.get("time_scenes", [])

        # 交互模式配置
        interaction_cfg = config.get("interaction", {})
        self.pass_by_cooldown = interaction_cfg.get("pass_by_cooldown", 120)
        self.interact_cooldown = interaction_cfg.get("interact_cooldown", 180)

        # 触发追踪器（双镜头联合判断 + 当天偶数次升级）
        self.tracker = TriggerTracker(
            dual_window=interaction_cfg.get("dual_window", 60),
        )

        # 冷却管理（从播放结束时刻开始计算）
        self._last_pass_by_time = 0
        self._last_interact_time = 0
        self._last_speak_end_time = 0   # 任何模式的最后一次播放结束时间

        # 深夜静默保护 (被动触发在此时段内不播放)
        quiet_cfg = config.get("quiet_hours", {})
        self.quiet_start = quiet_cfg.get("start", "22:00")
        self.quiet_end = quiet_cfg.get("end", "06:00")
        self.quiet_enabled = quiet_cfg.get("enabled", True)
        logger.info(f"[Emily] 深夜静默: {'开启' if self.quiet_enabled else '关闭'}"
                    f" ({self.quiet_start}-{self.quiet_end})")

        # 播放互斥锁 — 防止定时任务和被动触发同时播放
        self._speaking_lock = threading.Lock()
        self._is_speaking = False

        # 对话管理器（pass_by / interact 模式支持多轮对话）
        conv_cfg = config.get("conversation", {})
        self.conversation_enabled = conv_cfg.get("enabled", False)
        self.pass_by_max_rounds = conv_cfg.get("pass_by_max_rounds", 3)
        self.interact_max_rounds = conv_cfg.get("interact_max_rounds", 5)

        if self.conversation_enabled:
            self.conversation_mgr = ConversationManager(
                config=config,
                tts=self.tts,
                logger=logger,
                camera_speaker=self.camera_speaker,
            )
            logger.info(f"[Emily] 🎤 对话模式: 已启用 "
                        f"(路过{self.pass_by_max_rounds}轮, 互动{self.interact_max_rounds}轮, "
                        f"超时降级→单向{self.repeat_count}遍)")
        else:
            self.conversation_mgr = None
            logger.info("[Emily] 🔇 对话模式: 未启用 (纯单向播放)")

        # 定时任务
        scheduled_cfg = config.get("scheduled_tasks", {})
        self.scheduler = None
        if scheduled_cfg.get("enabled", False):
            self.scheduler = ScheduledTaskRunner(
                tasks=scheduled_cfg.get("tasks", []),
                callback=self.on_scheduled_task,
                logger=logger,
            )

    def _is_quiet_hour(self, now: datetime = None) -> bool:
        """判断当前是否在深夜静默时段内"""
        if not self.quiet_enabled:
            return False
        if now is None:
            now = datetime.now()

        try:
            current_minutes = now.hour * 60 + now.minute
            start_minutes = parse_hhmm_to_minutes(self.quiet_start)
            end_minutes = parse_hhmm_to_minutes(self.quiet_end)
        except (TypeError, ValueError):
            self.logger.warning("[Emily] quiet_hours 配置非法，已跳过静默时段判断")
            return False

        return is_time_in_range(current_minutes, start_minutes, end_minutes)

    def on_person_detected(self, trigger_info: dict):

        """
        有人被检测到时的处理流程：
        0. 深夜静默检查 & 播放互斥检查
        1. 判断交互模式（pass_by vs interact）
        2. 匹配时间场景
        3. AI 生成内容
        4. 流式 TTS 播放
        """
        now = datetime.now()
        now_ts = time.time()

        # 0a. 深夜静默保护
        if self._is_quiet_hour(now):
            self.logger.debug(f"[Emily] 🌙 深夜静默时段 ({self.quiet_start}-{self.quiet_end})，忽略触发")
            return

        # 0b. 播放互斥 — 如果正在说话，跳过本次触发
        if self._is_speaking:
            self.logger.debug("[Emily] 正在播放中，忽略本次触发")
            return

        # 1. 判断模式（双镜头联合 + 当天偶数次升级）
        channel_no = trigger_info.get("channel_no", 0)
        mode, mode_reason = self.tracker.record_trigger(channel_no)

        # 冷却检查（从上次播放结束时刻开始计算）
        # 2a. 统一冷却：上次播放还没结束+冷却完，任何模式都不触发
        cooldown = self.pass_by_cooldown if mode == "pass_by" else self.interact_cooldown
        if self._last_speak_end_time > 0:
            since_last_speak = now_ts - self._last_speak_end_time
            if since_last_speak < cooldown:
                remaining = cooldown - since_last_speak
                self.logger.debug(f"[Emily] 冷却中(距上次播放结束{since_last_speak:.0f}s)，剩余 {remaining:.0f}s")
                return

        mode_label = "路过打招呼" if mode == "pass_by" else "停留互动教学"
        self.logger.info(f"[Emily] 检测到人! 模式={mode_label}, 通道={channel_no}, "
                         f"判定原因={mode_reason}, 当天第{self.tracker.today_count}次, "
                         f"时间={now.strftime('%H:%M:%S')}")

        # 2. 匹配场景
        scene = match_time_scene(self.time_scenes, now)
        if not scene:
            self.logger.info("[Emily] 当前时间段无匹配场景，使用默认")
            scene = {
                "target": "family",
                "description": "默认时段",
                "pass_by": {"type": "pass_by_hello", "description": "路过打招呼"},
                "interact": {"type": "daily_lesson", "description": "日常英语教学"},
            }

        sub_scene = scene.get(mode, scene.get("pass_by", {}))
        self.logger.info(f"[Emily] 场景: {sub_scene.get('description', '?')}"
                         f" ({sub_scene.get('type', '?')})")

        # 3. AI 生成内容
        max_chars = 120 if mode == "pass_by" else 300
        content = None

        # 优先使用 OpenClaw Emily API（统一人设 + 记忆）
        if self.use_openclaw:
            content = generate_content_via_openclaw(
                api_url=self.openclaw_api_url,
                api_token=self.openclaw_api_token,
                mode=mode, scene=scene,
                target=scene.get("target", "family"),
                current_time=now.strftime("%H:%M"),
                logger=self.logger,
                timeout=self.openclaw_timeout,
                max_chars=max_chars,
            )

            if not content:
                self.logger.warning("[Emily] OpenClaw API 失败，fallback 到 Deepseek 直连")

        # Fallback：本地 Deepseek 直连
        if not content:
            system_prompt, user_prompt = build_prompt_v2(
                scene, self.family, now.strftime("%H:%M"), mode=mode
            )
            content = generate_english_content(
                self.ai_client, self.ai_model,
                system_prompt, user_prompt,
                self.logger, max_chars=max_chars,
            )

        if not content:
            self.logger.error("[Emily] AI 生成失败，跳过本次")
            return

        self.logger.info(f"[Emily] [{mode_label}] 即将播放 ({len(content)}字符): {content[:80]}...")

        # 4. 播放（对话模式 or 传统单向模式）
        if self.conversation_enabled and self.conversation_mgr:
            # 对话模式：播放一遍 → 等待回应 → 对话 or 降级重复播放
            max_rounds = (
                self.pass_by_max_rounds if mode == "pass_by"
                else self.interact_max_rounds
            )
            ok = self._conversation_with_lock(content, max_rounds, mode_label)
        else:
            # 传统模式：纯单向重复播放
            ok = self._speak_with_lock(content)

        elapsed_total = time.time() - now_ts
        # 冷却从播放结束时刻开始计算
        self._last_speak_end_time = time.time()
        if ok:
            self.logger.info(f"[Emily] ✅ [{mode_label}] 播放完成! (全链路耗时 {elapsed_total:.1f}s)")
        else:
            self.logger.error(f"[Emily] ❌ [{mode_label}] 播放失败 (耗时 {elapsed_total:.1f}s)")

    def on_scheduled_task(self, task: dict):
        """定时任务触发回调"""
        now = datetime.now()
        start_ts = time.time()
        self.logger.info(f"[Emily] 定时任务: {task.get('description', task.get('type'))}")

        # 播放互斥 — 如果正在说话（被动触发正在播放），等待完成
        if self._is_speaking:
            self.logger.info("[Emily] [定时] 等待当前播放完成...")
            # 最多等待 60 秒
            wait_start = time.time()
            while self._is_speaking and (time.time() - wait_start) < 60:
                time.sleep(1)

        content = None

        # 优先使用 OpenClaw Emily API
        if self.use_openclaw:
            content = generate_content_via_openclaw(
                api_url=self.openclaw_api_url,
                api_token=self.openclaw_api_token,
                mode="scheduled", scene=task,
                target=task.get("target", "family"),
                current_time=now.strftime("%H:%M"),
                logger=self.logger,
                content_hint=task.get("content_hint", ""),
                timeout=self.openclaw_timeout,
                max_chars=350,
            )

            if not content:
                self.logger.warning("[Emily] OpenClaw API 失败，fallback 到 Deepseek 直连")

        # Fallback：本地 Deepseek 直连
        if not content:
            system_prompt, user_prompt = build_prompt_v2(
                task, self.family, now.strftime("%H:%M"), mode="scheduled"
            )
            content = generate_english_content(
                self.ai_client, self.ai_model,
                system_prompt, user_prompt,
                self.logger, max_chars=350,
            )

        if not content:
            self.logger.error("[Emily] 定时任务 AI 生成失败")
            return

        self.logger.info(f"[Emily] [定时播报] 即将播放 ({len(content)}字符): {content[:80]}...")

        ok = self._speak_with_lock(content)
        elapsed_total = time.time() - start_ts
        # 定时播报结束也更新冷却时间，避免播完立刻被动触发
        self._last_speak_end_time = time.time()
        if ok:
            self.logger.info(f"[Emily] ✅ [定时播报] 播放完成! (全链路耗时 {elapsed_total:.1f}s)")
        else:
            self.logger.error(f"[Emily] ❌ [定时播报] 播放失败 (耗时 {elapsed_total:.1f}s)")

    def _conversation_with_lock(self, text: str, max_rounds: int, mode_label: str) -> bool:
        """
        带互斥锁的对话模式。

        流程:
          1. 播放开场白（1遍）
          2. 聆听 MuMu 回应
          3a. 有回应 → 多轮对话（最多 max_rounds 轮）
          3b. 无回应 → 降级为单向重复播放（补剩余遍数）

        Args:
            text: Emily 的开场白（中英双语）
            max_rounds: 最大对话轮数
            mode_label: 模式标签（用于日志）
        """
        acquired = self._speaking_lock.acquire(blocking=False)
        if not acquired:
            self.logger.warning("[Emily] 播放锁被占用，跳过本次")
            return False

        try:
            self._is_speaking = True
            self.logger.info(
                f"[Emily] [{mode_label}] 🎤 对话模式启动 "
                f"(最多{max_rounds}轮, 无回应→单向{self.repeat_count}遍)"
            )

            result = self.conversation_mgr.start_conversation(
                initial_text=text,
                max_rounds=max_rounds,
            )

            if result["mode"] == "conversation":
                self.logger.info(
                    f"[Emily] [{mode_label}] 🎤 对话完成 "
                    f"({result['rounds']}轮, {result['total_time']:.1f}s)"
                )
            else:
                self.logger.info(
                    f"[Emily] [{mode_label}] 📢 降级单向播放完成 "
                    f"({result['total_time']:.1f}s)"
                )

            return True

        except Exception:
            self.logger.exception(f"[Emily] [{mode_label}] 对话模式异常")
            return False
        finally:
            self._is_speaking = False
            self._speaking_lock.release()

    def _speak_with_lock(self, text: str) -> bool:
        """
        带互斥锁的语音播放方法。
        确保同一时间只有一个语音在播放（避免定时任务与被动触发冲突）。
        """
        acquired = self._speaking_lock.acquire(blocking=False)
        if not acquired:
            self.logger.warning("[Emily] 播放锁被占用，跳过本次播放")
            return False

        try:
            self._is_speaking = True
            return self._speak(text)
        finally:
            self._is_speaking = False
            self._speaking_lock.release()

    def _speak(self, text: str) -> bool:
        """
        统一的语音播放方法，支持中英双语和重复播放。

        播放流程：
        1. 将内容按 --- 分隔为英文和中文两段
        2. 分别合成 TTS（英文用英文声音，中文用中文声音）
        3. 播放顺序：英文 → (停顿) → 中文 → (长停顿) → 重复共 repeat_count 次
        """
        # 解析中英双语内容
        parts = re.split(r'\n---\n', text, maxsplit=1)
        en_text = parts[0].strip()
        cn_text = parts[1].strip() if len(parts) > 1 else ""

        if self.use_camera_speaker and self.camera_speaker:
            # 摄像头喇叭模式: TTS 合成 PCM → camera_speaker 转码推送
            # 合成英文部分
            en_pcm = self.tts.synthesize(en_text) if en_text else b""
            if not en_pcm:
                self.logger.error("[Emily] TTS 英文合成失败")
                return False

            # 合成中文部分（如果有）
            cn_pcm = b""
            if cn_text:
                cn_pcm = self.tts.synthesize(cn_text)
                if not cn_pcm:
                    self.logger.warning("[Emily] TTS 中文合成失败，仅播放英文")

            # 生成静音间隔 PCM（16kHz, 16-bit, mono）
            sample_rate = self.tts.tts_config.get("sample_rate", 16000)
            short_pause = b'\x00' * (sample_rate * 2 * 2)   # 2 秒短停顿（英文→中文）
            long_pause = b'\x00' * (sample_rate * 2 * 4)    # 4 秒长停顿（轮次之间）

            # 构建单次播放的 PCM 序列：英文 + 短停顿 + 中文
            single_round = en_pcm
            if cn_pcm:
                single_round += short_pause + cn_pcm

            # 组装完整 PCM：重复 repeat_count 次，轮次间加长停顿
            repeat_count = self.repeat_count
            full_pcm = single_round
            for i in range(1, repeat_count):
                full_pcm += long_pause + single_round

            total_duration = len(full_pcm) / (sample_rate * 2)
            self.logger.info(f"[Emily] 播放计划: 英文{len(en_pcm)//1000}KB"
                             f"{'+中文' + str(len(cn_pcm)//1000) + 'KB' if cn_pcm else ''}"
                             f" x{repeat_count}次, 总时长约{total_duration:.1f}s")

            return self.camera_speaker.speak_pcm(full_pcm)
        else:
            # 本地播放模式: 流式 TTS + PyAudio（重复播放）
            for i in range(self.repeat_count):
                if i > 0:
                    self.logger.info(f"[Emily] 第{i+1}次重复播放...")
                    time.sleep(4)  # 轮次间隔 4 秒
                if not self.tts.speak(en_text):
                    return False
                if cn_text:
                    time.sleep(2)  # 英中间隔 2 秒
                    if not self.tts.speak(cn_text):
                        return False
            return True

    def run(self):
        """主循环：持续轮询摄像头告警 + 定时任务"""
        self.logger.info("=" * 60)
        self.logger.info("[Emily V2.1] Emily 英语外教 V2.1 启动!")
        self.logger.info("[Emily V2.1] 支持模式: 路过打招呼 | 停留互动 | 定时播报")
        self.logger.info("=" * 60)

        # 设备发现
        serial = self.monitor.discover_device()
        if not serial:
            self.logger.error("[Emily V2.1] 没有可用设备，退出")
            return

        self.logger.info(f"[Emily V2.1] 监控设备: {serial}")
        self.logger.info(f"[Emily V2.1] 轮询间隔: {self.monitor.poll_interval}s")
        self.logger.info(f"[Emily V2.1] 路过冷却: {self.pass_by_cooldown}s (播放结束后计算)")
        self.logger.info(f"[Emily V2.1] 互动冷却: {self.interact_cooldown}s (播放结束后计算)")
        self.logger.info(f"[Emily V2.1] 双镜头窗口: {self.tracker.dual_window}s (广角+云台同时触发→interact)")
        self.logger.info(f"[Emily V2.1] 当天偶数次自动升级: 第2/4/6/...次触发→interact")
        self.logger.info(f"[Emily V2.1] 深夜静默: {'开启' if self.quiet_enabled else '关闭'}"
                         f" ({self.quiet_start}-{self.quiet_end})")
        if self.conversation_enabled:
            self.logger.info(f"[Emily V2.1] 🎤 对话模式: 路过{self.pass_by_max_rounds}轮 / "
                             f"互动{self.interact_max_rounds}轮 / 超时→单向{self.repeat_count}遍")
        else:
            self.logger.info(f"[Emily V2.1] 📢 纯单向播放模式: {self.repeat_count}遍")
        self.logger.info(f"[Emily V2.1] 时间场景: {len(self.time_scenes)} 个")

        # 启动定时任务
        if self.scheduler:
            self.scheduler.start()

        self.logger.info("[Emily V2.1] 等待摄像头检测到人...")
        self.logger.info("-" * 60)

        poll_count = 0
        heartbeat_interval = 12  # 每 12 次轮询(60s)打一条心跳
        try:
            while True:
                trigger = self.monitor.check_trigger()
                if trigger:
                    self.on_person_detected(trigger)
                    self.logger.info("-" * 60)
                    self.logger.info("[Emily V2.1] 继续监控...")
                    poll_count = 0

                poll_count += 1
                if poll_count % heartbeat_interval == 0:
                    seen = len(self.monitor._seen_alarm_ids)
                    self.logger.info(f"[Emily V2.1] 💓 心跳: 已轮询 {poll_count} 次, "
                                     f"已处理告警 {seen} 条, 等待检测...")

                time.sleep(self.monitor.poll_interval)

        except KeyboardInterrupt:
            self.logger.info("\n[Emily V2.1] 收到退出信号，再见!")
            if self.scheduler:
                self.scheduler.stop()


# ============================================
# 入口（通过 run.py emily 调用）
# ============================================

def run_emily(config: dict, logger: logging.Logger):
    """启动 Emily V2.1"""
    emily = EmilyV2(config, logger)
    emily.run()
