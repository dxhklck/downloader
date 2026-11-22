#!/usr/bin/env bash
# 麒麟 V10 ARM64（飞腾/鲲鹏/海思）deb 构建脚本
# 用法：把整个项目目录拷到麒麟机器，在项目根目录执行：
#   bash build_kylin_arm64.sh
# 产物：视频下载助手-1.0.4-kylin-arm64.deb（在当前目录）

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="视频下载助手"
VERSION="1.0.4"
ARCH="arm64"
OUT="视频下载助手-${VERSION}-kylin-${ARCH}.deb"

echo "=== 麒麟 V10 ARM64 构建 ==="
echo "架构: $(uname -m)"

if [ "$(uname -m)" != "aarch64" ] && [ "$(uname -m)" != "arm64" ]; then
  echo "[错误] 本脚本只能在 ARM64 机器上运行，当前架构: $(uname -m)"
  exit 1
fi

# ---- 1. 探测包管理器并安装系统依赖 ----
echo "=== [1/5] 安装系统依赖 ==="
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3 python3-pip python3-gi python3-gi-cairo \
    gir1.2-gtk-3.0 libcairo2-dev dpkg-deb ffmpeg
  # WebKit GIR 包名探测（不同麒麟 SP 版本可能是 4.0 或 4.1）
  for pkg in gir1.2-webkit2-4.1 gir1.2-webkit2-4.0; do
    if apt-cache show "$pkg" >/dev/null 2>&1; then
      sudo apt-get install -y "$pkg"
      WEBKIT_GIR="$pkg"
      break
    fi
  done
elif command -v yum >/dev/null 2>&1 || command -v dnf >/dev/null 2>&1; then
  PKM=$(command -v dnf >/dev/null 2>&1 && echo dnf || echo yum)
  sudo "$PKM" install -y python3 python3-pip pygobject3 gtk3 \
    webkit2gtk3 libwebkit2gtk-devel dpkg ffmpeg
  WEBKIT_GIR="webkit2gtk3"
else
  echo "[错误] 未识别的包管理器（需要 apt 或 yum/dnf）"
  exit 1
fi
: "${WEBKIT_GIR:=gir1.2-webkit2-4.1}"

# ---- 2. 探测 ffmpeg（系统 PATH 中的 ARM64 版本即可）----
echo "=== [2/5] 检查 ffmpeg ==="
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "[错误] 未找到 ffmpeg/ffprobe，请先安装系统 ffmpeg"
  exit 1
fi
mkdir -p bin
cp -f "$(command -v ffmpeg)" bin/ffmpeg
cp -f "$(command -v ffprobe)" bin/ffprobe
echo "  已复制系统 ffmpeg/ffprobe 到 bin/"
chmod +x bin/ffmpeg bin/ffprobe

# ---- 3. Python 依赖 ----
echo "=== [3/5] 安装 Python 依赖 ==="
python3 -m pip install --user --upgrade pip
python3 -m pip install --user -r requirements.txt "pyinstaller==6.11.1"

# 麒麟系统 Python 的 gi 模块需对 PyInstaller 可用
# PyInstaller 会收集系统 site-packages 中的 gi（由 python3-gi 提供）

# ---- 4. PyInstaller 构建 ----
echo "=== [4/5] PyInstaller 打包 ==="
export PYTHONIOENCODING=utf-8
python3 build.py

# ---- 5. 打 deb ----
echo "=== [5/5] 生成 deb 包 ==="
PKG="pkg_kylin"
rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/$APP_NAME" \
  "$PKG/usr/share/applications" "$PKG/usr/share/icons/hicolor/256x256/apps"

cp -r "dist/$APP_NAME/." "$PKG/opt/$APP_NAME/"

cat > "$PKG/DEBIAN/control" <<EOF
Package: video-downloader-assistant
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Depends: python3, python3-gi, gir1.2-gtk-3.0, ${WEBKIT_GIR}, ffmpeg
Maintainer: 视频下载助手 contributors
Description: 对话式视频下载工具（麒麟 V10 ARM64）
 用一句话下载 B站/抖音/YouTube 等视频。
 首次运行请在设置页填写 API Key。
EOF

cat > "$PKG/usr/share/applications/$APP_NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Exec=/opt/$APP_NAME/$APP_NAME
Icon=$APP_NAME
Categories=Network;Utility;
EOF

if [ -f assets/app_256.png ]; then
  cp assets/app_256.png "$PKG/usr/share/icons/hicolor/256x256/apps/$APP_NAME.png"
fi

# PyInstaller 生成的二进制需可执行
find "$PKG/opt/$APP_NAME" -type f -perm -u+x -exec chmod 755 {} + 2>/dev/null || true
if [ -f "$PKG/opt/$APP_NAME/$APP_NAME" ]; then
  chmod +x "$PKG/opt/$APP_NAME/$APP_NAME"
fi

dpkg-deb --build "$PKG" "$OUT"

echo ""
echo "=== 完成 ==="
echo "产物: $OUT ($(du -h "$OUT" | cut -f1))"
echo "安装: sudo dpkg -i $OUT"
echo "若提示缺依赖: sudo apt -f install  或  sudo yum install -y <缺失包>"
