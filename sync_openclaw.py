#!/usr/bin/env python3
"""
sync_openclaw.py — Emily Agent 人设文件 + API 网关一键同步到 OpenClaw 服务器

用法:
    python sync_openclaw.py              # 同步有变化的文件
    python sync_openclaw.py --dry-run    # 只检查差异，不传输
    python sync_openclaw.py --force      # 强制全部重传
    python sync_openclaw.py --diff SOUL.md       # 查看指定人设文件的远程差异
    python sync_openclaw.py --diff emily-api.py  # 查看 API 网关的远程差异
    python sync_openclaw.py --host YOUR_SERVER_IP  # 指定服务器地址

同步范围:
    - *.md 人设文件 → /root/.openclaw/workspace-emily/
    - emily-api.py  → /root/emily-api.py (API 网关，同步后需重启服务)

前置条件:
    1. SSH 已配置 openclaw 别名，或使用 --host 指定服务器地址
       在 ~/.ssh/config 中添加:
         Host openclaw
             HostName YOUR_SERVER_IP
             User root
    2. 本地 openclaw-emily-config/ 目录下（或 examples/openclaw-config/）有要同步的文件
"""

import subprocess
import os
import sys
import hashlib
import argparse
from datetime import datetime

# Windows 控制台默认 GBK，强制 UTF-8 输出
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.system("")  # 启用 ANSI 转义序列 (Windows 10+)


# === 配置 ===
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, "openclaw-emily-config")
# 如果 openclaw-emily-config/ 不存在，尝试 examples/openclaw-config/
if not os.path.isdir(LOCAL_DIR):
    LOCAL_DIR = os.path.join(HERE, "examples", "openclaw-config")

REMOTE_HOST = os.environ.get("OPENCLAW_HOST", "openclaw")  # SSH 别名或 IP
REMOTE_DIR = os.environ.get("OPENCLAW_REMOTE_DIR", "/root/.openclaw/workspace-emily")
SYNC_FILES = ["SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"]

# emily-api.py 单独管理（远程路径不同于 .md 文件）
API_SCRIPT = "emily-api.py"
API_REMOTE_PATH = os.environ.get("OPENCLAW_API_PATH", "/root/emily-api.py")

# 颜色输出
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"


def log(icon, msg, color=""):
    print(f"  {color}{icon}{RESET} {msg}")


def file_md5(path):
    """计算本地文件 MD5"""
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def remote_md5(host, path):
    """计算远程文件 MD5"""
    try:
        r = subprocess.run(
            ["ssh", host, f"md5sum {path}"],
            capture_output=True, timeout=10
        )
        if r.returncode == 0:
            stdout = r.stdout.decode("utf-8", errors="replace")
            return stdout.strip().split()[0]
    except subprocess.TimeoutExpired:
        pass
    return None


def check_ssh():
    """检查 SSH 连通性"""
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", REMOTE_HOST, "echo ok"],
            capture_output=True, timeout=10
        )
        stdout = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
        return r.returncode == 0 and "ok" in stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def compare_files():
    """对比本地和远程文件，返回 (unchanged, changed, missing_local)"""
    unchanged = []
    changed = []
    missing_local = []

    # 1. 对比 .md 人设文件
    for f in SYNC_FILES:
        local_path = os.path.join(LOCAL_DIR, f)
        remote_path = f"{REMOTE_DIR}/{f}"

        if not os.path.exists(local_path):
            missing_local.append(f)
            continue

        local_hash = file_md5(local_path)
        remote_hash = remote_md5(REMOTE_HOST, remote_path)

        if local_hash == remote_hash:
            unchanged.append((f, local_hash))
        else:
            changed.append((f, local_hash, remote_hash))

    # 2. 对比 emily-api.py（远程路径不同）
    api_local = os.path.join(LOCAL_DIR, API_SCRIPT)
    if os.path.exists(api_local):
        local_hash = file_md5(api_local)
        remote_hash = remote_md5(REMOTE_HOST, API_REMOTE_PATH)
        if local_hash == remote_hash:
            unchanged.append((API_SCRIPT, local_hash))
        else:
            changed.append((API_SCRIPT, local_hash, remote_hash))
    else:
        missing_local.append(API_SCRIPT)

    return unchanged, changed, missing_local


