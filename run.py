#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emily 通用启动器 - 用 __file__ 自动定位目录，彻底规避路径特殊字符问题。

用法:
    python run.py test_tts            # 测试流式 TTS
    python run.py test_tts "自定义文本"
    python run.py test_ezviz          # 测试萤石摄像头
    python run.py test_speaker        # 测试摄像头喇叭推送 (TTS → 摄像头)
    python run.py test_speaker "文本"  # 指定文本测试摄像头喇叭
    python run.py test_full           # 端到端测试（默认 pass_by 模式）
    python run.py test_full pass_by   # 测试路过打招呼模式
    python run.py test_full interact  # 测试停留互动教学模式
    python run.py test_full scheduled # 测试定时播报模式
    python run.py test_asr            # 测试麦克风录音 + ASR 语音识别
    python run.py test_cam_mic        # 测试摄像头麦克风 (RTSP 音频采集)
    python run.py test_cam_asr        # 测试摄像头麦克风 + ASR (端到端语音识别)
    python run.py test_conversation   # 测试多轮对话 (麦克风 → ASR → AI → TTS)
    python run.py emily               # 运行 Emily V2.1 主程序
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config_loader import ConfigError, load_and_validate_config

# 关键：切到脚本自身所在目录，后续所有相对路径都正确
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# 设置编码
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_LOG_DATE_FORMAT = "%H:%M:%S"


def _resolve_log_level(level_name: str | None, default_level: int) -> int:
    if not level_name:
        return default_level

    resolved = getattr(logging, str(level_name).strip().upper(), None)
    return resolved if isinstance(resolved, int) else default_level


def _build_logger(name: str, config: dict | None = None, default_level: int = logging.INFO) -> logging.Logger:
    log_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    level = _resolve_log_level(log_cfg.get("level"), default_level)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    log_file = str(log_cfg.get("file", "")).strip()
    if log_file:
        log_path = Path(log_file)
        if not log_path.is_absolute():
            log_path = Path(HERE) / log_path
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=int(log_cfg.get("max_bytes", 1_048_576)),
                backupCount=int(log_cfg.get("backup_count", 3)),
                encoding="utf-8",
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
        except Exception as exc:
            root_logger.warning(f"[LOG] 日志文件初始化失败，已回退到控制台输出: {exc}")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger


def _load_config_or_exit(command_name: str) -> dict:
    try:
        return load_and_validate_config(command_name)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)


def _prepare_command(command_name: str, logger_name: str, default_level: int) -> tuple[dict, logging.Logger]:
    config = _load_config_or_exit(command_name)
    logger = _build_logger(logger_name, config, default_level)
    logger.debug(f"[RUN] 工作目录: {os.getcwd()}")
    return config, logger


def cmd_test_tts(args):
    """测试流式 TTS 模块"""
    from tts_stream import StreamTTS

    config, logger = _prepare_command("test_tts", "tts_test", logging.DEBUG)
    tts_cfg = config["tts"]

    text = " ".join(args) if args else (
        "Hello! Good morning! I am Emily, your friendly English tutor."
    )

    logger.info(f"[TEST] 测试文本: {text}")
    logger.info(
        f"[TEST] AppId={tts_cfg['app_id']}, Voice={tts_cfg.get('voice_type', 101009)}, "
        f"Rate={tts_cfg.get('sample_rate', 16000)}"
    )

    engine = StreamTTS(tts_cfg, logger)
    ok = engine.speak(text)

    if ok:
        logger.info("[TEST] ✅ 流式 TTS 测试成功!")
    else:
        logger.error("[TEST] ❌ 流式 TTS 测试失败")
        sys.exit(1)


def cmd_test_ezviz(args):
    """测试萤石摄像头模块"""
    from ezviz_monitor import run_test

    config, logger = _prepare_command("test_ezviz", "ezviz_test", logging.DEBUG)

    ok = run_test(config, logger)
    if ok:
        logger.info("[TEST] ✅ 萤石模块测试完成!")
    else:
        logger.error("[TEST] ❌ 萤石模块测试失败")
        sys.exit(1)


