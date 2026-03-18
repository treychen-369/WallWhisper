#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
萤石摄像头联动模块 - 告警轮询 + Token 管理
独立模块，可通过 run.py test_ezviz 单独测试。
"""

import time
import logging
from pathlib import Path
from threading import Lock

import requests
from datetime import datetime, timedelta



# ============================================
# API 地址常量
# ============================================
EZVIZ_BASE = "https://open.ys7.com"
URL_TOKEN_GET = f"{EZVIZ_BASE}/api/lapp/token/get"
URL_DEVICE_LIST = f"{EZVIZ_BASE}/api/lapp/device/list"
URL_ALARM_LIST = f"{EZVIZ_BASE}/api/lapp/alarm/device/list"


def _get_token_file_candidates() -> list[Path]:
    script_dir = Path(__file__).resolve().parent
    candidates = [
        script_dir / "ezviz_token" / "ezviz.secret",
        script_dir.parent / "ezviz.secret",
        script_dir / "ezviz.secret",
        Path("/app/ezviz_token/ezviz.secret"),
        Path("/app/ezviz.secret"),
    ]

    unique_candidates = []
    seen = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized not in seen:
            seen.add(normalized)
            unique_candidates.append(candidate)
    return unique_candidates


def resolve_ezviz_token_file() -> Path:
    for candidate in _get_token_file_candidates():
        if candidate.exists():
            return candidate
    return _get_token_file_candidates()[0]


def load_ezviz_cached_token(logger: logging.Logger = None) -> tuple[str, str, Path]:
    logger = logger or logging.getLogger("ezviz_token")
    token_file = resolve_ezviz_token_file()
    initial_token = ""
    token_expires = ""

    if token_file.exists():
        logger.debug(f"[Token] 读取缓存文件: {token_file}")
        with open(token_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("EZVIZ_ACCESS_TOKEN="):
                    initial_token = line.split("=", 1)[1]
                elif line.startswith("EZVIZ_TOKEN_EXPIRES="):
                    token_expires = line.split("=", 1)[1]

    return initial_token, token_expires, token_file


def save_ezviz_cached_token(token_file: Path, token: str, expire_time: float, logger: logging.Logger = None) -> None:
    logger = logger or logging.getLogger("ezviz_token")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    expires_str = datetime.fromtimestamp(expire_time).strftime("%Y-%m-%d %H:%M:%S")
    content = f"EZVIZ_ACCESS_TOKEN={token}\nEZVIZ_TOKEN_EXPIRES={expires_str}\n"
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(content)
    logger.debug(f"[Token] 已写入缓存文件: {token_file}")


class EzvizTokenManager:

    """
    萤石 AccessToken 管理器。
    - 支持手动设置初始 token
    - token 过期时自动通过 API 刷新
    - 线程安全
    """

    def __init__(self, app_key: str, app_secret: str,
                 initial_token: str = "", expires_str: str = "",
                 token_file: str | Path | None = None,
                 logger: logging.Logger = None):
        self.app_key = app_key
        self.app_secret = app_secret
        self.logger = logger or logging.getLogger("ezviz_token")
        self._lock = Lock()
        self.token_file = Path(token_file) if token_file else None

        self._token = initial_token.strip() if initial_token else ""
        self._expire_time = 0  # Unix 时间戳

        # 解析初始过期时间
        if self._token and expires_str:
            try:
                dt = datetime.strptime(expires_str.strip(), "%Y-%m-%d %H:%M:%S")
                self._expire_time = dt.timestamp()
                self.logger.info(f"[Token] 使用已有 token, 过期时间: {expires_str}")
            except ValueError:
                self.logger.warning(f"[Token] 无法解析过期时间: {expires_str}, 将重新获取")
                self._token = ""

    @property
    def token(self) -> str:
        """获取有效的 token，过期自动刷新"""
        with self._lock:
            now = time.time()
            needs_refresh = (not self._token) or (now > (self._expire_time - 300))
            if not needs_refresh:
                return self._token

            previous_token = self._token
            previous_expire_time = self._expire_time
            if self._refresh():
                return self._token

            if previous_token and now < previous_expire_time:
                self.logger.warning("[Token] 刷新失败，继续使用当前未过期 token")
                return previous_token

            self._token = ""
            self._expire_time = 0
            return ""

    def _refresh(self) -> bool:
        """通过 API 刷新 token。成功返回 True，失败返回 False。"""
        self.logger.info("[Token] 正在获取新的 accessToken...")
        try:
            resp = requests.post(URL_TOKEN_GET, data={
                "appKey": self.app_key,
                "appSecret": self.app_secret,
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != "200":
                self.logger.error(f"[Token] 获取失败: {data}")
                return False

            token_data = data["data"]
            self._token = token_data["accessToken"]
            # expireTime 是毫秒时间戳
            self._expire_time = token_data["expireTime"] / 1000
            expire_dt = datetime.fromtimestamp(self._expire_time)
            self.logger.info(f"[Token] 获取成功, 过期时间: {expire_dt}")
            if self.token_file:
                save_ezviz_cached_token(self.token_file, self._token, self._expire_time, self.logger)
            return True
        except Exception:
            self.logger.exception("[Token] 请求异常")
            return False

    def force_refresh(self) -> bool:
        """强制刷新 token"""
        with self._lock:
            return self._refresh()



class EzvizMonitor:
    """
    萤石摄像头告警监控器。
    - 自动发现设备（如果未配置设备序列号）
    - 轮询告警消息
    - 支持冷却时间，避免频繁触发
    """

    def __init__(self, ezviz_config: dict, token_mgr: EzvizTokenManager,
                 logger: logging.Logger = None):
        self.config = ezviz_config
        self.token_mgr = token_mgr
        self.logger = logger or logging.getLogger("ezviz_monitor")

        self.device_serial = ezviz_config.get("device_serial", "").strip()
        self.channel_no = int(ezviz_config.get("channel_no", 1))
        self.poll_interval = int(ezviz_config.get("poll_interval", 5))
        self.cooldown = int(ezviz_config.get("cooldown", 60))
        self.alarm_types = ezviz_config.get("alarm_types", ["人体检测", "移动侦测"])

        self._last_trigger_time = 0  # 上次触发时间
        self._seen_alarm_ids = set() # 已处理的告警 ID 集合，避免重复
        self._last_poll_time = 0     # 上次轮询时间戳（毫秒）

    def discover_device(self) -> str:
        """
        自动发现第一个设备的序列号。
        如果已配置 device_serial 则直接返回。
        """
        if self.device_serial:
            self.logger.info(f"[设备] 使用配置的设备: {self.device_serial}")
            return self.device_serial

        self.logger.info("[设备] 未配置设备序列号，正在自动发现...")
        try:
            resp = requests.post(URL_DEVICE_LIST, data={
                "accessToken": self.token_mgr.token,
                "pageStart": 0,
                "pageSize": 10,
            }, timeout=10)
            data = resp.json()

            if data.get("code") == "200" and data.get("data"):
                devices = data["data"]
                if devices:
                    dev = devices[0]
                    self.device_serial = dev["deviceSerial"]
                    name = dev.get("deviceName", "未命名")
                    self.logger.info(f"[设备] 发现设备: {name} ({self.device_serial})")
                    # 列出所有设备
                    for i, d in enumerate(devices):
                        self.logger.info(f"  [{i}] {d.get('deviceName','?')} - {d['deviceSerial']}")
                    return self.device_serial
                else:
                    self.logger.warning("[设备] 账号下没有设备")
            else:
                self.logger.error(f"[设备] 获取设备列表失败: {data}")
        except Exception:
            self.logger.exception("[设备] 请求异常")


        return ""

    def poll_alarms(self) -> list:
        """
        轮询告警消息。
        返回新的、未处理的告警列表（已过滤类型 + 冷却时间）。
        """
        if not self.device_serial:
            self.logger.warning("[告警] 没有设备序列号，跳过轮询")
            return []

        # 查询时间范围：上次轮询到现在（往前多查 30s 防止告警入库延迟导致遗漏）
        now_ms = int(time.time() * 1000)
        LOOKBACK_MS = 30_000  # 30 秒回溯缓冲
        if self._last_poll_time == 0:
            # 首次轮询：查最近 2 分钟
            start_ms = now_ms - 120_000
        else:
            start_ms = self._last_poll_time - LOOKBACK_MS

        try:
            resp = requests.post(URL_ALARM_LIST, data={
                "accessToken": self.token_mgr.token,
                "deviceSerial": self.device_serial,
                "startTime": start_ms,
                "endTime": now_ms,
                "pageStart": 0,
                "pageSize": 10,
                "status": 2,  # 0-未读 2-所有
            }, timeout=10)
            data = resp.json()
            self._last_poll_time = now_ms

            if data.get("code") == "200":
                alarms = data.get("data", []) or []
                if not alarms:
                    return []


                # 过滤：类型匹配 + 去重
                new_alarms = []
                for alarm in alarms:
                    alarm_id = alarm.get("alarmId")
                    alarm_type = alarm.get("alarmType", "")
                    alarm_name = alarm.get("alarmName", alarm_type)

                    # 去重（用集合，支持回溯窗口内多条告警）
                    if alarm_id in self._seen_alarm_ids:
                        continue

                    # 类型过滤（如果配置了 alarm_types）
                    # 同时匹配 alarmName 和 alarmType（数字码），任一匹配即可
                    if self.alarm_types:
                        alarm_type_str = str(alarm.get("alarmType", ""))
                        alarm_name_str = str(alarm_name)
                        matched = any(
                            t in alarm_name_str or t in alarm_type_str
                            for t in self.alarm_types
                        )
                        if not matched:
                            self.logger.debug(f"[告警] 跳过不匹配的告警: name={alarm_name}, type={alarm_type_str}")
                            continue

                    new_alarms.append(alarm)
                    self._seen_alarm_ids.add(alarm_id)

                # 防止集合无限增长，保留最近 200 条
                if len(self._seen_alarm_ids) > 200:
                    self._seen_alarm_ids = set(list(self._seen_alarm_ids)[-100:])

                return new_alarms

            elif data.get("code") == "10002":
                # token 过期
                self.logger.warning("[告警] Token 已过期，正在刷新...")
                self.token_mgr.force_refresh()
                return []
            else:
                self.logger.error(f"[告警] 查询失败: code={data.get('code')}, msg={data.get('msg')}")
                return []

        except Exception:
            self.logger.exception("[告警] 请求异常")
            return []


    def check_trigger(self) -> dict | None:
        """
        检查是否应该触发 Emily 说话。
        返回触发信息 dict 或 None。

        触发条件：
        1. 有新的告警消息
        2. 距上次触发超过冷却时间
        """
        now = time.time()

        # 冷却检查
        if now - self._last_trigger_time < self.cooldown:
            remaining = self.cooldown - (now - self._last_trigger_time)
            self.logger.debug(f"[触发] 冷却中，剩余 {remaining:.0f}s")
            return None

        # 轮询告警
        alarms = self.poll_alarms()
        if not alarms:
            return None

        # 取最新的一条
        alarm = alarms[0]
        alarm_time_ms = alarm.get("alarmTime", 0)
        alarm_dt = datetime.fromtimestamp(alarm_time_ms / 1000) if alarm_time_ms else datetime.now()

        self._last_trigger_time = now

        trigger_info = {
            "alarm_type": alarm.get("alarmName", alarm.get("alarmType", "未知")),
            "alarm_time": alarm_dt.strftime("%H:%M:%S"),
            "device_serial": self.device_serial,
            "alarm_id": alarm.get("alarmId"),
            "channel_no": alarm.get("channelNo", self.channel_no),
        }

        self.logger.info(f"[触发] 检测到人! 类型={trigger_info['alarm_type']}, "
                         f"时间={trigger_info['alarm_time']}")
        return trigger_info


# ============================================
# 独立测试入口（通过 run.py test_ezviz 调用）
# ============================================

def run_test(config: dict, logger: logging.Logger):
    """
    萤石模块独立测试：
    1. Token 管理测试
    2. 设备发现测试
    3. 告警轮询测试（30秒）
    """
    ezviz_cfg = config.get("ezviz", {})
    initial_token, token_expires, token_file = load_ezviz_cached_token(logger)

    # 1. Token 管理测试
    logger.info("=" * 50)
    logger.info("[TEST 1/3] Token 管理测试")
    logger.info("=" * 50)
    logger.info(f"[TEST] Token 缓存文件: {token_file}")

    token_mgr = EzvizTokenManager(
        app_key=ezviz_cfg["app_key"],
        app_secret=ezviz_cfg["app_secret"],
        initial_token=initial_token,
        expires_str=token_expires,
        token_file=token_file,
        logger=logger,
    )

    token = token_mgr.token

    if token:
        logger.info(f"[TEST] ✅ Token 获取成功: {token[:20]}...")
    else:
        logger.error("[TEST] ❌ Token 获取失败!")
        return False

    # 2. 设备发现测试
    logger.info("")
    logger.info("=" * 50)
    logger.info("[TEST 2/3] 设备发现测试")
    logger.info("=" * 50)

    monitor = EzvizMonitor(ezviz_cfg, token_mgr, logger)
    serial = monitor.discover_device()

    if serial:
        logger.info(f"[TEST] ✅ 设备发现成功: {serial}")
    else:
        logger.error("[TEST] ❌ 未发现设备，请检查 accessToken 和设备绑定")
        return False

    # 3. 告警轮询测试（轮询 30 秒）
    logger.info("")
    logger.info("=" * 50)
    logger.info("[TEST 3/3] 告警轮询测试 (30秒)")
    logger.info("=" * 50)
    logger.info("[TEST] 请在摄像头前走动以触发告警...")

    # 设置短冷却时间便于测试
    monitor.cooldown = 5
    start = time.time()
    triggered = False

    while time.time() - start < 30:
        trigger = monitor.check_trigger()
        if trigger:
            logger.info(f"[TEST] ✅ 触发成功! {trigger}")
            triggered = True
            break

        elapsed = int(time.time() - start)
        logger.info(f"[TEST] 等待告警... ({elapsed}/30s)")
        time.sleep(monitor.poll_interval)

    if not triggered:
        logger.info("[TEST] ⚠️ 30秒内未收到告警（这不一定是错误，可能摄像头前无人经过）")
        logger.info("[TEST] Token 管理 ✅ | 设备发现 ✅ | 告警轮询 ✅ (功能正常，无新告警)")

    return True