def sync_file(filename):
    """同步单个文件到远程服务器"""
    local_path = os.path.join(LOCAL_DIR, filename)

    # emily-api.py 使用不同的远程路径
    if filename == API_SCRIPT:
        remote_path = API_REMOTE_PATH
    else:
        remote_path = f"{REMOTE_DIR}/{filename}"

    # 1. 备份远程旧文件
    subprocess.run(
        ["ssh", REMOTE_HOST, f"cp {remote_path} {remote_path}.bak 2>/dev/null; true"],
        capture_output=True, timeout=10
    )

    # 2. 通过 SSH stdin 管道传输文件内容
    with open(local_path, "rb") as fp:
        content = fp.read()

    result = subprocess.run(
        ["ssh", REMOTE_HOST, f"cat > {remote_path}"],
        input=content, capture_output=True, timeout=30
    )

    if result.returncode != 0:
        err_msg = result.stderr.decode("utf-8", errors="replace") if result.stderr else "unknown"
        return False, err_msg

    # 3. 验证传输完整性
    remote_hash = remote_md5(REMOTE_HOST, remote_path)
    local_hash = file_md5(local_path)

    if remote_hash == local_hash:
        return True, None
    else:
        return False, f"MD5 不匹配: 本地={local_hash[:8]} 远程={remote_hash[:8]}"


def show_diff(filename):
    """显示本地和远程文件的差异"""
    local_path = os.path.join(LOCAL_DIR, filename)
    if not os.path.exists(local_path):
        print(f"{RED}本地文件不存在: {local_path}{RESET}")
        return

    # emily-api.py 使用不同的远程路径
    if filename == API_SCRIPT:
        remote_file_path = API_REMOTE_PATH
    else:
        remote_file_path = f"{REMOTE_DIR}/{filename}"

    # 获取远程文件内容 (用 bytes 模式避免 GBK 问题)
    r = subprocess.run(
        ["ssh", REMOTE_HOST, f"cat {remote_file_path}"],
        capture_output=True, timeout=10
    )
    if r.returncode != 0:
        print(f"{RED}远程文件不存在或读取失败{RESET}")
        return

    remote_content = r.stdout.decode("utf-8", errors="replace")

    # 用 Python difflib 对比 (跨平台兼容，不依赖外部 diff 命令)
    with open(local_path, 'r', encoding='utf-8') as f:
        local_lines = f.readlines()
    remote_lines = remote_content.splitlines(keepends=True)

    import difflib
    diff = list(difflib.unified_diff(
        remote_lines, local_lines,
        fromfile=f"远程 {REMOTE_DIR}/{filename}",
        tofile=f"本地 {LOCAL_DIR}/{filename}",
        lineterm=""
    ))
    if diff:
        print(f"{BOLD}--- 远程 (OPENCLAW){RESET}")
        print(f"{BOLD}+++ 本地 (DEV){RESET}")
        for line in diff:
            if line.startswith("+") and not line.startswith("+++"):
                print(f"{GREEN}{line}{RESET}")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"{RED}{line}{RESET}")
            else:
                print(line)
    else:
        print(f"{GREEN}文件完全一致{RESET}")