def cmd_test_speaker(args):
    """测试摄像头喇叭推送 (TTS → AAC → 摄像头喇叭)"""
    from camera_speaker import CameraSpeaker
    from tts_stream import StreamTTS

    config, logger = _prepare_command("test_speaker", "speaker_test", logging.INFO)

    speaker_cfg = config.get("camera_speaker", {})
    text = " ".join(args) if args else (
        "Hello! I am Emily, your friendly English tutor. "
        "Can you hear me through the camera speaker? This is working perfectly!"
    )

    logger.info(f"[TEST] 测试文本: {text}")

    tts = StreamTTS(config["tts"], logger)
    logger.info("[TEST] Step 1: TTS 合成...")
    pcm_data = tts.synthesize(text)
    if not pcm_data:
        logger.error("[TEST] ❌ TTS 合成失败")
        sys.exit(1)

    duration = len(pcm_data) / (int(config["tts"].get("sample_rate", 16000)) * 2)
    logger.info(f"[TEST] TTS 合成完成: {duration:.1f}s")

    speaker = CameraSpeaker(speaker_cfg, logger)
    logger.info("[TEST] Step 2: 推送到摄像头喇叭...")
    logger.info("[TEST] >>> 请去摄像头附近听！<<<")

    ok = speaker.speak_pcm(pcm_data)
    if ok:
        logger.info("[TEST] ✅ 摄像头喇叭推送成功!")
    else:
        logger.error("[TEST] ❌ 摄像头喇叭推送失败")
        sys.exit(1)


