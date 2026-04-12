# bilibili-transcript-oneclick

本地音视频与 **哔哩哔哩** 链接：拉取字幕/弹幕、可选 **faster-whisper** 转写、合并文稿与 **可选 LLM 分析**（Gemini / Groq）。提供 **Windows 便携** 思路：嵌入式 Python + 自带 Chromium（Playwright）登录。

## 功能概览

- **GUI**：`START.bat` / `启动.bat` → `gui.py`
- **命令行**：`bilibili_pipeline.py extract <URL|本地媒体路径>`
- **B 站登录**：Playwright Chromium，会话写入 `browser_state.json`，导出 `cookies.txt` 供 yt-dlp
- **无字幕转写**：yt-dlp 下音频 + faster-whisper；GUI 可选模型 **large-v3** / **small**
- **FFmpeg**：优先 `ffmpeg/` 目录、PATH，其次 static-ffmpeg（可能访问 GitHub）

## 环境要求

- **Windows 10/11 x64**
- 构建便携环境时需要可访问 **python.org、pip、Playwright CDN、Hugging Face**（按需）

## 从源码部署（本仓库不含大文件）

仓库**不包含**（体积/授权原因，见 `.gitignore`）：

| 路径 | 说明 |
|------|------|
| `python_embed/` | 官方 embeddable Python + pip 依赖 |
| `pw-browsers/` | Playwright Chromium |
| `whisper-models/` | 可选；无则首次转写从 Hugging Face 下载 |
| `ffmpeg/*.exe` | 可选；可放 `ffmpeg.exe` / `ffprobe.exe` 免拉 GitHub |

**推荐**：在仓库根目录用 PowerShell 执行（需联网）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
.\准备便携环境.ps1
```

可选跳过 Whisper 预下载（减小本机构建时间）：

```powershell
.\准备便携环境.ps1 -SkipWhisperModel
```

补充 **tkinter**（embed 包默认无 GUI）：若脚本未自动完成，执行 `.\INSTALL_TKINTER.bat` 或 `.\add_tkinter_to_embed.ps1`。

仅预下载 Whisper 模型（默认 large-v3 + small）：

```powershell
.\DOWNLOAD_WHISPER.bat
# 或
.\python_embed\python.exe .\download_whisper_models.py
```

Chromium 若下载失败：双击 `install_chromium.bat` 多试几次，或自行按 Playwright 文档放入 `pw-browsers`。

## API Key（可选）

GUI 左侧 **「API 与模型」** 可填写 **Gemini / OpenAI（GPT）/ Groq / Anthropic（Claude）/ xAI（Grok）** 的 Key 与模型 ID，并选择「首选提供商」或「自动」。

也可复制 `local_api_keys.example.py` 为 `local_api_keys.py`（勿提交），或设置对应环境变量。详见 `llm_analyze.py`。

## 命令行示例

```text
python_embed\python.exe bilibili_pipeline.py extract "https://www.bilibili.com/video/BVxxxx"
python_embed\python.exe bilibili_pipeline.py extract --asr-if-no-subs --whisper-model small "https://..."
```

## 推送到 GitHub

本仓库已包含 `.gitignore`（忽略密钥、`python_embed/`、浏览器与模型等大目录）、GitHub Actions（`.github/workflows/ci.yml`）与 `SECURITY.md`。

### 方式 A：一键脚本（推荐）

1. 安装 **Git** 与 **GitHub CLI**：`winget install Git.Git`、`winget install GitHub.cli`
2. 在本目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File ".\一键推送GitHub.ps1"
```

按提示浏览器登录 GitHub，脚本会创建**公开**仓库并推送 `main`。

### 方式 B：手动

1. 安装 [Git for Windows](https://git-scm.com/download/win)。
2. 在 GitHub 网页 **New repository** 建空仓库（不要勾选自动添加 README）。
3. 若尚未初始化：`git init` 且 `git checkout -b main`，再 `git add .` / `git commit`。
4. 添加远程并推送：

```bash
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main
```

若使用 SSH：`git remote add origin git@github.com:<用户>/<仓库>.git`

**注意**：勿将 `local_api_keys.py`、`cookies.txt` 等提交进仓库（已在 `.gitignore`）。

**说明**：在 Cursor 里看到的 `SHA256:…` 多为 **github.com 的 SSH 主机密钥指纹**，用于核对身份，**不是**可用来登录的令牌；登录请用 `gh auth login` 或 GitHub 网页 **Personal access token**。

## 许可证

见仓库内 `LICENSE`（MIT）。

## English summary

Portable-oriented toolkit for Bilibili URLs and local media: subtitles/danmaku, optional faster-whisper ASR, merged transcript + optional LLM report. Large binaries (`python_embed`, `pw-browsers`, models, ffmpeg exes) are not in git; run `准备便携环境.ps1` on Windows to bootstrap.