def main():
    global REMOTE_HOST
    parser = argparse.ArgumentParser(description="Emily Agent 人设文件同步到 OpenClaw 服务器")
    parser.add_argument("--dry-run", action="store_true", help="只检查差异，不传输")
    parser.add_argument("--force", action="store_true", help="强制全部重传")
    parser.add_argument("--diff", metavar="FILE", help="查看指定文件的远程差异")
    parser.add_argument("--host", metavar="HOST", help="OpenClaw 服务器 SSH 别名或 IP（默认读环境变量 OPENCLAW_HOST 或 'openclaw'）")
    args = parser.parse_args()

    if args.host:
        REMOTE_HOST = args.host

    print(f"\n{BOLD}{'='*50}{RESET}")
    print(f"{BOLD}  Emily Agent 人设同步工具{RESET}")
    print(f"{BOLD}{'='*50}{RESET}")
    print(f"  本地: {LOCAL_DIR}")
    print(f"  远程: {REMOTE_HOST}:{REMOTE_DIR}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 检查本地目录
    if not os.path.isdir(LOCAL_DIR):
        print(f"{RED}错误: 本地目录不存在: {LOCAL_DIR}{RESET}")
        print(f"{YELLOW}提示: 请先创建 openclaw-emily-config/ 目录或 examples/openclaw-config/ 目录{RESET}")
        sys.exit(1)

    # 如果是 diff 模式
    if args.diff:
        print(f"  对比文件: {args.diff}")
        print()
        show_diff(args.diff)
        return

    # 检查 SSH 连通性
    print("  检查 SSH 连接...", end="", flush=True)
    if not check_ssh():
        print(f" {RED}失败{RESET}")
        print(f"\n{RED}无法连接到 {REMOTE_HOST}，请检查:{RESET}")
        print(f"  1. SSH config 是否配置了 {REMOTE_HOST} 别名")
        print(f"  2. 服务器是否可达")
        print(f"  3. SSH 密钥是否已授权")
        sys.exit(1)
    print(f" {GREEN}OK{RESET}")
    print()

    # 对比文件
    print(f"{BOLD}  文件状态:{RESET}")
    unchanged, changed, missing = compare_files()

    for f, h in unchanged:
        if args.force:
            changed.append((f, h, h))
        else:
            log("✓", f"{f} (一致)", GREEN)

    for f, lh, rh in changed:
        rh_display = (rh or "不存在")[:8]
        if not args.force or lh == rh:
            log("✗", f"{f} (本地: {lh[:8]}... 远程: {rh_display}...)", YELLOW)
        else:
            log("⟳", f"{f} (强制重传)", CYAN)

    for f in missing:
        log("⊘", f"{f} (本地不存在，跳过)", RED)

    print()

    if not changed:
        print(f"  {GREEN}所有文件已是最新，无需同步。{RESET}\n")
        return

    if args.dry_run:
        print(f"  {YELLOW}[DRY RUN] 将同步 {len(changed)} 个文件，实际未执行。{RESET}\n")
        return

    # 执行同步
    print(f"{BOLD}  开始同步 ({len(changed)} 个文件):{RESET}")
    success_count = 0
    for f, lh, rh in changed:
        ok, err = sync_file(f)
        if ok:
            log("✓", f"{f} 已同步", GREEN)
            success_count += 1
        else:
            log("✗", f"{f} 同步失败: {err}", RED)

    print()
    if success_count == len(changed):
        print(f"  {GREEN}✅ 全部 {success_count} 个文件同步成功！{RESET}")
    else:
        print(f"  {YELLOW}⚠ 成功 {success_count}/{len(changed)}，部分失败。{RESET}")
        sys.exit(1)

    # 检查 emily-api.py 是否被同步，提示需要重启
    synced_files = [f for f, _, _ in changed]
    if API_SCRIPT in synced_files:
        print()
        print(f"  {YELLOW}⚠ emily-api.py 已更新，需要重启 API 服务才能生效！{RESET}")
        print(f"  {CYAN}  重启命令 (通过 SSH):{RESET}")
        print(f"  {CYAN}  kill $(pgrep -f emily-api.py) && sleep 2 && nohup python3 /root/emily-api.py > /root/emily-api.log 2>&1 &{RESET}")
        print(f"  {CYAN}  验证: curl http://localhost:8901/api/emily/health{RESET}")
    print()


if __name__ == "__main__":
    main()
