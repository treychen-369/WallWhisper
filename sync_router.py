#!/usr/bin/env python3
"""
Emily 路由器配置安全同步工具
==============================
在本地 PC 上运行，安全地将配置文件同步到路由器。

安全特性:
  1. 传输前：校验本地文件格式 (YAML 语法 + 必要配置段)
  2. 传输前：检查路由器连通性和 Emily 容器状态
  3. 传输时：先传到临时文件，验证完整性后才替换
  4. 传输后：自动备份旧配置 (带时间戳)
  5. 传输后：MD5 校验确认一致
  6. 可选：--restart 同步后安全重启容器
  7. 可选：--diff 只查看本地和远程的差异，不做任何更改
  8. 可选：--dry-run 模拟执行，不实际传输

用法:
  python sync_router.py config                     # 同步 config.docker.yaml
  python sync_router.py deploy                     # 同步 deploy.sh
  python sync_router.py config --diff              # 查看配置差异
  python sync_router.py config --restart           # 同步配置并重启容器
  python sync_router.py config --dry-run           # 模拟同步
  python sync_router.py status                     # 查看路由器 Emily 状态
  python sync_router.py --router-host MY_ROUTER    # 指定路由器 SSH 别名

环境变量:
  EMILY_ROUTER_HOST   路由器 SSH 别名（默认: router）
  EMILY_ROUTER_DIR    Emily 在路由器上的目录（默认: /opt/emily）
  EMILY_DOCKER_CMD    Docker 命令路径（默认: docker）
"""

import subprocess
import sys
import os
import hashlib
import argparse
import difflib

# Windows 控制台默认 GBK，强制 UTF-8 输出
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.system("")  # 启用 ANSI 转义序列 (Windows 10+)

# ── 配置（通过环境变量或命令行参数覆盖）──
HERE = os.path.dirname(os.path.abspath(__file__))
ROUTER_HOST = os.environ.get("EMILY_ROUTER_HOST", "router")
EMILY_DIR = os.environ.get("EMILY_ROUTER_DIR", "/opt/emily")
DOCKER_CMD = os.environ.get("EMILY_DOCKER_CMD", "docker")
CONTAINER_NAME = os.environ.get("EMILY_CONTAINER", "wallwhisper")

# 允许同步的文件白名单 (防止误操作)
SYNC_FILES = {
    "config": {
        "local": os.path.join(HERE, "config.docker.yaml"),
        "remote": f"{EMILY_DIR}/config.docker.yaml",
        "description": "Emily Docker 配置文件 (含密钥)",
        "validate": True,  # 需要 YAML 格式校验
    },
    "deploy": {
        "local": os.path.join(HERE, "deploy.sh"),
        "remote": f"{EMILY_DIR}/deploy.sh",
        "description": "Emily 部署脚本",
        "validate": False,
    },
}


class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


def info(msg):
    print(f"{Colors.CYAN}[INFO]{Colors.END}  {msg}")

def ok(msg):
    print(f"{Colors.GREEN}[OK]{Colors.END}    {msg}")

def warn(msg):
    print(f"{Colors.YELLOW}[WARN]{Colors.END}  {msg}")

def error(msg):
    print(f"{Colors.RED}[ERROR]{Colors.END} {msg}")

def step(n, total, msg):
    print(f"\n{Colors.BOLD}━━━ [{n}/{total}] {msg} ━━━{Colors.END}")


def file_md5(path):
    """计算本地文件 MD5"""
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def ssh_run(cmd, capture=True, check=False):
    """在路由器上执行命令 (Windows 兼容: 用 bytes 模式避免 GBK 解码问题)"""
    full_cmd = ["ssh", ROUTER_HOST, cmd]
    if capture:
        result = subprocess.run(
            full_cmd,
            capture_output=True,
            timeout=30,
        )
        # 手动用 UTF-8 解码, 路由器输出是 UTF-8
        result.stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
        result.stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    else:
        result = subprocess.run(full_cmd, timeout=30)
    if check and result.returncode != 0:
        raise RuntimeError(f"SSH 命令失败: {cmd}\n{result.stderr}")
    return result


def check_connectivity():
    """检查路由器连通性"""
    try:
        result = ssh_run("echo ok", capture=True)
        return result.returncode == 0 and "ok" in result.stdout
    except Exception:
        return False


