#!/bin/sh
# WallWhisper - 家庭隐形英语外教 - 路由器安全部署脚本 v2
# 用法: sh deploy.sh [--force] [--rollback]
#
# 部署前请根据你的环境设置以下变量（或使用默认值）:
#   DOCKER_CMD   - Docker 命令路径（默认: docker）
#   EMILY_IMAGE  - WallWhisper 镜像地址（默认: wallwhisper:latest）
#   EMILY_DIR    - WallWhisper 在路由器上的目录（默认: /opt/emily）
#
# 安全特性:
#   - 部署前自动创建带时间戳的完整快照 (镜像ID + 配置hash)
#   - 6 步部署 + 4 步健康检查
#   - 可用内存 <100MB 自动拒绝部署
#   - 部署失败自动回滚到上一个好的版本
#   - --rollback 手动回滚到最近一次成功部署的镜像

set -e

# === 配置（通过环境变量覆盖） ===
DOCKER="${DOCKER_CMD:-docker}"
IMAGE="${EMILY_IMAGE:-wallwhisper:latest}"
CONTAINER_NAME="${EMILY_CONTAINER:-wallwhisper}"
EMILY_DIR="${EMILY_DIR:-/opt/emily}"
BACKUP_DIR="$EMILY_DIR/backups"
STATE_FILE="$EMILY_DIR/.last_good_deploy"
MIN_MEMORY_KB=102400  # 100MB，低于此值拒绝部署

# === 颜色输出 (兼容 ash/busybox) ===
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo "${CYAN}[INFO]${NC}  $1"; }
log_ok()    { echo "${GREEN}[OK]${NC}    $1"; }
log_warn()  { echo "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo "${RED}[ERROR]${NC} $1"; }
log_step()  { echo ""; echo "${CYAN}━━━ [$1] $2 ━━━${NC}"; }

# === 获取路由器状态 ===
get_mem_available() {
    cat /proc/meminfo | grep MemAvailable | awk '{print $2}'
}

get_load() {
    cat /proc/loadavg | awk '{print $1}'
}

get_current_image_id() {
    $DOCKER inspect --format='{{.Image}}' $CONTAINER_NAME 2>/dev/null | cut -c8-19 || echo "none"
}

# === 回滚功能 ===
do_rollback() {
    if [ ! -f "$STATE_FILE" ]; then
        log_error "没有找到上一次成功部署的记录 ($STATE_FILE)"
        log_error "无法回滚。请手动处理。"
        exit 1
    fi

    ROLLBACK_IMAGE=$(cat "$STATE_FILE" | head -1)
    log_warn "回滚到上一次成功部署的镜像: $ROLLBACK_IMAGE"

    $DOCKER stop $CONTAINER_NAME 2>/dev/null || true
    $DOCKER rm $CONTAINER_NAME 2>/dev/null || true

    # 用保存的镜像 ID 重新启动
    start_container "$ROLLBACK_IMAGE"

    sleep 8
    CONTAINER_STATUS=$($DOCKER inspect --format='{{.State.Status}}' $CONTAINER_NAME 2>/dev/null || echo "not_found")
    if [ "$CONTAINER_STATUS" = "running" ]; then
        log_ok "回滚成功！容器已恢复运行。"
    else
        log_error "回滚后容器仍未运行，需要手动排查。"
        exit 1
    fi
    exit 0
}

# === 启动容器 (复用函数) ===
start_container() {
    TARGET_IMAGE="$1"
    $DOCKER run -d \
        --name $CONTAINER_NAME \
        --network host \
        --restart unless-stopped \
        -v $EMILY_DIR/config.docker.yaml:/app/config.yaml:ro \
        -v $EMILY_DIR/ezviz_token:/app/ezviz_token \
        -v $EMILY_DIR/logs:/app/logs \
        -e TZ=Asia/Shanghai \
        -e PYTHONUNBUFFERED=1 \
        -e PYTHONIOENCODING=utf-8 \
        --memory 128m \
        --memory-reservation 64m \
        --memory-swap 192m \
        --cpus 0.5 \
        --cpu-shares 256 \
        --pids-limit 32 \
        --oom-score-adj 500 \
        "$TARGET_IMAGE"
}

# === 参数解析 ===
FORCE=0
for arg in "$@"; do
    case "$arg" in
        --force)    FORCE=1 ;;
        --rollback) do_rollback ;;
    esac
done

# === 开始部署 ===
echo ""
echo "========================================="
echo "  WallWhisper Deploy Script v2 (Safe Mode)"
echo "========================================="
echo "  Image:     $IMAGE"
echo "  Container: $CONTAINER_NAME"
echo "  Time:      $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================="