def cmd_test_full(args):
    """端到端测试（AI生成+流式TTS，支持 pass_by/interact/scheduled 模式）"""
    from openai import OpenAI

    from emily_v2 import (
        build_prompt_v2,
        generate_content_via_openclaw,
        generate_english_content,
        match_time_scene,
    )
    from tts_stream import StreamTTS

    config, logger = _prepare_command("test_full", "full_test", logging.INFO)

    mode = args[0] if args else "pass_by"
    if mode not in ("pass_by", "interact", "scheduled"):
        logger.warning(f"[TEST] 未知模式 '{mode}', 使用 pass_by")
        mode = "pass_by"

    mode_labels = {
        "pass_by": "路过打招呼",
        "interact": "停留互动教学",
        "scheduled": "定时播报",
    }

    now = datetime.now()
    logger.info(f"[TEST] 端到端测试 | 模式: {mode_labels[mode]} | 时间: {now.strftime('%H:%M:%S')}")

    if mode == "scheduled":
        tasks = config.get("scheduled_tasks", {}).get("tasks", [])
        scene = tasks[0] if tasks else {
            "type": "daily_briefing",
            "target": "family",
            "description": "每日英语晨播",
            "content_hint": "A motivational quote and word of the day",
        }
        logger.info(f"[TEST] 定时任务: {scene.get('description', '?')}")
        max_chars = 350
    else:
        scene = match_time_scene(config.get("time_scenes", []), now)
        if not scene:
            scene = {
                "target": "family",
                "description": "默认时段",
                "pass_by": {"type": "pass_by_hello", "description": "路过打招呼"},
                "interact": {"type": "daily_lesson", "description": "日常英语教学"},
            }
        sub = scene.get(mode, scene.get("pass_by", {}))
        logger.info(f"[TEST] 匹配场景: {sub.get('description', '?')} ({sub.get('type', '?')})")
        max_chars = {"pass_by": 120, "interact": 300}.get(mode, 300)

    content = None
    openclaw_cfg = config.get("openclaw_emily", {})
    if openclaw_cfg.get("enabled", False):
        logger.info("[TEST] 🧠 AI 模式: OpenClaw Emily API (统一人设 + 记忆)")
        content = generate_content_via_openclaw(
            api_url=openclaw_cfg.get("api_url", ""),
            api_token=openclaw_cfg.get("api_token", ""),
            mode=mode,
            scene=scene,
            target=scene.get("target", "family"),
            current_time=now.strftime("%H:%M"),
            content_hint=scene.get("content_hint", ""),
            timeout=openclaw_cfg.get("timeout", 30),
            logger=logger,
            max_chars=max_chars,
        )
        if not content:
            logger.warning("[TEST] OpenClaw API 失败，fallback 到 Deepseek 直连")

    if not content:
        ai_cfg = config["ai"]
        ai_client = OpenAI(
            api_key=ai_cfg["api_key"],
            base_url=ai_cfg.get("base_url"),
            timeout=float(ai_cfg.get("timeout_seconds", 30)),
        )
        if not openclaw_cfg.get("enabled", False):
            logger.info("[TEST] 🧠 AI 模式: Deepseek 直连")

        system_prompt, user_prompt = build_prompt_v2(
            scene,
            config.get("family", {}),
            now.strftime("%H:%M"),
            mode=mode,
        )
        content = generate_english_content(
            ai_client,
            ai_cfg.get("model", "deepseek-chat"),
            system_prompt,
            user_prompt,
            logger,
            max_chars=max_chars,
        )

    if not content:
        logger.error("[TEST] ❌ AI 生成失败")
        sys.exit(1)
    logger.info(f"[TEST] AI 生成内容 ({len(content)}字符): {content}")

    speaker_cfg = config.get("camera_speaker", {})
    if speaker_cfg.get("enabled", False) and speaker_cfg.get("cam_ip"):
        from camera_speaker import CameraSpeaker

        logger.info("[TEST] 输出方式: 摄像头喇叭 (RTSP Backchannel)")
        tts = StreamTTS(config["tts"], logger)
        pcm_data = tts.synthesize(content)
        if not pcm_data:
            logger.error("[TEST] ❌ TTS 合成失败")
            sys.exit(1)
        speaker = CameraSpeaker(speaker_cfg, logger)
        ok = speaker.speak_pcm(pcm_data)
    else:
        logger.info("[TEST] 输出方式: 本地扬声器 (PyAudio)")
        tts = StreamTTS(config["tts"], logger)
        ok = tts.speak(content)

    if ok:
        logger.info(f"[TEST] ✅ {mode_labels[mode]}模式测试成功!")
    else:
        logger.error("[TEST] ❌ TTS 播放失败")
        sys.exit(1)


def cmd_test_asr(args):
    """测试麦克风录音 + ASR 语音识别"""
    from asr_engine import ASREngine

    config, logger = _prepare_command("test_asr", "asr_test", logging.DEBUG)

    # ASR 配置: 复用 TTS 密钥
    asr_cfg = config.get("asr", {})
    if not asr_cfg.get("secret_id"):
        asr_cfg["secret_id"] = config["tts"]["secret_id"]
    if not asr_cfg.get("secret_key"):
        asr_cfg["secret_key"] = config["tts"]["secret_key"]

    # 可选指定引擎类型: python run.py test_asr en / zh / zh_en
    if args:
        lang_map = {"en": "16k_en", "zh": "16k_zh", "zh_en": "16k_zh-PY", "mix": "16k_zh-PY"}
        engine_type = lang_map.get(args[0], args[0])
        asr_cfg["engine_type"] = engine_type
        logger.info(f"[TEST] 使用引擎: {engine_type}")

    engine = ASREngine(asr_cfg, logger)

    logger.info("=" * 50)
    logger.info("[TEST] ASR 语音识别测试")
    logger.info("[TEST] 请对着麦克风说话，说完后会自动识别")
    logger.info("=" * 50)

    # 连续测试 3 次
    for i in range(3):
        logger.info(f"\n[TEST] --- 第 {i+1}/3 次 ---")
        result = engine.listen_and_recognize()
        if result:
            logger.info(f"[TEST] >>> 识别结果: {result}")
        else:
            logger.info("[TEST] --- 未识别到内容")

        if i < 2:
            logger.info("[TEST] 准备下一次... (2秒)")
            import time
            time.sleep(2)

    logger.info("\n[TEST] ASR 测试完成!")


