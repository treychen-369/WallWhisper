#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emily - 家庭隐形英语外教 (MVP)
使用 Deepseek API 生成个性化英语内容，腾讯云 TTS 合成语音，系统播放器播放。
"""

import os
import sys
import re
import json
import base64
import time
import random
import logging
import argparse
from datetime import datetime, timedelta
from uuid import uuid4
from pathlib import Path

import yaml
from openai import OpenAI

from tencentcloud.common import credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.tts.v20190823 import tts_client, models


# ============================================
# 配置加载
# ============================================

def load_config(config_path: str = None) -> dict:
    """加载 YAML 配置文件，缺少必填项时报错"""
    if config_path is None:
        # 默认在脚本所在目录找 config.yaml
        script_dir = Path(__file__).parent
        config_path = script_dir / "config.yaml"

    config_path = Path(config_path)
    if not config_path.exists():
        print(f"[ERROR] 配置文件不存在: {config_path}")
        print(f"   请复制 config.example.yaml 为 config.yaml 并填入你的 API 密钥")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 验证必填配置
    errors = []
    ai = config.get("ai", {})
    if not ai.get("api_key") or ai["api_key"].startswith("sk-xxxx"):
        errors.append("ai.api_key 未配置")
    if not ai.get("base_url"):
        errors.append("ai.base_url 未配置")

    tts = config.get("tts", {})
    if not tts.get("secret_id") or tts["secret_id"] == "your-secret-id":
        errors.append("tts.secret_id 未配置")
    if not tts.get("secret_key") or tts["secret_key"] == "your-secret-key":
        errors.append("tts.secret_key 未配置")

    if errors:
        print("[ERROR] 配置文件缺少必填项:")
        for e in errors:
            print(f"   - {e}")
        sys.exit(1)

    return config


# ============================================
# 日志初始化
# ============================================

def setup_logging(config: dict) -> logging.Logger:
    """初始化日志，同时输出到终端和日志文件"""
    log_config = config.get("logging", {})
    level_str = log_config.get("level", "INFO").upper()
    log_file = log_config.get("file", "logs/emily.log")

    level = getattr(logging, level_str, logging.INFO)

    logger = logging.getLogger("emily")
    logger.setLevel(level)

    # 避免重复添加 handler
    if logger.handlers:
        return logger

    # 终端输出（强制 UTF-8 编码，解决 Windows GBK 终端 emoji 问题）
    import io
    utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    console_handler = logging.StreamHandler(utf8_stdout)
    console_handler.setLevel(level)
    console_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # 文件输出
    script_dir = Path(__file__).parent
    log_path = script_dir / log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
    file_handler.setLevel(level)
    file_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger


# ============================================
# 场景匹配
# ============================================

def match_scene(schedule: list, now: datetime = None) -> dict:
    """
    根据当前时间匹配最近的场景（前后30分钟内）。
    找不到则随机选一个场景。
    """
    if now is None:
        now = datetime.now()

    current_minutes = now.hour * 60 + now.minute
    best_scene = None
    best_diff = float("inf")

    for scene in schedule:
        time_str = scene.get("time", "12:00")
        parts = time_str.split(":")
        scene_minutes = int(parts[0]) * 60 + int(parts[1])
        diff = abs(current_minutes - scene_minutes)
        if diff < best_diff:
            best_diff = diff
            best_scene = scene

    # 30分钟内匹配
    if best_diff <= 30 and best_scene:
        return best_scene

    # 找不到就随机选一个
    return random.choice(schedule)


# ============================================
# Prompt 构建
# ============================================

SYSTEM_PROMPT_TEMPLATE = """You are Emily, a warm, friendly, and enthusiastic English tutor for a Chinese family.

Your personality:
- Patient and encouraging, especially with beginners
- You love using everyday situations to teach English naturally
- You always include both English content and Chinese translations
- You explain key vocabulary in simple terms

Current scene: {scene_type} ({scene_desc})
Target audience: {target_desc}

Family members info:
{family_info}

IMPORTANT RULES:
1. Keep your English content under 300 letters (for TTS compatibility)
2. Use simple, natural, conversational English
3. Always respond in this EXACT format:

[English]
(Your English content here - greeting, story, vocabulary, etc.)

[Chinese]
(Chinese translation of the English content)

