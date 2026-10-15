#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WAIT_SECONDS="${RP2040_WAIT_SECONDS:-20}"

# 全局：记录自动创建的临时挂载点，供 EXIT trap 清理
_AUTO_MOUNT_DIR=""

log() {
    printf '[flash_rp2040_bootsel] %s\n' "$*" >&2
}

die() {
    printf '[flash_rp2040_bootsel] 错误: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$_AUTO_MOUNT_DIR" && -d "$_AUTO_MOUNT_DIR" ]]; then
        umount "$_AUTO_MOUNT_DIR" 2>/dev/null || true
        rmdir  "$_AUTO_MOUNT_DIR" 2>/dev/null || true
    fi
}

usage() {
    cat <<EOF
用法:
  $(basename "$0") [firmware.uf2]

说明:
  1. 先让 RP2040 进入 BOOTSEL 烧录模式（按住 BOOTSEL 键再上电/复位）。
  2. 脚本自动检测 USB 设备（VID:PID 2e8a:0003），创建临时挂载点并挂载，
     复制 UF2 固件后自动卸载并删除临时挂载点。
  3. 未传 firmware.uf2 时，自动选择脚本所在目录下修改时间最新的 .uf2 文件。

环境变量:
  RP2040_MOUNT_POINT   跳过自动挂载，直接使用已挂载的指定目录
  RP2040_WAIT_SECONDS  等待设备出现的秒数，默认 20
EOF
}

canonical_path() {
    local input="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath "$input"
        return
    fi

    local dir_name base_name
    dir_name="$(CDPATH= cd -- "$(dirname -- "$input")" && pwd)"
    base_name="$(basename -- "$input")"
    printf '%s/%s\n' "$dir_name" "$base_name"
}

file_mtime_and_path() {
    local input="$1"
    if stat -c '%Y %n' "$input" >/dev/null 2>&1; then
        stat -c '%Y %n' "$input"
        return
    fi

    stat -f '%m %N' "$input"
}

resolve_uf2_path() {
    if [[ $# -ge 1 && -n "${1:-}" ]]; then
        [[ -f "$1" ]] || die "UF2 文件不存在: $1"
        canonical_path "$1"
        return
    fi

    [[ -d "${SCRIPT_DIR}" ]] || die "未找到脚本目录，请检查环境配置。"

    local uf2_path
    uf2_path="$(
        find "${SCRIPT_DIR}" -maxdepth 1 -type f -name '*.uf2' -print 2>/dev/null \
            | while IFS= read -r file_path; do
                file_mtime_and_path "$file_path"
            done \
            | sort -nr \
            | head -n 1 \
            | cut -d' ' -f2-
    )"

    [[ -n "$uf2_path" ]] || die "未找到 UF2 文件，请在脚本同目录下放置 firmware.uf2，或手动传入 firmware.uf2 参数。"
    printf '%s\n' "$uf2_path"
}

# 通过 sysfs 查找 RP2040 BOOTSEL 对应的块设备（VID:PID 2e8a:0003）
find_rp2040_block_device() {
    local usb_path block_dir block_dev
    for usb_path in /sys/bus/usb/devices/*/; do
        [[ -f "${usb_path}idVendor" ]] || continue
        if [[ "$(< "${usb_path}idVendor")" == "2e8a" && \
              "$(< "${usb_path}idProduct")" == "0003" ]]; then
            while IFS= read -r block_dir; do
                for _bd_path in "${block_dir}/"/*/; do
                    block_dev="$(basename -- "$_bd_path")"
                    if [[ -n "$block_dev" && "$block_dev" != '*' ]]; then
                        # 优先返回第一个分区（如 sda1），若无分区则返回裸设备（如 sda）
                        local _part
                        for _part in "/sys/block/${block_dev}/${block_dev}"[0-9]*/; do
                            local _part_name
                            _part_name="$(basename -- "$_part")"
                            if [[ -n "$_part_name" && "$_part_name" != '*' ]]; then
                                printf '/dev/%s\n' "$_part_name"
                                return 0
                            fi
                        done
                        printf '/dev/%s\n' "$block_dev"
                        return 0
                    fi
                done
            done < <(find "${usb_path}" -maxdepth 8 -type d -name 'block' 2>/dev/null)
        fi
    done
    return 1
}

wait_for_rp2040_device() {
    local block_dev="" elapsed=0
    [[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]] || die "RP2040_WAIT_SECONDS 必须是非负整数。"
    log "等待 RP2040 BOOTSEL 设备出现 (最多 ${WAIT_SECONDS}s)..."
    while (( elapsed <= WAIT_SECONDS )); do
        block_dev="$(find_rp2040_block_device || true)"
        if [[ -n "$block_dev" ]]; then
            printf '%s\n' "$block_dev"
            return 0
        fi
        sleep 1
        elapsed=$(( elapsed + 1 ))
    done
    return 1
}

main() {
    if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
        usage
        exit 0
    fi

    [[ $# -le 1 ]] || die "仅支持一个可选参数: firmware.uf2"

    trap cleanup EXIT

    local uf2_path mount_point=""
    uf2_path="$(resolve_uf2_path "${1:-}")"
    log "固件: $uf2_path"

    if [[ -n "${RP2040_MOUNT_POINT:-}" ]]; then
        [[ -d "${RP2040_MOUNT_POINT}" ]] || die "指定的挂载点不存在: ${RP2040_MOUNT_POINT}"
        mount_point="${RP2040_MOUNT_POINT}"
        log "使用指定挂载点: $mount_point"
    else
        local block_dev
        block_dev="$(wait_for_rp2040_device)" \
            || die "未检测到 RP2040 BOOTSEL 设备，请确认已进入 BOOTSEL 模式。"
        log "检测到设备: $block_dev"

        mount_point="$(mktemp -d /tmp/rpi-rp2-XXXXXX)"
        _AUTO_MOUNT_DIR="$mount_point"

        log "挂载 ${block_dev} -> ${mount_point}"
        mount -t vfat -o uid="$(id -u)",gid="$(id -g)",noatime "${block_dev}" "${mount_point}" \
            || die "挂载失败: ${block_dev}，请检查权限（可能需要 sudo）。"
    fi

    local target_path
    target_path="${mount_point}/$(basename -- "$uf2_path")"
    log "挂载点: $mount_point"
    cp -f "$uf2_path" "$target_path"
    sync
    log "固件已写入，设备将自动重启。"

    if [[ -n "$_AUTO_MOUNT_DIR" ]]; then
        umount "$mount_point" 2>/dev/null || true
        rmdir  "$mount_point"  2>/dev/null || true
        _AUTO_MOUNT_DIR=""
        log "已卸载并清理临时挂载点: ${mount_point}"
    fi
}

main "$@"