# ─── STEP 0: 部署前安全预检 ───
log_step "0/7" "Pre-deploy Safety Check"

FREE_BEFORE=$(get_mem_available)
LOAD_BEFORE=$(get_load)
log_info "可用内存: ${FREE_BEFORE} kB"
log_info "系统负载: $LOAD_BEFORE"

# 内存预检：部署前就检查，内存不够直接拒绝
if [ "$FREE_BEFORE" -lt "$MIN_MEMORY_KB" ]; then
    log_error "可用内存 ${FREE_BEFORE}kB < ${MIN_MEMORY_KB}kB (100MB)"
    log_error "路由器内存不足，拒绝部署！请先释放内存。"
    exit 1
fi

# 检查配置文件
if [ ! -f "$EMILY_DIR/config.docker.yaml" ]; then
    log_error "配置文件不存在: $EMILY_DIR/config.docker.yaml"
    log_error "请先将 config.docker.yaml 传输到路由器！"
    exit 1
fi
log_ok "配置文件: 存在"

# 检查 config.docker.yaml 基本格式 (至少包含 ai: 和 tts: 段)
if ! grep -q "^ai:" "$EMILY_DIR/config.docker.yaml" || ! grep -q "^tts:" "$EMILY_DIR/config.docker.yaml"; then
    log_error "配置文件格式异常！缺少 ai: 或 tts: 配置段"
    log_error "请检查 config.docker.yaml 是否完整。"
    exit 1
fi
log_ok "配置文件: 格式校验通过"

# ─── STEP 1: 记录当前运行状态 (用于回滚) ───
log_step "1/7" "Snapshot Current State"

mkdir -p "$BACKUP_DIR"
OLD_IMAGE_ID=$(get_current_image_id)
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

if [ "$OLD_IMAGE_ID" != "none" ]; then
    log_info "当前镜像 ID: $OLD_IMAGE_ID"
    # 保存当前镜像 ID 用于快速回滚
    echo "$OLD_IMAGE_ID" > "$BACKUP_DIR/image_$TIMESTAMP"
    log_ok "已保存回滚点: backups/image_$TIMESTAMP"
else
    log_warn "当前无运行中的容器 (首次部署?)"
fi

# 备份当前配置文件
cp "$EMILY_DIR/config.docker.yaml" "$BACKUP_DIR/config_$TIMESTAMP.yaml"
log_ok "配置备份: backups/config_$TIMESTAMP.yaml"

# 清理超过 7 天的旧备份 (路由器空间有限)
find "$BACKUP_DIR" -name "image_*" -mtime +7 -delete 2>/dev/null || true
find "$BACKUP_DIR" -name "config_*" -mtime +7 -delete 2>/dev/null || true

# ─── STEP 2: 拉取新镜像 ───
log_step "2/7" "Pull Latest Image"

log_info "正在拉取: $IMAGE"
if ! $DOCKER pull $IMAGE; then
    log_error "镜像拉取失败！网络问题？CI 构建完了吗？"
    exit 1
fi

NEW_IMAGE_ID=$($DOCKER inspect --format='{{.Id}}' $IMAGE 2>/dev/null | cut -c8-19)
log_ok "新镜像 ID: $NEW_IMAGE_ID"

# 如果镜像没变化，且不是 --force，跳过部署
if [ "$OLD_IMAGE_ID" = "$NEW_IMAGE_ID" ] && [ "$FORCE" -eq 0 ]; then
    log_warn "镜像未变化 (ID: $NEW_IMAGE_ID)，跳过部署。"
    log_info "如需强制重新部署，请使用: deploy.sh --force"
    exit 0
fi

# ─── STEP 3: 停止旧容器 ───
log_step "3/7" "Stop Old Container"

if $DOCKER inspect $CONTAINER_NAME > /dev/null 2>&1; then
    $DOCKER stop $CONTAINER_NAME 2>/dev/null || true
    $DOCKER rm $CONTAINER_NAME 2>/dev/null || true
    log_ok "旧容器已停止并移除"
else
    log_info "无旧容器需要清理"
fi

# ─── STEP 4: 启动新容器 ───
log_step "4/7" "Start New Container (Resource Limited)"

log_info "资源限制: 内存 128MB / CPU 0.5核 / 进程 32 / OOM优先级 500"
start_container "$IMAGE"
log_ok "容器已启动"

# ─── STEP 5: 容器健康检查 ───
log_step "5/7" "Container Health Check (15s)"

sleep 15
CONTAINER_STATUS=$($DOCKER inspect --format='{{.State.Status}}' $CONTAINER_NAME 2>/dev/null || echo "not_found")