[Key Words]
(2-3 key words/phrases with simple explanations in Chinese)
"""

TARGET_DESCRIPTIONS = {
    "family": "the whole family (both adults and a 6-year-old child)",
    "adult": "the adult (intermediate English level, interested in tech and movies)",
    "kid": "a 6-year-old child (beginner English, loves dinosaurs, drawing, and Peppa Pig)",
}

SCENE_USER_PROMPTS = {
    "morning_greeting": "Generate a cheerful morning greeting in English. Include a fun English phrase or expression for the day.",
    "school_sendoff": "Create an encouraging send-off message for a child going to school. Use simple words and include a fun English expression.",
    "lunch_chat": "Share an interesting English conversation topic suitable for a casual lunch break. Can be about tech, movies, or daily life.",
    "welcome_home": "Create a warm welcome-home message for a child returning from school. Ask about their day using simple English.",
    "daily_word": "Teach one interesting English word or phrase. Explain its meaning, give an example sentence, and share a fun fact about it.",
    "dinner_funfact": "Share a fun and interesting fact in English that the whole family can discuss during dinner.",
    "bedtime_story": "Tell a very short bedtime story in simple English (3-5 sentences). Make it magical and soothing.",
}


def build_prompt(scene: dict, family: dict) -> tuple:
    """根据场景和家庭成员构建 (system_prompt, user_prompt)"""
    scene_type = scene.get("type", "daily_word")
    scene_desc = scene.get("description", "日常英语")
    target = scene.get("target", "family")

    # 构建家庭信息
    members = family.get("members", [])
    family_lines = []
    for m in members:
        line = f"- {m['name']} ({m.get('role', 'member')}): English level={m.get('english_level', 'beginner')}"
        if m.get("age"):
            line += f", age={m['age']}"
        if m.get("interests"):
            line += f", interests: {m['interests']}"
        family_lines.append(line)
    family_info = "\n".join(family_lines) if family_lines else "- No specific family info provided"

    target_desc = TARGET_DESCRIPTIONS.get(target, TARGET_DESCRIPTIONS["family"])

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        scene_type=scene_type,
        scene_desc=scene_desc,
        target_desc=target_desc,
        family_info=family_info,
    )

    user_prompt = SCENE_USER_PROMPTS.get(scene_type, SCENE_USER_PROMPTS["daily_word"])

    return system_prompt, user_prompt


# ============================================
# AI 调用（Deepseek）
# ============================================

def generate_content(client: OpenAI, model: str, system_prompt: str, user_prompt: str, logger: logging.Logger) -> str:
    """调用 Deepseek API 生成英语教学内容"""
    logger.info("[AI] 正在调用 Deepseek 生成内容...")
    start_time = time.time()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.8,
            max_tokens=800,
        )
        elapsed = time.time() - start_time
        content = response.choices[0].message.content.strip()
        logger.info(f"[OK] AI 生成完成 (耗时 {elapsed:.1f}s)")
        return content
    except Exception as e:
        logger.error(f"[FAIL] Deepseek API 调用失败: {e}")
        return None


# ============================================
# 响应解析
# ============================================

def parse_response(text: str) -> dict:
    """
    解析 AI 返回的结构化文本，提取 english/chinese/keywords 三部分。
    支持格式:
    [English] ... [Chinese] ... [Key Words] ...
    """
    result = {"english": "", "chinese": "", "keywords": "", "raw": text}

    if not text:
        return result

    # 尝试用正则提取各段
    english_match = re.search(r"\[English\]\s*\n?(.*?)(?=\[Chinese\]|\[Key\s*Words?\]|\Z)", text, re.DOTALL | re.IGNORECASE)
    chinese_match = re.search(r"\[Chinese\]\s*\n?(.*?)(?=\[Key\s*Words?\]|\Z)", text, re.DOTALL | re.IGNORECASE)
    keywords_match = re.search(r"\[Key\s*Words?\]\s*\n?(.*?)$", text, re.DOTALL | re.IGNORECASE)

    if english_match:
        result["english"] = english_match.group(1).strip()
    if chinese_match:
        result["chinese"] = chinese_match.group(1).strip()
    if keywords_match:
        result["keywords"] = keywords_match.group(1).strip()

    # 如果解析失败，把全文当作英文内容
    if not result["english"]:
        result["english"] = text.strip()

    return result


# ============================================
# 腾讯云 TTS 语音合成
# ============================================

def split_text_for_tts(text: str, max_len: int = 450) -> list:
    """
    将长英文文本按句子边界分段，每段不超过 max_len 字母。
    确保不会在句子中间断开。
    """
    if len(text) <= max_len:
        return [text]

    # 按句子分割（.!? 后跟空格或换行）
    sentences = re.split(r'(?<=[.!?])\s+', text)
    segments = []
    current = ""

    for sentence in sentences:
        # 单个句子超过限制，强制按字数切割
        if len(sentence) > max_len:
            if current:
                segments.append(current.strip())
                current = ""
            # 按空格切割长句子
            words = sentence.split()
            for word in words:
                if len(current) + len(word) + 1 > max_len:
                    if current:
                        segments.append(current.strip())
                    current = word
                else:
                    current = f"{current} {word}" if current else word
        elif len(current) + len(sentence) + 1 > max_len:
            segments.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence

    if current.strip():
        segments.append(current.strip())

    return segments


def synthesize_speech(text: str, tts_config: dict, output_path: str, logger: logging.Logger) -> str:
    """
    使用腾讯云 TTS 合成英文语音 MP3。
    支持长文本分段合成后拼接。
    返回输出文件路径，失败返回 None。
    """
    secret_id = tts_config["secret_id"]
    secret_key = tts_config["secret_key"]
    region = tts_config.get("region", "ap-guangzhou")
    voice_type = tts_config.get("voice_type", 501009)
    codec = tts_config.get("codec", "mp3")
    sample_rate = tts_config.get("sample_rate", 16000)
    speed = tts_config.get("speed", 0)
    volume = tts_config.get("volume", 0)

    # 初始化客户端
    try:
        cred = credential.Credential(secret_id, secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "tts.tencentcloudapi.com"
        client_profile = ClientProfile()
        client_profile.httpProfile = http_profile
        client = tts_client.TtsClient(cred, region, client_profile)
    except Exception as e:
        logger.error(f"[FAIL] 腾讯云 TTS 客户端初始化失败: {e}")
        return None

    # 分段处理
    segments = split_text_for_tts(text)
    logger.info(f"[TTS] 文本分段: {len(segments)} 段")

    audio_chunks = []
    for i, segment in enumerate(segments):
        logger.info(f"[TTS] 合成第 {i+1}/{len(segments)} 段 ({len(segment)} 字母)...")
        try:
            req = models.TextToVoiceRequest()
            req.Text = segment
            req.SessionId = str(uuid4())
            req.VoiceType = int(voice_type)
            req.Codec = codec
            req.SampleRate = int(sample_rate)
            req.Speed = float(speed)
            req.Volume = float(volume)
            req.PrimaryLanguage = 2  # 2=英文

            resp = client.TextToVoice(req)
            audio_data = base64.b64decode(resp.Audio)
            audio_chunks.append(audio_data)

            # 多段之间加短暂延迟，避免频率限制
            if len(segments) > 1 and i < len(segments) - 1:
                time.sleep(0.3)

        except TencentCloudSDKException as e:
            logger.error(f"[FAIL] 腾讯云 TTS 合成失败 (段 {i+1}): {e}")
            return None
        except Exception as e:
            logger.error(f"[FAIL] TTS 合成出错 (段 {i+1}): {e}")
            return None

    if not audio_chunks:
        logger.error("[FAIL] 没有成功合成任何音频")
        return None

    # 拼接并保存
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(output_path, "wb") as f:
            for chunk in audio_chunks:
                f.write(chunk)
        file_size = output_path.stat().st_size / 1024
        logger.info(f"[OK] 语音文件已保存: {output_path} ({file_size:.1f} KB)")
        return str(output_path)
    except Exception as e:
        logger.error(f"[FAIL] 保存音频文件失败: {e}")
        return None


# ============================================
# 音频播放
# ============================================

def play_audio(file_path: str, logger: logging.Logger) -> None:
    """使用系统默认播放器播放 MP3 文件（Windows）"""
    try:
        if sys.platform == "win32":
            os.startfile(file_path)
            logger.info("[PLAY] 已启动系统播放器播放语音")
        elif sys.platform == "darwin":
            os.system(f'open "{file_path}"')
            logger.info("[PLAY] 已启动系统播放器播放语音")
        else:
            os.system(f'xdg-open "{file_path}"')
            logger.info("[PLAY] 已启动系统播放器播放语音")
    except Exception as e:
        logger.warning(f"[WARN] 自动播放失败: {e}，请手动打开文件: {file_path}")


# ============================================
# 文件清理
# ============================================

def cleanup_old_audio(audio_dir: str, keep_days: int, logger: logging.Logger) -> int:
    """删除超过保留天数的旧 MP3 文件，返回清理数量"""
    audio_path = Path(audio_dir)
    if not audio_path.exists():
        return 0

    cutoff = time.time() - keep_days * 86400
    cleaned = 0

    for f in audio_path.glob("*.mp3"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                cleaned += 1
                logger.debug(f"[CLEAN] 清理旧文件: {f.name}")
        except Exception as e:
            logger.warning(f"[WARN] 清理文件失败 {f.name}: {e}")

    if cleaned > 0:
        logger.info(f"[CLEAN] 已清理 {cleaned} 个过期音频文件")

    return cleaned


# ============================================
# 终端美化输出
# ============================================

def safe_print(text: str) -> None:
    """安全打印，处理 Windows GBK 终端无法显示 emoji 的问题"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 回退：替换无法编码的字符
        encoded = text.encode(sys.stdout.encoding or "gbk", errors="replace")
        print(encoded.decode(sys.stdout.encoding or "gbk", errors="replace"))


