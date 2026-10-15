#!/usr/bin/env bash
# deploy_to_klipper.sh
# 将本目录下的驱动文件以软链接方式部署到 ~/klipper/klippy/extras/
# 同时为 pipette_driver.py 创建 pipette.py 别名（Klipper section 名匹配要求）
#
# 用法：
#   chmod +x deploy_to_klipper.sh
#   ./deploy_to_klipper.sh           # 默认目标：~/klipper/klippy/extras
#   ./deploy_to_klipper.sh /custom/path/extras

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRAS_DIR="${1:-${HOME}/klipper/klippy/extras}"

# 需要软链接的文件：源文件名 -> 目标链接名（相同则只写一次）
declare -A LINKS=(
    ["pipette_driver.py"]="pipette_driver.py"
    ["pipette_action.py"]="pipette_action.py"
    ["pipette_pressure_guard.py"]="pipette_pressure_guard.py"
    ["pipette_pressure_trace.py"]="pipette_pressure_trace.py"
    ["load_cell_probe.py"]="load_cell_probe.py"
    ["adp1000_backend.py"]="adp1000_backend.py"
    ["sp20_backend.py"]="sp20_backend.py"
)
# pipette_driver.py 同时需要 pipette.py 别名供 [pipette] section 使用
ALIAS_SRC="pipette_driver.py"
ALIAS_DST="pipette.py"

# ---- 检查目标目录 ----
if [[ ! -d "$EXTRAS_DIR" ]]; then
    echo "[ERROR] 目标目录不存在: $EXTRAS_DIR"
    echo "        请先确认 Klipper 已安装，或手动指定路径：$0 /path/to/extras"
    exit 1
fi

echo "源目录  : $SCRIPT_DIR"
echo "目标目录: $EXTRAS_DIR"
echo ""

# ---- 创建软链接 ----
for src_name in "${!LINKS[@]}"; do
    dst_name="${LINKS[$src_name]}"
    src_path="$SCRIPT_DIR/$src_name"
    dst_path="$EXTRAS_DIR/$dst_name"

    if [[ ! -f "$src_path" ]]; then
        echo "[SKIP]  $src_name — 源文件不存在，跳过"
        continue
    fi

    if [[ -L "$dst_path" ]]; then
        # 已是软链接，更新
        ln -sf "$src_path" "$dst_path"
        echo "[UPDATE] $dst_name -> $src_path"
    elif [[ -f "$dst_path" ]]; then
        # 是普通文件，备份后替换
        backup="${dst_path}.bak.$(date +%Y%m%d_%H%M%S)"
        mv "$dst_path" "$backup"
        echo "[BACKUP] 原文件已备份为 $backup"
        ln -s "$src_path" "$dst_path"
        echo "[NEW]    $dst_name -> $src_path"
    else
        ln -s "$src_path" "$dst_path"
        echo "[NEW]    $dst_name -> $src_path"
    fi
done

# ---- pipette.py 别名 ----
alias_src_path="$SCRIPT_DIR/$ALIAS_SRC"
alias_dst_path="$EXTRAS_DIR/$ALIAS_DST"

if [[ -L "$alias_dst_path" ]]; then
    ln -sf "$alias_src_path" "$alias_dst_path"
    echo "[UPDATE] $ALIAS_DST -> $alias_src_path  (alias)"
elif [[ -f "$alias_dst_path" ]]; then
    backup="${alias_dst_path}.bak.$(date +%Y%m%d_%H%M%S)"
    mv "$alias_dst_path" "$backup"
    echo "[BACKUP] 原 $ALIAS_DST 已备份为 $backup"
    ln -s "$alias_src_path" "$alias_dst_path"
    echo "[NEW]    $ALIAS_DST -> $alias_src_path  (alias)"
else
    ln -s "$alias_src_path" "$alias_dst_path"
    echo "[NEW]    $ALIAS_DST -> $alias_src_path  (alias)"
fi

echo ""
echo "部署完成，正在重启 Klipper..."
if sudo systemctl restart klipper; then
    echo "Klipper 重启成功"
else
    echo "[WARN] systemctl restart klipper 失败，请手动重启"
fi