def get_remote_md5(remote_path):
    """获取远程文件 MD5"""
    result = ssh_run(f"md5sum {remote_path} 2>/dev/null")
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().split()[0]
    return None


def get_remote_content(remote_path):
    """获取远程文件内容"""
    result = ssh_run(f"cat {remote_path} 2>/dev/null")
    if result.returncode == 0:
        return result.stdout
    return None


def get_container_status():
    """获取 Emily 容器状态"""
    result = ssh_run(f"{DOCKER_CMD} inspect --format='{{{{.State.Status}}}}' {CONTAINER_NAME} 2>/dev/null")
    if result.returncode == 0:
        return result.stdout.strip().strip("'")
    return "not_found"


def get_router_health():
    """获取路由器健康状态"""
    result = ssh_run("cat /proc/meminfo | grep MemAvailable | awk '{print $2}'")
    mem = result.stdout.strip() if result.returncode == 0 else "unknown"
    result = ssh_run("cat /proc/loadavg | awk '{print $1}'")
    load = result.stdout.strip() if result.returncode == 0 else "unknown"
    return mem, load


def validate_config_yaml(path):
    """校验 config.docker.yaml 格式"""
    errors = []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 必须包含的配置段
    required_sections = ["ai:", "tts:", "ezviz:"]
    for section in required_sections:
        if section not in content:
            errors.append(f"缺少配置段: {section}")

    # 不能包含明显的占位符
    placeholders = ["your-", "xxxxxxx", "sk-xxxx"]
    for ph in placeholders:
        if ph in content:
            errors.append(f"疑似占位符未替换: '{ph}'")

    # 文件大小合理性检查 (config 通常 1-10KB)
    size = os.path.getsize(path)
    if size < 200:
        errors.append(f"文件过小 ({size} bytes)，可能不完整")
    if size > 50000:
        errors.append(f"文件过大 ({size} bytes)，可能有误")

    # 尝试 YAML 解析 (如果有 pyyaml)
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            errors.append("YAML 解析结果不是字典")
        elif "ai" not in data or "tts" not in data:
            errors.append("YAML 解析后缺少 ai 或 tts 配置")
    except ImportError:
        pass  # pyyaml 不是必须的
    except Exception as e:
        errors.append(f"YAML 语法错误: {e}")

    return errors


def do_diff(file_key):
    """显示本地和远程文件差异"""
    file_info = SYNC_FILES[file_key]

    if not os.path.exists(file_info["local"]):
        error(f"本地文件不存在: {file_info['local']}")
        return

    info(f"获取远程文件: {file_info['remote']}")
    remote_content = get_remote_content(file_info["remote"])
    if remote_content is None:
        warn("远程文件不存在 (首次同步?)")
        return

    with open(file_info["local"], "r", encoding="utf-8") as f:
        local_content = f.read()

    local_lines = local_content.splitlines(keepends=True)
    remote_lines = remote_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        remote_lines, local_lines,
        fromfile=f"ROUTER:{file_info['remote']}",
        tofile=f"DEV:{file_info['local']}",
        lineterm="",
    ))

    if not diff:
        ok("文件完全一致，无需同步。")
    else:
        print("\n" + "".join(diff))
        info(f"共 {len([l for l in diff if l.startswith('+') and not l.startswith('+++')])} 行新增, "
             f"{len([l for l in diff if l.startswith('-') and not l.startswith('---')])} 行删除")


def do_status():
    """查看路由器 Emily 状态"""
    step(1, 3, "路由器连通性")
    if not check_connectivity():
        error("无法连接路由器！请确认在家庭网络中。")
        return
    ok("路由器连接正常")

    step(2, 3, "Emily 容器状态")
    status = get_container_status()
    if status == "running":
        ok(f"容器状态: {status}")
        # 资源使用
        result = ssh_run(f"{DOCKER_CMD} stats --no-stream --format '内存: {{{{.MemUsage}}}} | CPU: {{{{.CPUPerc}}}} | 进程: {{{{.PIDs}}}}' {CONTAINER_NAME}")
        if result.returncode == 0:
            info(f"资源: {result.stdout.strip()}")
        # 最近日志
        result = ssh_run(f"{DOCKER_CMD} logs --tail 5 {CONTAINER_NAME} 2>&1")
        if result.returncode == 0 and result.stdout.strip():
            info("最近日志:")
            for line in result.stdout.strip().split("\n"):
                print(f"    {line}")
    elif status == "not_found":
        warn("Emily 容器不存在")
    else:
        warn(f"容器状态异常: {status}")

    step(3, 3, "路由器健康")
    mem, load = get_router_health()
    info(f"可用内存: {mem} kB")
    info(f"系统负载: {load}")
    if mem != "unknown" and int(mem) < 102400:
        warn(f"可用内存低于 100MB！当前: {mem} kB")