def print_content(parsed: dict) -> None:
    """在终端美观地打印英文内容、中文翻译和关键词"""
    safe_print("\n" + "=" * 60)
    safe_print("[*] Emily's English Time!")
    safe_print("=" * 60)

    if parsed["english"]:
        safe_print(f"\n[English]\n")
        safe_print(f"   {parsed['english']}")

    if parsed["chinese"]:
        safe_print(f"\n[Chinese]\n")
        safe_print(f"   {parsed['chinese']}")

    if parsed["keywords"]:
        safe_print(f"\n[Key Words]\n")
        safe_print(f"   {parsed['keywords']}")

    safe_print("\n" + "=" * 60)


# ============================================
# 主流程
# ============================================

def main():
    parser = argparse.ArgumentParser(description="Emily - 家庭隐形英语外教")
    parser.add_argument("--now", action="store_true", help="立即执行一次（手动触发）")
    parser.add_argument("--config", type=str, default=None, help="指定配置文件路径")
    args = parser.parse_args()

    if not args.now:
        print("[*] 使用方法: python emily.py --now")
        print("   --now    立即执行一次英语教学")
        print("   --config 指定配置文件路径 (默认: config.yaml)")
        sys.exit(0)

    # 1. 加载配置
    config = load_config(args.config)
    logger = setup_logging(config)

    logger.info("=" * 50)
    logger.info("[Emily] Emily 英语外教启动!")
    logger.info("=" * 50)

    # 2. 清理过期音频
    script_dir = Path(__file__).parent
    audio_config = config.get("audio", {})
    audio_dir = script_dir / audio_config.get("output_dir", "audio")
    keep_days = audio_config.get("keep_days", 7)
    cleanup_old_audio(str(audio_dir), keep_days, logger)

    # 3. 匹配场景
    schedule = config.get("schedule", [])
    if not schedule:
        logger.warning("[WARN] 未配置任何场景，使用默认场景")
        schedule = [{"time": "12:00", "target": "family", "type": "daily_word", "description": "每日一词"}]

    scene = match_scene(schedule)
    logger.info(f"[SCENE] 当前场景: {scene.get('description', '未知')} ({scene.get('type', 'unknown')})")
    logger.info(f"[SCENE] 目标听众: {scene.get('target', 'family')}")

    # 4. 构建 Prompt
    family = config.get("family", {})
    system_prompt, user_prompt = build_prompt(scene, family)

    # 5. 调用 Deepseek 生成内容
    ai_config = config.get("ai", {})
    ai_client = OpenAI(
        api_key=ai_config["api_key"],
        base_url=ai_config.get("base_url", "https://api.deepseek.com"),
    )

    raw_content = generate_content(
        client=ai_client,
        model=ai_config.get("model", "deepseek-chat"),
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        logger=logger,
    )

    if not raw_content:
        logger.error("[FAIL] AI 未生成任何内容，流程终止")
        sys.exit(1)

    # 6. 解析响应
    parsed = parse_response(raw_content)

    # 7. 终端打印
    print_content(parsed)

    # 8. TTS 合成
    if parsed["english"]:
        tts_config = config.get("tts", {})
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        scene_type = scene.get("type", "daily")
        output_file = audio_dir / f"emily_{scene_type}_{timestamp}.mp3"

        audio_path = synthesize_speech(
            text=parsed["english"],
            tts_config=tts_config,
            output_path=str(output_file),
            logger=logger,
        )

        # 9. 播放
        if audio_path and audio_config.get("auto_play", True):
            play_audio(audio_path, logger)
    else:
        logger.warning("[WARN] 没有英文内容可以合成语音")

    logger.info("[Emily] Emily 本次任务完成!")


if __name__ == "__main__":
    main()