def cmd_test_cam_mic(args):
    """测试摄像头麦克风 (RTSP 音频采集)"""
    from camera_mic import CameraMic

    config, logger = _prepare_command("test_cam_mic", "cam_mic_test", logging.DEBUG)

    speaker_cfg = config.get("camera_speaker", {})
    if not speaker_cfg.get("cam_ip"):
        logger.error("[TEST] ❌ config.yaml 中未配置 camera_speaker.cam_ip")
        sys.exit(1)

    # 合并 camera_mic 专属配置（如果有的话）
    mic_cfg = dict(speaker_cfg)
    cam_mic_overrides = config.get("camera_mic", {})
    mic_cfg.update(cam_mic_overrides)

    mic = CameraMic(mic_cfg, logger)
    ok = mic.test()

    if not ok:
        sys.exit(1)


def cmd_test_cam_asr(args):
    """测试摄像头麦克风 + ASR (RTSP → PCM → 语音识别)"""
    from asr_engine import ASREngine
    from camera_mic import CameraMic

    config, logger = _prepare_command("test_cam_asr", "cam_asr_test", logging.INFO)

    # 摄像头麦克风配置
    speaker_cfg = config.get("camera_speaker", {})
    if not speaker_cfg.get("cam_ip"):
        logger.error("[TEST] ❌ config.yaml 中未配置 camera_speaker.cam_ip")
        sys.exit(1)

    mic_cfg = dict(speaker_cfg)
    cam_mic_overrides = config.get("camera_mic", {})
    mic_cfg.update(cam_mic_overrides)

    # ASR 配置
    asr_cfg = config.get("asr", {})
    if not asr_cfg.get("secret_id"):
        asr_cfg["secret_id"] = config["tts"]["secret_id"]
    if not asr_cfg.get("secret_key"):
        asr_cfg["secret_key"] = config["tts"]["secret_key"]

    # 可选指定引擎类型
    if args:
        lang_map = {"en": "16k_en", "zh": "16k_zh", "zh_en": "16k_zh-PY", "mix": "16k_zh-PY"}
        engine_type = lang_map.get(args[0], args[0])
        asr_cfg["engine_type"] = engine_type
        logger.info(f"[TEST] 使用引擎: {engine_type}")

    mic = CameraMic(mic_cfg, logger)
    asr = ASREngine(asr_cfg, logger)

    logger.info("=" * 60)
    logger.info("[TEST] 摄像头麦克风 → ASR 端到端测试")
    logger.info("[TEST] 请对着摄像头说话，说完后会自动识别")
    logger.info("=" * 60)

    for i in range(3):
        logger.info(f"\n[TEST] --- 第 {i+1}/3 次 ---")

        # 从摄像头麦克风录音
        pcm_data = mic.listen(timeout=8.0, max_duration=15.0)

        if pcm_data:
            # ASR 识别
            result = asr.recognize_pcm(pcm_data)
            if result:
                logger.info(f"[TEST] >>> 识别结果: {result}")
            else:
                logger.info("[TEST] --- ASR 未识别到内容")
        else:
            logger.info("[TEST] --- 未录到有效音频")

        if i < 2:
            logger.info("[TEST] 准备下一次... (2秒)")
            import time
            time.sleep(2)

    logger.info("\n[TEST] 摄像头 ASR 测试完成!")


