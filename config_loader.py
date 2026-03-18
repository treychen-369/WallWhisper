#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Emily 配置加载与校验工具。"""

from __future__ import annotations

import importlib.util
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """配置无效时抛出。"""


_PLACEHOLDER_STRINGS = {
    "sk-xxxxxxxxxxxxxxxxxxxxxxxx",
    "your-secret-id",
    "your-secret-key",
    "your-app-id",
    "app-id",
    "xxxxxxxxxxxxxxxxxxxxxxxx",
    "xxxxxx",
    "192.168.1.xx",
    "https://your-openclaw.example.com/api/emily/generate",
    "your-openclaw-bearer-token",
    "your-ezviz-app-key",
    "your-ezviz-app-secret",
}

_ALLOWED_SCHEDULE_REPEATS = {"daily", "weekday", "weekend"}


def _default_config_path() -> Path:
    return Path(__file__).resolve().with_name("config.yaml")


def _read_yaml(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(
            f"配置文件不存在: {config_path}\n"
            "请先复制 `config.example.yaml` 为 `config.yaml`，再填入真实配置。"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    if not isinstance(config, dict):
        raise ConfigError(f"配置文件格式错误: {config_path} 顶层必须是 YAML 对象")

    return config


def _get_nested(config: dict[str, Any], dotted_path: str, default: Any = None) -> Any:
    current: Any = config
    for key in dotted_path.split("."):
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _is_placeholder(value: Any) -> bool:
    if value is None:
        return True

    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        return stripped in _PLACEHOLDER_STRINGS or stripped.lower().startswith("sk-xxxx")

    if isinstance(value, (int, float)):
        return value == 0

    return False


def _require_field(config: dict[str, Any], dotted_path: str, errors: list[str], hint: str | None = None) -> None:
    value = _get_nested(config, dotted_path)
    if _is_placeholder(value):
        errors.append(hint or f"{dotted_path} 未配置")


def _validate_positive_number(config: dict[str, Any], dotted_path: str, errors: list[str], hint: str) -> None:
    value = _get_nested(config, dotted_path)
    if value in (None, ""):
        return
    try:
        if float(value) <= 0:
            errors.append(hint)
    except (TypeError, ValueError):
        errors.append(hint)


def _validate_hhmm(value: Any, errors: list[str], hint: str) -> None:
    if value in (None, ""):
        errors.append(hint)
        return

    try:
        normalized = datetime.strptime(str(value), "%H:%M").strftime("%H:%M")
    except ValueError:
        errors.append(hint)
        return

    if normalized != str(value):
        errors.append(hint)


def _command_exists(command: Any) -> bool:
    if command in (None, ""):
        return False

    command_str = str(command).strip()
    if not command_str:
        return False

    command_path = Path(command_str).expanduser()
    if command_path.exists():
        return True

    return shutil.which(command_str) is not None


def _validate_local_audio_playback(errors: list[str]) -> None:
    if importlib.util.find_spec("pyaudio") is None:
        errors.append(
            "当前输出方式需要 `pyaudio`，但环境未安装。"
            "请先安装 PyAudio，或开启 `camera_speaker.enabled=true` 改走摄像头喇叭模式。"
        )


def _validate_tts(config: dict[str, Any], errors: list[str]) -> None:
    _require_field(config, "tts.app_id", errors, "tts.app_id 未配置")
    _require_field(config, "tts.secret_id", errors, "tts.secret_id 未配置")
    _require_field(config, "tts.secret_key", errors, "tts.secret_key 未配置")

    codec = str(_get_nested(config, "tts.codec", "pcm")).strip().lower()
    if codec != "pcm":
        errors.append("tts.codec 必须为 `pcm`，否则当前流式播放与摄像头喇叭链路无法正常工作")

    _validate_positive_number(config, "tts.sample_rate", errors, "tts.sample_rate 必须是正数")
    _validate_positive_number(config, "tts.speak_timeout_seconds", errors, "tts.speak_timeout_seconds 必须是正数")
    _validate_positive_number(config, "tts.synthesize_timeout_seconds", errors, "tts.synthesize_timeout_seconds 必须是正数")


def _validate_ai(config: dict[str, Any], errors: list[str]) -> None:
    _require_field(config, "ai.api_key", errors, "ai.api_key 未配置")
    _validate_positive_number(config, "ai.timeout_seconds", errors, "ai.timeout_seconds 必须是正数")


def _validate_ezviz(config: dict[str, Any], errors: list[str]) -> None:
    _require_field(config, "ezviz.app_key", errors, "ezviz.app_key 未配置")
    _require_field(config, "ezviz.app_secret", errors, "ezviz.app_secret 未配置")
    _validate_positive_number(config, "ezviz.poll_interval", errors, "ezviz.poll_interval 必须是正数")
    _validate_positive_number(config, "ezviz.cooldown", errors, "ezviz.cooldown 必须是正数")


def _validate_camera_speaker(config: dict[str, Any], errors: list[str]) -> None:
    _require_field(config, "camera_speaker.cam_ip", errors, "camera_speaker.cam_ip 未配置")
    _require_field(config, "camera_speaker.cam_password", errors, "camera_speaker.cam_password 未配置")
    _validate_positive_number(config, "camera_speaker.cam_rtsp_port", errors, "camera_speaker.cam_rtsp_port 必须是正整数")
    _validate_positive_number(config, "camera_speaker.sample_rate", errors, "camera_speaker.sample_rate 必须是正数")

    ffmpeg_path = _get_nested(config, "camera_speaker.ffmpeg_path", "ffmpeg")
    if not _command_exists(ffmpeg_path):
        errors.append(
            "camera_speaker.ffmpeg_path 不可用，请确认 FFmpeg 已安装并在 PATH 中，"
            "或填写正确的 ffmpeg 可执行文件路径"
        )


def _validate_openclaw_if_enabled(config: dict[str, Any], errors: list[str]) -> None:
    if not _get_nested(config, "openclaw_emily.enabled", False):
        return

    _require_field(config, "openclaw_emily.api_url", errors, "openclaw_emily.api_url 未配置")
    _require_field(config, "openclaw_emily.api_token", errors, "openclaw_emily.api_token 未配置")
    _validate_positive_number(config, "openclaw_emily.timeout", errors, "openclaw_emily.timeout 必须是正数")


def _validate_time_scenes(config: dict[str, Any], errors: list[str]) -> None:
    time_scenes = config.get("time_scenes", [])
    if time_scenes is None:
        return
    if not isinstance(time_scenes, list):
        errors.append("time_scenes 必须是列表")
        return

    for idx, scene in enumerate(time_scenes):
        if not isinstance(scene, dict):
            errors.append(f"time_scenes[{idx}] 必须是对象")
            continue
        _validate_hhmm(scene.get("start"), errors, f"time_scenes[{idx}].start 必须是 HH:MM 格式")
        _validate_hhmm(scene.get("end"), errors, f"time_scenes[{idx}].end 必须是 HH:MM 格式")


def _validate_scheduled_tasks(config: dict[str, Any], errors: list[str]) -> None:
    scheduled_cfg = config.get("scheduled_tasks", {})
    if not isinstance(scheduled_cfg, dict):
        errors.append("scheduled_tasks 必须是对象")
        return

    tasks = scheduled_cfg.get("tasks", [])
    if scheduled_cfg.get("enabled", False):
        if not isinstance(tasks, list) or not tasks:
            errors.append("scheduled_tasks.enabled=true 时，scheduled_tasks.tasks 不能为空")
            return

    if not isinstance(tasks, list):
        errors.append("scheduled_tasks.tasks 必须是列表")
        return

    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"scheduled_tasks.tasks[{idx}] 必须是对象")
            continue
        _validate_hhmm(task.get("time"), errors, f"scheduled_tasks.tasks[{idx}].time 必须是 HH:MM 格式")
        repeat = str(task.get("repeat", "daily")).strip().lower()
        if repeat not in _ALLOWED_SCHEDULE_REPEATS:
            errors.append(
                f"scheduled_tasks.tasks[{idx}].repeat 仅支持: {', '.join(sorted(_ALLOWED_SCHEDULE_REPEATS))}"
            )


def _validate_quiet_hours(config: dict[str, Any], errors: list[str]) -> None:
    quiet_cfg = config.get("quiet_hours", {})
    if quiet_cfg in (None, {}):
        return
    if not isinstance(quiet_cfg, dict):
        errors.append("quiet_hours 必须是对象")
        return

    _validate_hhmm(quiet_cfg.get("start"), errors, "quiet_hours.start 必须是 HH:MM 格式")
    _validate_hhmm(quiet_cfg.get("end"), errors, "quiet_hours.end 必须是 HH:MM 格式")


def _validate_interaction(config: dict[str, Any], errors: list[str]) -> None:
    _validate_positive_number(config, "interaction.pass_by_cooldown", errors, "interaction.pass_by_cooldown 必须是正数")
    _validate_positive_number(config, "interaction.interact_cooldown", errors, "interaction.interact_cooldown 必须是正数")
    _validate_positive_number(config, "interaction.dual_window", errors, "interaction.dual_window 必须是正数")


def _camera_speaker_enabled(config: dict[str, Any]) -> bool:
    return bool(_get_nested(config, "camera_speaker.enabled", False))


def _validate_emily_runtime(config: dict[str, Any], errors: list[str]) -> None:
    _validate_time_scenes(config, errors)
    _validate_scheduled_tasks(config, errors)
    _validate_quiet_hours(config, errors)
    _validate_interaction(config, errors)

    if _camera_speaker_enabled(config):
        _validate_camera_speaker(config, errors)
    else:
        _validate_local_audio_playback(errors)


def validate_config(config: dict[str, Any], command_name: str) -> list[str]:
    errors: list[str] = []

    if command_name in {"test_tts", "test_speaker", "test_full", "test_conversation", "emily"}:
        _validate_tts(config, errors)

    if command_name in {"test_full", "test_conversation", "emily"}:
        _validate_ai(config, errors)
        _validate_openclaw_if_enabled(config, errors)

    if command_name in {"test_ezviz", "emily"}:
        _validate_ezviz(config, errors)

    if command_name in {"test_tts", "test_asr"}:
        _validate_local_audio_playback(errors)

    if command_name == "test_speaker":
        _validate_camera_speaker(config, errors)

    if command_name == "test_full":
        if _camera_speaker_enabled(config):
            _validate_camera_speaker(config, errors)
        else:
            _validate_local_audio_playback(errors)
        _validate_time_scenes(config, errors)
        _validate_scheduled_tasks(config, errors)

    if command_name == "test_conversation":
        _validate_local_audio_playback(errors)
        _validate_time_scenes(config, errors)

    if command_name == "emily":
        _validate_emily_runtime(config, errors)

    return errors


def load_and_validate_config(command_name: str, config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path).resolve() if config_path else _default_config_path()
    config = _read_yaml(path)
    errors = validate_config(config, command_name)
    if errors:
        rendered_errors = "\n".join(f"- {item}" for item in errors)
        raise ConfigError(
            f"配置文件校验失败: {path}\n{rendered_errors}\n"
            "请根据 `config.example.yaml` 补齐或修正配置后再运行。"
        )
    return config
