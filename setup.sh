#!/usr/bin/env bash
# 一键安装依赖（macOS / Linux）。Windows 见 README。
set -e
echo "==> 检测系统"
OS="$(uname -s)"; ARCH="$(uname -m)"
echo "    $OS / $ARCH"

install_mac() {
  if ! command -v brew >/dev/null 2>&1; then
    echo "    未安装 Homebrew，请先装：https://brew.sh"; exit 1
  fi
  echo "==> brew 安装 yt-dlp ffmpeg"
  brew install yt-dlp ffmpeg
  if [ "$ARCH" = "arm64" ]; then
    echo "==> Apple Silicon：安装 mlx-whisper（GPU 加速）"
    pip3 install --break-system-packages --user mlx-whisper || pip3 install --user mlx-whisper
  else
    echo "==> Intel Mac：安装 faster-whisper"
    pip3 install --break-system-packages --user faster-whisper || pip3 install --user faster-whisper
  fi
}

install_linux() {
  echo "==> 安装 ffmpeg + yt-dlp（需要 sudo / pip）"
  sudo apt-get update && sudo apt-get install -y ffmpeg || true
  pip3 install --user yt-dlp faster-whisper
}

case "$OS" in
  Darwin) install_mac ;;
  Linux)  install_linux ;;
  *) echo "请手动安装 yt-dlp / ffmpeg / whisper 引擎" ;;
esac

echo ""
echo "==> 检查 ffmpeg 是否带 libass（烧字幕必须）"
NEED_LIBASS=1
if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass "; then
  echo "    [OK] 系统 ffmpeg 自带 libass"
  NEED_LIBASS=0
fi
if [ "$NEED_LIBASS" = 1 ]; then
  echo "    [!] 系统 ffmpeg 缺 libass，下载带 libass 的静态版到 bin/"
  mkdir -p bin
  if [ "$OS" = "Darwin" ]; then
    A="arm64"; [ "$ARCH" != "arm64" ] && A="amd64"
    curl -L --max-time 180 "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$A/release/ffmpeg.zip" -o /tmp/ff.zip \
      && unzip -o /tmp/ff.zip -d bin/ >/dev/null && chmod +x bin/ffmpeg \
      && xattr -dr com.apple.quarantine bin/ffmpeg 2>/dev/null \
      && echo "    [OK] 已装 bin/ffmpeg（带 libass）" || echo "    [缺] 下载失败，请手动装带 libass 的 ffmpeg"
  else
    echo "    Linux：请装带 libass 的 ffmpeg，如 sudo apt install ffmpeg（apt 版通常带 libass）"
  fi
fi

echo ""
echo "==> 检查 Claude Code CLI（翻译/出文档需要它）"
if command -v claude >/dev/null 2>&1; then
  echo "    [OK] $(claude --version 2>/dev/null | head -1)"
  echo "    若未登录，请运行： claude   然后按提示登录一次"
else
  echo "    [缺] 未安装 claude CLI。安装： npm install -g @anthropic-ai/claude-code"
  echo "         安装后运行 claude 登录一次。"
fi

echo ""
echo "==> 完成。启动面板： ./start.sh   （或双击 启动面板.command）"