def do_sync(file_key, dry_run=False, restart=False):
    """安全同步文件到路由器"""
    file_info = SYNC_FILES[file_key]
    total_steps = 6 if restart else 5

    print(f"\n{'=' * 50}")
    print(f"  同步: {file_info['description']}")
    print(f"  方向: DEV → ROUTER")
    if dry_run:
        print(f"  模式: DRY RUN (仅模拟)")
    print(f"{'=' * 50}")

    # ── Step 1: 本地文件检查 ──
    step(1, total_steps, "本地文件检查")

    if not os.path.exists(file_info["local"]):
        error(f"本地文件不存在: {file_info['local']}")
        sys.exit(1)
    ok(f"文件存在: {os.path.basename(file_info['local'])}")

    local_size = os.path.getsize(file_info["local"])
    local_hash = file_md5(file_info["local"])
    info(f"大小: {local_size} bytes | MD5: {local_hash[:12]}...")

    # YAML 格式校验 (仅 config)
    if file_info.get("validate"):
        errors = validate_config_yaml(file_info["local"])
        if errors:
            for e in errors:
                error(f"校验失败: {e}")
            error("请修复上述问题后重试！")
            sys.exit(1)
        ok("YAML 格式校验通过")

    # ── Step 2: 路由器连通性 + 状态检查 ──
    step(2, total_steps, "路由器连通性检查")

    if not check_connectivity():
        error("无法连接路由器！")
        error("请确认: 1) 在家庭网络中  2) SSH 配置正确  3) 路由器正常运行")
        sys.exit(1)
    ok("SSH 连接正常")

    mem, load = get_router_health()
    info(f"可用内存: {mem} kB | 系统负载: {load}")
    if mem != "unknown" and int(mem) < 102400:
        warn("路由器可用内存偏低，请注意观察")

    container_status = get_container_status()
    info(f"Emily 容器: {container_status}")

    # ── Step 3: 远程文件对比 ──
    step(3, total_steps, "远程文件对比")

    remote_hash = get_remote_md5(file_info["remote"])
    if remote_hash == local_hash:
        ok("文件已一致，无需同步。")
        return
    elif remote_hash:
        info(f"远程 MD5: {remote_hash[:12]}... (不同，需要同步)")
    else:
        info("远程文件不存在 (首次同步)")

    if dry_run:
        info("[DRY RUN] 将执行: 备份旧文件 → 传输新文件 → MD5 验证")
        if restart:
            info("[DRY RUN] 将执行: 重启 Emily 容器")
        ok("模拟完成，未做任何更改。")
        return

    # ── Step 4: 备份 + 传输 ──
    step(4, total_steps, "备份旧文件 & 传输新文件")

    # 备份远程旧文件 (带时间戳)
    ts_result = ssh_run("date '+%Y%m%d_%H%M%S'")
    timestamp = ts_result.stdout.strip()

    backup_path = f"{file_info['remote']}.bak.{timestamp}"
    ssh_run(f"cp {file_info['remote']} {backup_path} 2>/dev/null; true")
    ok(f"旧文件已备份: {os.path.basename(backup_path)}")

    # 先传到临时文件
    tmp_remote = f"{file_info['remote']}.tmp"
    with open(file_info["local"], "rb") as f:
        result = subprocess.run(
            ["ssh", ROUTER_HOST, f"cat > {tmp_remote}"],
            input=f.read(),
            capture_output=True,
            timeout=30,
        )
    if result.returncode != 0:
        err_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown"
        error(f"传输失败: {err_msg}")
        ssh_run(f"rm -f {tmp_remote}")
        sys.exit(1)
    ok("文件已传输到临时位置")

    # ── Step 5: 验证 + 替换 ──
    step(5, total_steps, "完整性验证 & 原子替换")

    # 验证远程临时文件 MD5
    tmp_hash = get_remote_md5(tmp_remote)
    if tmp_hash != local_hash:
        error(f"MD5 不匹配！本地: {local_hash[:12]}... 远程: {(tmp_hash or 'null')[:12]}...")
        error("传输可能损坏，已放弃替换！临时文件已保留供排查。")
        sys.exit(1)
    ok(f"MD5 校验通过: {local_hash[:12]}...")

    # 原子替换: mv 是原子操作
    ssh_run(f"mv {tmp_remote} {file_info['remote']}", check=True)
    ok("文件已安全替换")

    # 最终确认
    final_hash = get_remote_md5(file_info["remote"])
    if final_hash == local_hash:
        ok("最终验证通过 ✓")
    else:
        error("最终验证失败！正在回滚...")
        ssh_run(f"cp {backup_path} {file_info['remote']}")
        error("已回滚到备份版本。请排查问题。")
        sys.exit(1)

    # 清理超过 5 个的旧备份
    ssh_run(f"ls -t {file_info['remote']}.bak.* 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null; true")

    # ── Step 6 (可选): 重启容器 ──
    if restart:
        step(6, total_steps, "安全重启 Emily 容器")

        if container_status != "running":
            warn("容器当前不在运行，跳过重启")
        else:
            info("停止容器...")
            ssh_run(f"{DOCKER_CMD} stop {CONTAINER_NAME}")
            info("启动容器...")
            ssh_run(f"{DOCKER_CMD} start {CONTAINER_NAME}")

            import time
            info("等待 10 秒...")
            time.sleep(10)

            new_status = get_container_status()
            if new_status == "running":
                ok(f"容器重启成功: {new_status}")
                # 检查日志
                result = ssh_run(f"{DOCKER_CMD} logs --tail 5 {CONTAINER_NAME} 2>&1")
                if result.returncode == 0:
                    error_count = result.stdout.lower().count("error") + result.stdout.lower().count("traceback")
                    if error_count > 0:
                        warn(f"发现 {error_count} 处错误，最近日志:")
                        print(result.stdout)
                    else:
                        ok("容器日志正常")
            else:
                error(f"容器重启失败！状态: {new_status}")
                warn("配置文件可能有问题，但已有备份。")
                warn(f"回滚命令: ssh {ROUTER_HOST} cp {backup_path} {file_info['remote']}")
                sys.exit(1)

    print(f"\n{'=' * 50}")
    ok("同步完成！")
    print(f"{'=' * 50}")


