# 🎬 视频翻译控制面板 (video-translate-panel)

一个**本地网页界面**，把"下载视频 → Whisper 转写 → 翻译 → 烧录中文字幕 / 出 Markdown"整条流程变成填表点按钮。支持 **YouTube / 抖音 / 本地视频**。

- **下载 / 转写 / 烧录** 由面板直接执行（实时日志可见）
- **翻译润色 / 出文档** 由面板调度 `claude -p`（用你的 Claude Code 登录，**无需 API key**）
- 纯 Python 标准库，**零第三方 Python 包**即可起服务
- 路径全部相对自身，**clone 到任何电脑即可用**

---

## 快速开始

```bash
git clone <仓库地址> video-translate-panel
cd video-translate-panel
bash setup.sh        # 安装 yt-dlp / ffmpeg / whisper 引擎（首次）
bash start.sh        # 启动面板（macOS/Linux）
```

- **macOS** 也可双击 `启动面板.command`
- **Windows** 双击 `start.bat`（需先装好依赖，见下）

启动后浏览器自动打开 `http://127.0.0.1:8765`。停止：终端窗口按 `Ctrl+C`。

---

## 依赖清单

| 依赖 | 用途 | 安装 |
|------|------|------|
| Python 3.9+ | 运行面板 | 系统自带 / python.org |
| **yt-dlp** | 下载 YouTube/抖音 | `brew install yt-dlp` / `pip install yt-dlp` |
| **ffmpeg** | 提取音频、烧录字幕 | `brew install ffmpeg` / winget / apt |
| **mlx-whisper**（Apple Silicon）或 **faster-whisper**（其它） | 语音转写 | `pip install mlx-whisper` / `pip install faster-whisper` |
| **claude CLI** | 翻译润色 / 出文档 | `npm i -g @anthropic-ai/claude-code`，装好后跑一次 `claude` 登录 |

`setup.sh` 会按你的系统自动装前三类。`claude` 需自行安装并登录一次。

### Windows 提示
- 装 Python、ffmpeg（winget install Gyan.FFmpeg）、`pip install yt-dlp faster-whisper`
- 装 Node 后 `npm i -g @anthropic-ai/claude-code` 并运行 `claude` 登录
- 双击 `start.bat` 启动

---

## 用法

1. **配置**：填「成品输出目录」（绝对路径），选默认字幕类型，保存。留空则默认输出到仓库内 `output/`。
2. **新建任务**：选来源（YouTube / 抖音 / 本地文件）→ 选「翻译视频」或「转成 Markdown」→（翻译时）选中文/双语 → 点「开始」。
3. **看日志**：下载/转写/翻译/烧录每一步实时滚动。完成后列出成品路径，可一键在文件管理器打开。

成品在输出目录的 `data/`，中间文件在 `tmp/`。

### 抖音首次使用
需先登录一次（弹出浏览器扫码/登录）：
```bash
python3 scripts/douyin_login.py
```

---

## 配置项

`config.json`（首次运行自动生成，已在 .gitignore 中，不会上传）：

```json
{ "output_dir": "/绝对/路径", "settings": { "subtitle_type": "zh" } }
```

环境变量：
- `VIDEO_PANEL_PORT`：端口（默认 8765）
- `VIDEO_PANEL_MODEL`：翻译用模型（默认 `sonnet`）
- `VIDEO_PANEL_NO_BROWSER=1`：启动时不自动开浏览器

---

## 许可证

本项目 MIT。`scripts/` 下脚本来自 [xiaohuailabs/xiaohu-video-translate](https://github.com/xiaohuailabs/xiaohu-video-translate)（MIT），详见 `NOTICE.md`。