def cmd_test_conversation(args):
    """测试多轮对话 (麦克风 → ASR → AI → TTS 完整链路)"""
    from conversation import ConversationManager
    from tts_stream import StreamTTS

    config, logger = _prepare_command("test_conversation", "conv_test", logging.INFO)

    # 初始化 TTS
    tts = StreamTTS(config["tts"], logger)

    # 强制关闭摄像头喇叭（本地测试用电脑扬声器）
    if "camera_speaker" in config:
        config["camera_speaker"]["enabled"] = False

    # 初始化对话管理器
    conv = ConversationManager(config, tts, logger)

    # 自定义最大轮数
    max_rounds = None
    if args and args[0].isdigit():
        max_rounds = int(args[0])
        logger.info(f"[TEST] 设置最大对话轮数: {max_rounds}")

    logger.info("=" * 60)
    logger.info("[TEST] 多轮对话测试")
    logger.info("[TEST] Emily 会先说一句，然后你对着麦克风回答")
    logger.info("[TEST] 对话会自动进行多轮")
    logger.info("=" * 60)

    # 让 Emily 先生成一个开场白
    from emily_v2 import generate_content_via_openclaw, match_time_scene

    now = datetime.now()
    scene = match_time_scene(config.get("time_scenes", []), now)
    if not scene:
        scene = {
            "target": "kid",
            "description": "对话测试",
            "interact": {"type": "interactive_dialogue", "description": "多轮对话测试"},
        }

    initial_text = None
    openclaw_cfg = config.get("openclaw_emily", {})
    if openclaw_cfg.get("enabled", False):
        logger.info("[TEST] 通过 OpenClaw 生成开场白...")
        initial_text = generate_content_via_openclaw(
            api_url=openclaw_cfg.get("api_url", ""),
            api_token=openclaw_cfg.get("api_token", ""),
            mode="interact",
            scene=scene,
            target="kid",
            current_time=now.strftime("%H:%M"),
            logger=logger,
            content_hint="Start a conversation with MuMu. Ask her a simple question to get her talking.",
            timeout=openclaw_cfg.get("timeout", 30),
            max_chars=200,
        )

    if not initial_text:
        initial_text = (
            "Hi MuMu! What did you do today? Did you play with Elsa?\n"
            "---\n"
            "木木你好！你今天做了什么呀？有没有和艾莎玩？"
        )
        logger.info("[TEST] 使用默认开场白")

    logger.info(f"[TEST] 开场白: {initial_text[:80]}...")

    # 启动对话
    result = conv.start_conversation(initial_text=initial_text, max_rounds=max_rounds)

    logger.info("\n" + "=" * 60)
    mode_str = "[CONVERSATION]" if result.get("mode") == "conversation" else "[FALLBACK]"
    logger.info(f"[TEST] 对话结果: {mode_str}")
    logger.info(f"  完成轮数: {result['rounds']}")
    logger.info(f"  总耗时: {result['total_time']:.1f}s")
    for item in result["texts"]:
        role = "[Emily]" if item["role"] == "emily" else "[MuMu] "
        logger.info(f"  {role}: {item['text'][:80]}...")
    logger.info("=" * 60)


def cmd_emily(args):
    """运行 Emily V2 主程序（摄像头联动模式）"""
    from emily_v2 import run_emily

    config, logger = _prepare_command("emily", "emily", logging.INFO)
    run_emily(config, logger)


COMMANDS = {
    "test_tts": cmd_test_tts,
    "test_ezviz": cmd_test_ezviz,
    "test_speaker": cmd_test_speaker,
    "test_full": cmd_test_full,
    "test_asr": cmd_test_asr,
    "test_cam_mic": cmd_test_cam_mic,
    "test_cam_asr": cmd_test_cam_asr,
    "test_conversation": cmd_test_conversation,
    "emily": cmd_emily,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        print("可用命令:")
        for name, fn in COMMANDS.items():
            print(f"  {name:16s} - {fn.__doc__}")
        return

    cmd_name = sys.argv[1]
    cmd_args = sys.argv[2:]

    if cmd_name not in COMMANDS:
        print(f"[ERROR] 未知命令: {cmd_name}")
        print(f"可用命令: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    try:
        COMMANDS[cmd_name](cmd_args)
    except KeyboardInterrupt:
        logging.getLogger("run").info("[RUN] 用户中断执行")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception:
        logging.getLogger("run").exception(f"[RUN] 命令执行失败: {cmd_name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
