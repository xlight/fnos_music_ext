#!/usr/bin/env bash
# scripts/host-disk-gc.sh - fnOS 容器（CT201）根分区空间回收
#
# 背景：fnOS 跑在 LXC 里，根分区只有 12G，Docker 构建缓存、fnmusic-ext 的
# 构建暂存、测试下载都落在根分区上；写满后 PostgreSQL 无法写 WAL → 一直卡在
# recovery → 应用市场/应用管理器打不开、一批服务连带报错
# （现场：2026-11-05 03:00 pg_receivewal "No space left on device"）。
#
# 用法:
#   sudo bash scripts/host-disk-gc.sh [--dry-run]
#
# 动作（都可安全重复执行）：
#   1. Docker：清理悬空镜像 + 24h 之前的构建缓存（保留最近一天的缓存，
#      免得下一次重建要重新拉层）
#   2. /tmp：清理已知 scratch 目录（fnmusic-* / patch-* / repo / opencode），
#      只动「2 天前」的，且保留 *.tar.gz / *.bak 备份
#   3. apt 缓存
#   4. 把根分区用量记到日志（日志写在数据卷，不占根分区）
#
# 安装（容器内，root）：
#   install -m 0755 scripts/host-disk-gc.sh /usr/local/sbin/fnmusic-disk-gc.sh
#   cat > /etc/cron.d/fnmusic-disk-gc <<'CRON'
#   17 4 * * * root /usr/local/sbin/fnmusic-disk-gc.sh >/dev/null 2>&1
#   CRON
set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

LOG_DIR="${FNMUSIC_GC_LOG_DIR:-/vol1/@appcenter/fnmusic-ext}"
LOG_FILE="${LOG_DIR}/disk-gc.log"
SCRATCH_MAX_AGE_DAYS="${FNMUSIC_GC_SCRATCH_MAX_AGE_DAYS:-2}"
KEEP_BUILD_CACHE_H="${FNMUSIC_GC_KEEP_BUILD_CACHE_H:-24}"

log() {
    mkdir -p "${LOG_DIR}" 2>/dev/null || true
    # 自限长：单文件超过 512K 就轮转一份 .1，避免日志本身把根分区吃满
    if [ -f "${LOG_FILE}" ] && [ "$(stat -c %s "${LOG_FILE}" 2>/dev/null || echo 0)" -gt 524288 ]; then
        mv -f "${LOG_FILE}" "${LOG_FILE}.1" 2>/dev/null || true
    fi
    printf '%s %s\n' "$(date '+%F %T')" "$*" >> "${LOG_FILE}" 2>/dev/null || true
    echo "$*"
}

run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "[dry-run] $*"
        return 0
    fi
    log "执行: $*"
    "$@" >>"${LOG_FILE}" 2>&1 || log "  失败(忽略): $*"
}

root_used_pct() {
    df -P / 2>/dev/null | awk 'NR==2 {gsub("%","",$5); print $5}'
}

log "=== 磁盘回收开始（根分区已用 $(root_used_pct)%） ==="

# 1. Docker：悬空镜像 + 过期构建缓存
if command -v docker >/dev/null 2>&1; then
    run docker image prune -f
    run docker builder prune -f --filter "until=${KEEP_BUILD_CACHE_H}h"
fi

# 2. /tmp scratch：只清已知前缀 + 只清 2 天前的 + 保留备份
SCRATCH_PATTERNS=(
    "/tmp/opencode"
    "/tmp/fnmusic-*"
    "/tmp/patch-*"
    "/tmp/repo"
)
now_epoch="$(date +%s)"
for pattern in "${SCRATCH_PATTERNS[@]}"; do
    for path in ${pattern}; do
        [ -e "${path}" ] || continue
        mtime="$(stat -c %Y "${path}" 2>/dev/null || echo 0)"
        age_days=$(( (now_epoch - mtime) / 86400 ))
        [ "${age_days}" -lt "${SCRATCH_MAX_AGE_DAYS}" ] && continue
        case "${path}" in
            *.tar.gz|*.tar|*.bak|*.env.bak) continue ;;   # 备份保留
        esac
        if [ "${DRY_RUN}" -eq 1 ]; then
            log "[dry-run] rm -rf ${path}（${age_days} 天前）"
        else
            log "清理 scratch: ${path}（${age_days} 天前）"
            rm -rf "${path}" 2>/dev/null || true
        fi
    done
done

# 3. apt 缓存
if command -v apt-get >/dev/null 2>&1; then
    run apt-get clean
fi

# 4. 现状
log "=== 回收完成（根分区已用 $(root_used_pct)%） ==="
df -h / 2>/dev/null | tail -1 >> "${LOG_FILE}" 2>/dev/null || true