def main():
    global ROUTER_HOST

    parser = argparse.ArgumentParser(
        description="Emily 路由器配置安全同步工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python sync_router.py config              同步 config.docker.yaml 到路由器
  python sync_router.py config --diff       查看配置差异
  python sync_router.py config --restart    同步配置并重启容器
  python sync_router.py deploy              同步 deploy.sh 到路由器
  python sync_router.py deploy --diff       查看部署脚本差异
  python sync_router.py status              查看路由器 Emily 状态
  python sync_router.py config --dry-run    模拟同步（不实际执行）

环境变量:
  EMILY_ROUTER_HOST   路由器 SSH 别名（默认: router）
  EMILY_ROUTER_DIR    Emily 在路由器上的目录（默认: /opt/emily）
  EMILY_DOCKER_CMD    Docker 命令路径（默认: docker）
        """,
    )
    parser.add_argument(
        "target",
        choices=["config", "deploy", "status"],
        help="要操作的目标: config (配置文件), deploy (部署脚本), status (查看状态)",
    )
    parser.add_argument("--diff", action="store_true", help="只查看差异，不做更改")
    parser.add_argument("--dry-run", action="store_true", help="模拟执行，不实际传输")
    parser.add_argument("--restart", action="store_true", help="同步后安全重启容器")
    parser.add_argument("--router-host", metavar="HOST", help="路由器 SSH 别名（覆盖环境变量 EMILY_ROUTER_HOST）")

    args = parser.parse_args()

    if args.router_host:
        ROUTER_HOST = args.router_host

    if args.target == "status":
        do_status()
        return

    if args.diff:
        do_diff(args.target)
        return

    do_sync(args.target, dry_run=args.dry_run, restart=args.restart)


if __name__ == "__main__":
    main()