if [ "$CONTAINER_STATUS" != "running" ]; then
    log_error "容器启动失败！状态: $CONTAINER_STATUS"
    echo ""
    log_info "最后 30 行日志:"
    $DOCKER logs --tail 30 $CONTAINER_NAME 2>&1
    echo ""

    # 自动回滚
    log_warn "正在自动回滚..."
    $DOCKER rm -f $CONTAINER_NAME 2>/dev/null || true

    if [ "$OLD_IMAGE_ID" != "none" ]; then
        log_info "回滚到镜像: $OLD_IMAGE_ID"
        start_container "$OLD_IMAGE_ID"
        sleep 8
        ROLLBACK_STATUS=$($DOCKER inspect --format='{{.State.Status}}' $CONTAINER_NAME 2>/dev/null || echo "failed")
        if [ "$ROLLBACK_STATUS" = "running" ]; then
            log_ok "回滚成功，WallWhisper 已恢复到上一个版本。"
        else
            log_error "回滚也失败了！需要手动处理。"
        fi
    else
        log_error "无旧镜像可回滚。请手动排查。"
    fi
    exit 1
fi

log_ok "容器状态: running"
log_info "资源使用:"
$DOCKER stats --no-stream --format "    内存: {{.MemUsage}} | CPU: {{.CPUPerc}} | 进程数: {{.PIDs}}" $CONTAINER_NAME

# 检查容器日志中是否有明显错误
ERROR_COUNT=$($DOCKER logs --tail 20 $CONTAINER_NAME 2>&1 | grep -ci "error\|exception\|traceback" || true)
if [ "$ERROR_COUNT" -gt 0 ]; then
    log_warn "容器日志中发现 $ERROR_COUNT 处错误/异常，请检查："
    $DOCKER logs --tail 10 $CONTAINER_NAME 2>&1 | grep -i "error\|exception\|traceback" || true
else
    log_ok "容器日志: 无明显错误"
fi

# ─── STEP 6: 路由器系统健康检查 ───
log_step "6/7" "Router System Health Check"

FREE_AFTER=$(get_mem_available)
LOAD_AFTER=$(get_load)
log_info "可用内存: ${FREE_AFTER} kB (部署前: ${FREE_BEFORE} kB)"
log_info "系统负载: $LOAD_AFTER (部署前: $LOAD_BEFORE)"

# 部署后内存检查
if [ "$FREE_AFTER" -lt "$MIN_MEMORY_KB" ]; then
    log_error "部署后可用内存 < 100MB！紧急停止 WallWhisper 保护路由器！"
    $DOCKER stop $CONTAINER_NAME
    $DOCKER rm $CONTAINER_NAME
    log_warn "WallWhisper 已紧急停止。路由器安全。"
    log_info "当前可用内存: $(get_mem_available) kB"
    exit 1
fi
log_ok "内存安全: ${FREE_AFTER} kB (阈值: ${MIN_MEMORY_KB} kB)"

# DNS 检查
if nslookup baidu.com 127.0.0.1 > /dev/null 2>&1 || ping -c 1 -W 2 baidu.com > /dev/null 2>&1; then
    log_ok "DNS/网络: 正常"
else
    log_warn "DNS/网络: 响应异常，请手动检查"
fi

# Wi-Fi 检查 (路由器核心功能)
WIFI_CLIENTS=$(iwinfo 2>/dev/null | grep -c "ESSID" || echo "unknown")
if [ "$WIFI_CLIENTS" != "unknown" ]; then
    log_ok "Wi-Fi 服务: 正常 ($WIFI_CLIENTS 个射频)"
else
    log_info "Wi-Fi 检查: 跳过 (iwinfo 不可用)"
fi

# ─── STEP 7: 记录成功部署状态 ───
log_step "7/7" "Record Successful Deploy"

# 保存这次成功的镜像 ID，供下次回滚使用
$DOCKER inspect --format='{{.Image}}' $CONTAINER_NAME > "$STATE_FILE"
log_ok "已记录成功部署状态"

echo ""
echo "========================================="
echo "  ${GREEN}Deploy SUCCESS!${NC}"
echo "========================================="
echo "  镜像:   $NEW_IMAGE_ID"
echo "  内存:   ${FREE_AFTER} kB 可用"
echo "  负载:   $LOAD_AFTER"
echo ""
echo "  常用命令:"
echo "    查看日志: $DOCKER logs -f $CONTAINER_NAME"
echo "    资源监控: $DOCKER stats $CONTAINER_NAME"
echo "    停止:     $DOCKER stop $CONTAINER_NAME"
echo "    回滚:     sh $EMILY_DIR/deploy.sh --rollback"
echo "========================================="
