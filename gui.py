"""
本地音视频 + B 站：转写、合并文稿与分析；浅色 Cursor 风界面（统一页底色 + 白底编辑区），左侧导航 + 右侧内容区（分析报告内含对话）。

高 DPI：启动前启用 Windows Per-Monitor V2 感知；可用环境变量 BILIBILI_GUI_SCALE 覆盖缩放。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

ROOT = Path(__file__).resolve().parent
# 便携版：子进程继承此变量，Playwright 使用目录内浏览器
_pw_browsers = ROOT / "pw-browsers"
if _pw_browsers.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_pw_browsers.resolve()))
sys.path.insert(0, str(ROOT))
from llm_analyze import (
    MAX_LOCAL_CHAT_MERGED_SNIPPET,
    SYSTEM_LOCAL_CHAT_ZH,
    chat_followup,
    collect_timeline_frame_paths,
    local_openai_compatible_chat_round,
    resolve_provider,
)
from transcribe_local import (
    SUPPORTED_LOCAL_MEDIA_SUFFIXES,
    WHISPER_MODEL_CHOICES,
    default_whisper_model_choice,
    is_supported_local_media,
)

# Windows：PROCESS_PER_MONITOR_DPI_AWARE_V2，须在创建任何 Tk 窗口前调用
_WIN_PMDPI_V2 = 2


def _win_set_per_monitor_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(_WIN_PMDPI_V2)
    except (AttributeError, OSError):
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _read_gui_scale_env() -> float | None:
    raw = os.environ.get("BILIBILI_GUI_SCALE", "").strip()
    if not raw:
        return None
    try:
        return max(0.75, min(float(raw), 4.0))
    except ValueError:
        return None


def _detect_tk_scale(widget: tk.Misc) -> float:
    env = _read_gui_scale_env()
    if env is not None:
        return env
    try:
        px_per_inch = float(widget.winfo_fpixels("1i"))
        s = px_per_inch / 72.0
    except tk.TclError:
        s = 1.0
    return max(1.0, min(s, 3.0))


def _pick_mono_family(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Consolas"
    for name in ("Cascadia Mono", "JetBrains Mono", "Consolas", "Lucida Console"):
        if name in families:
            return name
    return "Consolas"


def _font_soft_factor(geom_scale: float) -> float:
    """高 DPI 下 tk scaling 已放大界面，字号不要再按 geom 线性乘，只做轻微补偿。"""
    g = min(max(geom_scale, 1.0), 2.25)
    return 1.0 + (g - 1.0) * 0.28


def _pick_sans_cjk(root: tk.Misc) -> str:
    """正文用无衬线 + 中文优先，避免 Tk Text 默认回退成宋体发糊。"""
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Segoe UI"
    for name in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Source Han Sans SC",
        "Noto Sans CJK SC",
        "Segoe UI",
    ):
        if name in families:
            return name
    return "Segoe UI"


OUT = ROOT / "out"
MERGED = OUT / "transcript_merged.txt"
ANALYSIS = OUT / "video_analysis.txt"
ANALYSIS_DEEP_JSON = OUT / "video_analysis_deep.json"
LOCAL_CHAT_ROOT = ROOT / "local_chat_data"
# 「本地对话」默认指向 Vision 副本中 SERVE_GEMMA4_4BIT.bat 的监听地址（非 LM Studio 的 1234）
DEFAULT_LOCAL_OPENAI_BASE = "http://127.0.0.1:18090/v1"
DEFAULT_LOCAL_OPENAI_MODEL_ID = "gemma-4-31b-4bit"


def _maybe_rewrite_lm_studio_local_url(url: str | None) -> str:
    """空串保持空；经典 LM Studio 默认 1234 → 本项目 Gemma 18090。"""
    u = (url if url is not None else "").strip()
    if not u:
        return ""
    low = u.rstrip("/").lower()
    if low in ("http://127.0.0.1:1234/v1", "http://localhost:1234/v1"):
        return DEFAULT_LOCAL_OPENAI_BASE
    return u


def _local_openai_base_for_ui(stored: str | None) -> str:
    """偏好/输入框：空或上述 LM 默认 → Gemma 默认。"""
    return _maybe_rewrite_lm_studio_local_url(stored) or DEFAULT_LOCAL_OPENAI_BASE


def _migrate_local_llm_prefs_json() -> None:
    try:
        if not LLM_GUI_PREF_JSON.is_file():
            return
        raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return
        changed = False
        for key in ("local_inf_autofill_url", "chat_local_base_url"):
            if key not in raw:
                continue
            old = raw.get(key)
            if not isinstance(old, str):
                continue
            new = _local_openai_base_for_ui(old)
            if new != old:
                raw[key] = new
                changed = True
        if changed:
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, json.JSONDecodeError, TypeError):
        pass


LLM_PROVIDER_COMBO_VALUES = ("auto", "gemini", "openai", "groq", "anthropic", "xai")
_LLM_PROVIDER_SET = frozenset(LLM_PROVIDER_COMBO_VALUES)
LLM_GUI_PREF_JSON = ROOT / "local_llm_prefs.json"


def _norm_llm_provider(raw: str) -> str:
    p = (raw or "auto").strip().lower()
    return p if p in _LLM_PROVIDER_SET else "auto"


def _load_gui_llm_provider_pref() -> str:
    try:
        if not LLM_GUI_PREF_JSON.is_file():
            return ""
        data = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return _norm_llm_provider(str(data.get("llm_provider", "")))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def _save_gui_llm_provider_pref(provider: str) -> None:
    prov = _norm_llm_provider(provider)
    merged: dict = {}
    try:
        if LLM_GUI_PREF_JSON.is_file():
            raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged = raw
    except (OSError, json.JSONDecodeError, TypeError):
        merged = {}
    merged["llm_provider"] = prov
    try:
        LLM_GUI_PREF_JSON.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

# Cursor 风浅色：统一「页」底色（侧栏+主区同色），白底仅用于输入/正文区，避免灰条与纯白硬切
T_PAGE = "#f3f4f6"
T_PANEL = "#ffffff"
T_BG = T_PAGE
T_SURFACE = T_PAGE
T_RAISED = "#e8eaef"
T_BORDER = "#d8dce3"
T_TEXT = "#1f2937"
T_MUTED = "#6b7280"
T_ACCENT = "#0969da"
T_ACCENT_ACTIVE = "#0550ae"
T_ENTRY = T_PANEL
T_SELECT = "#cce5ff"

NAV_BG = T_PAGE
NAV_ACTIVE = T_PANEL
NAV_DIVIDER = "#d0d4dc"

# 对话气泡（与页面区分仍靠圆角卡片色，不用大块灰底）
A_BG = T_PANEL
A_USER = "#e8eeff"
A_CARD = "#f0f2f5"
A_CARD_BORDER = "#d8dce3"
A_META = "#6b7280"
A_ERR = "#fef2f2"
A_COMPOSER = T_PANEL
A_TEXT = "#1f2937"

SCROLL_TROUGH = "#dfe3e8"

# API 设置页：信息条与卡片（略提亮，减少「一片灰」）
API_INFO_BG = "#eef4ff"
API_INFO_BORDER = "#c9daf8"
API_SECTION_MUTED = "#5c6570"

# 常见 B 站域名与短链（分享链接不一定是 www.bilibili.com）
URL_HINTS = (
    "bilibili.com",
    "bilibili.tv",
    "b23.tv",
    "bili2233.cn",
    "bilivideo.com",
)


def looks_like_bilibili_url(url: str) -> bool:
    u = url.strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    return any(h in u for h in URL_HINTS) or bool(re.search(r"\bBV1[\w]{9}\b", url, re.I))


def is_valid_task_source(text: str) -> bool:
    s = text.strip()
    if looks_like_bilibili_url(s):
        return True
    p = Path(os.path.expanduser(s.strip('"')))
    try:
        return bool(p.is_file() and is_supported_local_media(p))
    except OSError:
        return False


def bind_text_mousewheel(text: tk.Text, *, lines_per_notch: int) -> None:
    """加快 Text 内滚轮翻动：每「格」滚动多行（系统默认约 3 行偏慢）。"""

    def on_wheel(event: tk.Event) -> None:
        d = getattr(event, "delta", 0) or 0
        if d:
            if sys.platform == "win32":
                steps = int(-d * lines_per_notch / 120.0)
                if steps == 0:
                    steps = -lines_per_notch if d > 0 else lines_per_notch
            else:
                steps = int(-d * lines_per_notch / 120.0)
                if steps == 0:
                    steps = -lines_per_notch if d > 0 else lines_per_notch
            if steps:
                text.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            text.yview_scroll(-lines_per_notch, "units")
        elif n == 5:
            text.yview_scroll(lines_per_notch, "units")

    text.bind("<MouseWheel>", on_wheel)
    text.bind("<Button-4>", on_wheel)
    text.bind("<Button-5>", on_wheel)


def _widget_under_ancestor(w: tk.Misc | None, ancestor: tk.Misc) -> bool:
    cur: tk.Misc | None = w
    while cur is not None:
        if cur == ancestor:
            return True
        cur = cur.master  # type: ignore[assignment]
    return False


def make_text_y_scrollbar(
    parent: tk.Misc,
    text: tk.Text,
    *,
    width_px: int,
    trough: str = SCROLL_TROUGH,
    thumb: str = "#c4c4c4",
    thumb_active: str = "#a8a8a8",
) -> tk.Scrollbar:
    """粗竖条（浅色主题默认）。"""
    sb = tk.Scrollbar(
        parent,
        command=text.yview,
        width=width_px,
        borderwidth=0,
        troughcolor=trough,
        bg=thumb,
        activebackground=thumb_active,
        highlightthickness=0,
        relief="flat",
        jump=1,
    )
    text.configure(yscrollcommand=sb.set)
    return sb


def sanitize_analysis_display(text: str) -> str:
    """弱化旧版 Markdown 痕迹，便于阅读（新 prompt 下模型应不再输出这些）。"""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\n"):
            core, nl = line[:-1], "\n"
        else:
            core, nl = line, ""
        core = re.sub(r"^#{1,6}\s*", "", core)
        core = re.sub(r"\*\*([^*]+)\*\*", r"\1", core)
        core = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", core)
        core = re.sub(r"^\*\s+", "· ", core)
        core = re.sub(r"^-\s+", "· ", core)
        core = re.sub(r"(?m)^\s+-\s+", "  · ", core)
        out.append(core + nl)
    return "".join(out)


_REPORT_META_LINE = re.compile(r"^（[^）\n]{0,400}）$")


def report_line_style_tag(line: str) -> str | None:
    """按行选择 Tk Text 标签，与纯文本章节标题约定一致。"""
    s = line.strip()
    if not s:
        return None
    if s.startswith("【视频内容总结】"):
        return "report_title"
    if _REPORT_META_LINE.match(s):
        return "report_meta"
    if re.match(r"^[一二三四五六七八九十百千万]+、", s):
        return "report_h1"
    if re.match(r"^观看建议[：:]?", s):
        return "report_subhead"
    if re.match(r"^第[一二三四五六七八九十百千万0-9]+[章节卷、.．]", s):
        return "report_h1"
    return None


def run_pipeline(
    url: str,
    on_line,
    on_done,
    *,
    asr_if_no_subs: bool = False,
    whisper_model: str | None = None,
    llm_provider: str = "auto",
) -> None:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")

    def worker() -> None:
        code = -1
        try:
            cmd = [
                sys.executable,
                "-u",
                str(ROOT / "bilibili_pipeline.py"),
                "extract",
            ]
            if asr_if_no_subs:
                cmd.append("--asr-if-no-subs")
            wm = (whisper_model or default_whisper_model_choice()).strip()
            if wm not in WHISPER_MODEL_CHOICES:
                wm = default_whisper_model_choice()
            cmd.extend(["--whisper-model", wm])
            cmd.append(url.strip())
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                on_line(line)
            code = proc.wait()
            # 确保分析报告与当前 transcript_merged.txt 一致（避免子进程缓冲/旧版管线漏跑 analyze）
            if code == 0:
                on_line("\n--- 正在刷新分析报告（analyze_transcript.py）---\n")
                lp = (llm_provider or "auto").strip() or "auto"
                r = subprocess.run(
                    [
                        sys.executable,
                        "-u",
                        str(ROOT / "analyze_transcript.py"),
                        "--llm-provider",
                        lp,
                    ],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=120,
                )
                if r.stdout:
                    on_line(r.stdout)
                if r.stderr:
                    on_line(r.stderr)
                if r.returncode != 0:
                    on_line(f"\n[警告] 分析报告步骤退出码 {r.returncode}\n")
                    code = r.returncode
        except Exception as e:
            on_line(f"\n[错误] {e}\n")
        finally:
            on_done(code)

    threading.Thread(target=worker, daemon=True).start()


def _analysis_report_bottom_pane_minsize(
    font_content: tuple,
    report_edge: int,
    *,
    font_soft: float,
) -> int:
    """分析报告页纵向分割：下半栏最小高度，避免缩放窗口时输入框与按钮被裁掉。"""
    sz = float(font_content[1])
    inp_lines = 3
    line_h = max(18, int(round(sz * 1.55)))
    composer = max(168, 18 + inp_lines * line_h + 50)
    # 分隔线 + 内边距 + 输入条 + 至少一条聊天可视区
    return max(292, int(round(10 + 8 + report_edge + composer + 72 * font_soft)))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        _migrate_local_llm_prefs_json()
        self.title("本地与 B 站 · 转写与分析")
        self.configure(bg=T_BG)

        self.update_idletasks()
        self._scale = _detect_tk_scale(self)
        try:
            self.tk.call("tk", "scaling", "-displayof", ".", str(self._scale))
        except tk.TclError:
            pass

        self._geom_scale = min(self._scale, 2.25)
        gw = int(1180 * self._geom_scale)
        gh = int(760 * self._geom_scale)
        self.geometry(f"{gw}x{gh}")
        self.minsize(
            int(880 * min(self._geom_scale, 2.0)),
            int(600 * min(self._geom_scale, 2.0)),
        )

        self._font_soft = _font_soft_factor(self._geom_scale)
        self._scrollbar_px = max(
            18, int(round(20 + 8 * (min(self._geom_scale, 2.25) - 1.0)))
        )
        self._text_wheel_lines = max(6, min(14, int(round(9 * self._font_soft))))
        self._run_button_style = self._setup_fonts_and_styles()
        self._init_llm_settings_vars()
        self.agent_session = AgentSession(self)
        inner = max(8, int(round(8 + 3 * (min(self._geom_scale, 2.0) - 1.0))))

        main = tk.Frame(self, bg=T_BG)
        main.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        nav_w = int(200 * min(self._geom_scale, 1.35))
        self._nav = tk.Frame(main, bg=NAV_BG, width=nav_w)
        self._nav.pack(side=tk.LEFT, fill=tk.Y)
        self._nav.pack_propagate(False)

        tk.Label(
            self._nav,
            text="功能",
            font=(self._font_ui[0], max(8, int(round(9 * self._font_soft))), "bold"),
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=14, pady=(16, 8))

        self._nav_rows: dict[str, tk.Frame] = {}
        self._nav_labels: dict[str, tk.Label] = {}
        self._current_nav = ""

        def nav_item(key: str, title: str) -> None:
            row = tk.Frame(self._nav, bg=NAV_BG, cursor="hand2")
            row.pack(fill=tk.X, padx=8, pady=2)
            lb = tk.Label(
                row,
                text=title,
                font=self._font_ui,
                bg=NAV_BG,
                fg=T_TEXT,
                anchor=tk.W,
                padx=10,
                pady=8,
            )
            lb.pack(fill=tk.X)
            self._nav_rows[key] = row
            self._nav_labels[key] = lb

            def on_enter(_e: object) -> None:
                if self._current_nav == key:
                    return
                row.configure(bg=NAV_ACTIVE)
                lb.configure(bg=NAV_ACTIVE)

            def on_leave(_e: object) -> None:
                if self._current_nav == key:
                    row.configure(bg=NAV_ACTIVE)
                    lb.configure(bg=NAV_ACTIVE)
                else:
                    row.configure(bg=NAV_BG)
                    lb.configure(bg=NAV_BG)

            def on_click(_e: object) -> None:
                self._show_view(key)

            for w in (row, lb):
                w.bind("<Button-1>", on_click)
                w.bind("<Enter>", on_enter)
                w.bind("<Leave>", on_leave)

        nav_item("task", "提取与日志")
        nav_item("merged", "合并文稿")
        nav_item("report", "分析报告")
        nav_item("local_chat", "本地对话")
        nav_item("api", "API 与模型")

        tk.Frame(main, bg=NAV_DIVIDER, width=1).pack(side=tk.LEFT, fill=tk.Y)

        work = ttk.Frame(main, padding=(inner, inner, inner, inner))
        work.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._content_host = ttk.Frame(work)
        self._content_host.pack(fill=tk.BOTH, expand=True)
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)

        self.frame_task = ttk.Frame(self._content_host, padding=0)
        self.frame_merged = ttk.Frame(self._content_host, padding=0)
        self.frame_report = ttk.Frame(self._content_host, padding=0)
        self.frame_local_chat = ttk.Frame(self._content_host, padding=0)
        self.frame_api = ttk.Frame(self._content_host, padding=0)

        for f in (
            self.frame_task,
            self.frame_merged,
            self.frame_report,
            self.frame_local_chat,
            self.frame_api,
        ):
            f.grid(row=0, column=0, sticky="nsew")

        self._views = {
            "task": self.frame_task,
            "merged": self.frame_merged,
            "report": self.frame_report,
            "local_chat": self.frame_local_chat,
            "api": self.frame_api,
        }

        card = ttk.LabelFrame(self.frame_task, text=" 提取任务 ", padding=inner)
        card.pack(fill=tk.X, pady=(0, inner))

        ttk.Label(card, text="链接或本地文件").pack(anchor=tk.W, pady=(0, 4))
        self.url_var = tk.StringVar(value="")
        row = ttk.Frame(card)
        row.pack(fill=tk.X, pady=(0, 6))
        entry = ttk.Entry(row, textvariable=self.url_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        entry.bind("<Return>", lambda e: self.start_run())

        media_glob = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_LOCAL_MEDIA_SUFFIXES))

        def browse_file() -> None:
            path = filedialog.askopenfilename(
                parent=self,
                title="选择音视频文件",
                filetypes=[
                    ("音视频", media_glob),
                    ("全部文件", "*.*"),
                ],
            )
            if path:
                self.url_var.set(path)

        ttk.Button(row, text="浏览…", command=browse_file, width=10).pack(side=tk.RIGHT)

        hint_pt = max(8, min(10, int(round(8 * self._font_soft))))
        hint_font = (self._font_ui[0], hint_pt)
        ttk.Label(
            card,
            text="B 站：以 https:// 开头的链接（www / m 站、b23.tv、含 BV 号均可）。"
            " 本地：点「浏览」或粘贴 mp3、m4a、wav、mp4、mkv 等路径。",
            wraplength=int(720 * self._geom_scale),
            font=hint_font,
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(0, inner))

        ttk.Separator(card, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, inner))

        model_row = ttk.Frame(card)
        model_row.pack(fill=tk.X, pady=(0, inner))
        ttk.Label(model_row, text="本地 Whisper 模型").pack(side=tk.LEFT, padx=(0, 8))
        self.whisper_model_var = tk.StringVar(value=default_whisper_model_choice())
        self.whisper_combo = ttk.Combobox(
            model_row,
            textvariable=self.whisper_model_var,
            values=WHISPER_MODEL_CHOICES,
            state="readonly",
            width=14,
        )
        self.whisper_combo.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(
            model_row,
            text="large-v3 更准确；small 更快、占用更小。用于本地音视频转写，或勾选「无字幕转写」时的口播识别。",
            wraplength=int(560 * self._geom_scale),
            font=hint_font,
            foreground=T_MUTED,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        actions = ttk.Frame(card)
        actions.pack(fill=tk.X)

        self.asr_if_no_subs_var = tk.BooleanVar(value=True)
        asr_row = tk.Frame(actions, bg=T_SURFACE, cursor="hand2")
        asr_row.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._asr_sym = tk.Label(
            asr_row,
            text="",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_ACCENT,
            cursor="hand2",
            padx=0,
            pady=0,
        )
        self._asr_sym.pack(side=tk.LEFT, padx=(0, 8), anchor=tk.CENTER)

        asr_caption = tk.Label(
            asr_row,
            text="无字幕时使用本地语音转写（较慢；模型见上一行下拉框）",
            font=self._font_ui,
            bg=T_SURFACE,
            fg=T_TEXT,
            cursor="hand2",
            anchor=tk.W,
        )
        asr_caption.pack(side=tk.LEFT, anchor=tk.CENTER)

        def _asr_click(_event: object = None) -> None:
            self.asr_if_no_subs_var.set(not self.asr_if_no_subs_var.get())

        for w in (asr_row, self._asr_sym, asr_caption):
            w.bind("<Button-1>", _asr_click)

        self.asr_if_no_subs_var.trace_add("write", lambda *_: self._sync_asr_symbol())
        self._sync_asr_symbol()

        btn_row = ttk.Frame(actions)
        btn_row.pack(side=tk.RIGHT)
        self.run_btn = ttk.Button(
            btn_row,
            text="开始提取与分析",
            command=self.start_run,
            style=self._run_button_style,
        )
        self.run_btn.pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text="重新载入输出文件", command=self.reload_files).pack(
            side=tk.LEFT
        )

        status_outer = ttk.Frame(self.frame_task, padding=(0, inner, 0, inner))
        status_outer.pack(fill=tk.X)
        self.status = ttk.Label(
            status_outer,
            text="就绪（首次运行可能需要浏览器登录 B 站）",
            anchor=tk.W,
        )
        self.status.pack(fill=tk.X)

        log_card = ttk.LabelFrame(self.frame_task, text=" 运行日志 ", padding=inner)
        log_card.pack(fill=tk.BOTH, expand=True, pady=(inner, 0))
        self.log_txt = self._build_text_view(log_card, mono=True)
        self.merged_txt = self._build_text_view(self.frame_merged, mono=False)

        report_edge = max(14, inner + 6)
        rep_paned = ttk.Panedwindow(self.frame_report, orient=tk.VERTICAL)
        rep_paned.pack(fill=tk.BOTH, expand=True)
        rep_top = ttk.Frame(
            rep_paned,
            padding=(report_edge, report_edge, report_edge, 14),
        )
        rep_paned.add(rep_top, weight=14)
        rep_bot = ttk.Frame(rep_paned, padding=0)
        rep_paned.add(rep_bot, weight=4)
        self.report_txt = self._build_text_view(rep_top, mono=False)
        self._configure_report_text_tags(self.report_txt)
        ttk.Separator(rep_bot, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(0, 8))
        rep_bot_inner = ttk.Frame(
            rep_bot,
            padding=(report_edge, 0, report_edge, report_edge),
        )
        rep_bot_inner.pack(fill=tk.BOTH, expand=True)
        self._report_chat_parent = rep_bot_inner
        self.report_agent_panel = AgentChatPanel(
            rep_bot_inner, self, self.agent_session, compact=True
        )
        self.report_agent_panel.pack(fill=tk.BOTH, expand=True)
        try:
            rep_paned.paneconfigure(rep_top, minsize=160)
            rep_paned.paneconfigure(
                rep_bot,
                minsize=_analysis_report_bottom_pane_minsize(
                    self._font_content,
                    report_edge,
                    font_soft=self._font_soft,
                ),
            )
        except tk.TclError:
            pass

        self._build_api_settings_page(inner)

        self._local_chat_hub = LocalChatHub(self.frame_local_chat, self)
        self._local_chat_hub.pack(fill=tk.BOTH, expand=True)

        self._busy = False
        self._show_view("task")
        entry.focus_set()

    def _init_llm_settings_vars(self) -> None:
        g = os.environ.get
        pref = _load_gui_llm_provider_pref()
        envp = _norm_llm_provider(g("LLM_PROVIDER", ""))
        self.llm_provider_var = tk.StringVar(value=(pref or envp or "auto"))
        self._llm_gemini_key = tk.StringVar(value=g("GEMINI_API_KEY", ""))
        self._llm_openai_key = tk.StringVar(value=g("OPENAI_API_KEY", ""))
        self._llm_groq_key = tk.StringVar(value=g("GROQ_API_KEY", ""))
        self._llm_anthropic_key = tk.StringVar(value=g("ANTHROPIC_API_KEY", ""))
        self._llm_xai_key = tk.StringVar(value=g("XAI_API_KEY", ""))
        self._llm_gemini_model = tk.StringVar(value=g("GEMINI_MODEL", ""))
        self._llm_openai_model = tk.StringVar(value=g("OPENAI_MODEL", ""))
        self._llm_openai_base = tk.StringVar(
            value=_maybe_rewrite_lm_studio_local_url(g("OPENAI_BASE_URL", ""))
        )
        self._llm_groq_model = tk.StringVar(value=g("GROQ_MODEL", ""))
        self._llm_anthropic_model = tk.StringVar(value=g("ANTHROPIC_MODEL", ""))
        self._llm_xai_model = tk.StringVar(value=g("XAI_MODEL", ""))
        self.report_chat_attach_vision_var = tk.BooleanVar(value=False)

    def _sync_llm_env_from_form(self) -> None:
        def push(name: str, val: str) -> None:
            v = val.strip()
            if v:
                os.environ[name] = v
            else:
                os.environ.pop(name, None)

        push("GEMINI_API_KEY", self._llm_gemini_key.get())
        push("OPENAI_API_KEY", self._llm_openai_key.get())
        push("GROQ_API_KEY", self._llm_groq_key.get())
        push("ANTHROPIC_API_KEY", self._llm_anthropic_key.get())
        push("XAI_API_KEY", self._llm_xai_key.get())
        push("GEMINI_MODEL", self._llm_gemini_model.get())
        push("OPENAI_MODEL", self._llm_openai_model.get())
        push("OPENAI_BASE_URL", self._llm_openai_base.get())
        push("GROQ_MODEL", self._llm_groq_model.get())
        push("ANTHROPIC_MODEL", self._llm_anthropic_model.get())
        push("XAI_MODEL", self._llm_xai_model.get())
        os.environ["LLM_PROVIDER"] = _norm_llm_provider(self.llm_provider_var.get())

    def _save_llm_keys_to_local_file(self) -> None:
        path = ROOT / "local_api_keys.py"
        pairs: list[tuple[str, str]] = []
        for name, var in (
            ("GEMINI_API_KEY", self._llm_gemini_key),
            ("OPENAI_API_KEY", self._llm_openai_key),
            ("GROQ_API_KEY", self._llm_groq_key),
            ("ANTHROPIC_API_KEY", self._llm_anthropic_key),
            ("XAI_API_KEY", self._llm_xai_key),
            ("GEMINI_MODEL", self._llm_gemini_model),
            ("OPENAI_MODEL", self._llm_openai_model),
            ("OPENAI_BASE_URL", self._llm_openai_base),
            ("GROQ_MODEL", self._llm_groq_model),
            ("ANTHROPIC_MODEL", self._llm_anthropic_model),
            ("XAI_MODEL", self._llm_xai_model),
        ):
            v = var.get().strip()
            if v:
                pairs.append((name, v))
        pairs.append(("LLM_PROVIDER", _norm_llm_provider(self.llm_provider_var.get())))
        lines = [
            "# -*- coding: utf-8 -*-\n",
            "# 勿将本文件提交到公开仓库或分享 zip。\n\n",
        ]
        for name, v in pairs:
            lines.append(f"{name} = {repr(v)}\n")
        if len(lines) <= 3:
            lines.append("# （当前无已填项；可在上方 GUI 填写后保存）\n")
        path.write_text("".join(lines), encoding="utf-8")

    def rebuild_report_chat_panel(self) -> None:
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        if getattr(self, "report_agent_panel", None) is not None:
            old = self.report_agent_panel
            self.agent_session.detach(old)
            try:
                old.destroy()
            except tk.TclError:
                pass
        self.report_agent_panel = AgentChatPanel(
            self._report_chat_parent, self, self.agent_session, compact=True
        )
        self.report_agent_panel.pack(fill=tk.BOTH, expand=True)

    def _apply_llm_clicked(self, *, save_file: bool) -> None:
        self._sync_llm_env_from_form()
        _save_gui_llm_provider_pref(self.llm_provider_var.get())
        self.agent_session.clear()
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        self.rebuild_report_chat_panel()
        if save_file:
            try:
                self._save_llm_keys_to_local_file()
            except OSError as e:
                messagebox.showerror("保存失败", str(e))
                return
        messagebox.showinfo(
            "已应用",
            "已写入环境变量并刷新「分析报告」页底部对话区。"
            + (" 已保存到 local_api_keys.py。" if save_file else ""),
        )

    def _build_api_settings_page(self, pad: int) -> None:
        outer = ttk.Frame(self.frame_api, padding=pad)
        outer.pack(fill=tk.BOTH, expand=True)

        # 底部按钮先占位，避免窗口较矮时滚到视口外；中间区域单独滚动
        btns = tk.Frame(outer, bg=T_PAGE)
        btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        sec_style = getattr(self, "_secondary_button_style", "TButton")
        ttk.Button(
            btns,
            text="应用到当前会话（刷新对话区）",
            command=lambda: self._apply_llm_clicked(save_file=False),
            style=sec_style,
        ).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(
            btns,
            text="应用并保存到 local_api_keys.py",
            command=lambda: self._apply_llm_clicked(save_file=True),
            style=self._run_button_style,
        ).pack(side=tk.LEFT)
        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(
            side=tk.BOTTOM, fill=tk.X, pady=(0, 10)
        )

        scroll_host = ttk.Frame(outer)
        scroll_host.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        scroll_host.grid_rowconfigure(0, weight=1)
        scroll_host.grid_columnconfigure(0, weight=1)

        y_inc = max(18, int(round(self._font_content[1] * 2.4)))
        api_canvas = tk.Canvas(
            scroll_host,
            highlightthickness=0,
            borderwidth=0,
            bg=T_PAGE,
            yscrollincrement=y_inc,
        )
        api_vsb = tk.Scrollbar(
            scroll_host,
            orient=tk.VERTICAL,
            command=api_canvas.yview,
            width=self._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        api_canvas.configure(yscrollcommand=api_vsb.set)
        api_canvas.grid(row=0, column=0, sticky="nsew")
        api_vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(api_canvas, bg=T_PAGE)
        _api_inner_win = api_canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        def _api_canvas_on_cfg(event: tk.Event) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 1:
                api_canvas.itemconfigure(_api_inner_win, width=w)

        def _api_inner_on_cfg(_event: object | None = None) -> None:
            api_canvas.configure(scrollregion=api_canvas.bbox("all"))

        api_canvas.bind("<Configure>", _api_canvas_on_cfg)
        inner.bind("<Configure>", lambda _e: _api_inner_on_cfg())

        def _api_wheel(event: tk.Event) -> str | None:
            lines = self._text_wheel_lines
            d = getattr(event, "delta", 0) or 0
            if d:
                if sys.platform == "win32":
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                else:
                    steps = int(-d * lines / 120.0)
                    if steps == 0:
                        steps = -lines if d > 0 else lines
                if steps:
                    api_canvas.yview_scroll(steps, "units")
                return "break"
            n = getattr(event, "num", 0)
            if n == 4:
                api_canvas.yview_scroll(-lines, "units")
                return "break"
            if n == 5:
                api_canvas.yview_scroll(lines, "units")
                return "break"
            return None

        def _api_bind_wheel(w: tk.Misc) -> None:
            w.bind("<MouseWheel>", _api_wheel, add="+")
            w.bind("<Button-4>", _api_wheel, add="+")
            w.bind("<Button-5>", _api_wheel, add="+")
            for c in w.winfo_children():
                _api_bind_wheel(c)

        hint_wrap = int(680 * self._geom_scale)
        hint_small_pt = max(8, min(10, int(round(8 * self._font_soft))))
        hint_small: tuple[str, int] = (self._font_ui[0], hint_small_pt)
        title_pt = max(12, min(15, int(self._font_ui[1]) + 2))

        head = tk.Frame(inner, bg=T_PAGE)
        head.pack(fill=tk.X, pady=(0, 2))
        tk.Label(
            head,
            text="API 与模型",
            font=(self._font_ui[0], title_pt, "bold"),
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        tk.Label(
            head,
            text="配置密钥与接口后，「提取与分析」与「分析报告」中的对话将按下方首选平台调用。",
            font=hint_small,
            bg=T_PAGE,
            fg=API_SECTION_MUTED,
            wraplength=hint_wrap,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(6, 0))

        info_outer = tk.Frame(inner, bg=T_PAGE)
        info_outer.pack(fill=tk.X, pady=(14, 16))
        info = tk.Frame(
            info_outer,
            bg=API_INFO_BG,
            highlightbackground=API_INFO_BORDER,
            highlightthickness=1,
        )
        info.pack(fill=tk.X)
        tk.Label(
            info,
            text=(
                "· API Key 以密文显示。\n"
                "· 选「自动」时按 Gemini → OpenAI → Groq → Anthropic → xAI 顺序，使用第一个已填 Key 的平台。\n"
                "· 「应用」会记住首选平台（local_llm_prefs.json）；「应用并保存」同时写入 local_api_keys.py。\n"
                "· 模型 ID 可留空则用各平台默认；OpenAI 兼容网关请填写「API 根 URL」。"
            ),
            bg=API_INFO_BG,
            fg=T_TEXT,
            font=hint_small,
            wraplength=max(320, hint_wrap - 32),
            justify=tk.LEFT,
            anchor=tk.NW,
        ).pack(anchor=tk.W, padx=16, pady=14)

        prov_shell = tk.Frame(inner, bg=T_PAGE)
        prov_shell.pack(fill=tk.X, pady=(0, 12))
        prov_card = tk.Frame(
            prov_shell,
            bg=T_PANEL,
            highlightbackground=T_BORDER,
            highlightthickness=1,
        )
        prov_card.pack(fill=tk.X)
        prov_inner = tk.Frame(prov_card, bg=T_PANEL)
        prov_inner.pack(fill=tk.X, padx=18, pady=16)
        tk.Label(
            prov_inner,
            text="首选提供商",
            font=(self._font_ui[0], self._font_ui[1], "bold"),
            bg=T_PANEL,
            fg=T_TEXT,
        ).pack(anchor=tk.W)
        prov_row = tk.Frame(prov_inner, bg=T_PANEL)
        prov_row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(
            prov_row,
            text="分析 / 对话使用",
            bg=T_PANEL,
            fg=T_TEXT,
            font=self._font_ui,
        ).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Combobox(
            prov_row,
            textvariable=self.llm_provider_var,
            values=LLM_PROVIDER_COMBO_VALUES,
            state="readonly",
            width=20,
        ).pack(side=tk.LEFT)
        tk.Label(
            prov_row,
            text="auto=自动；指定平台时请确保该平台 Key 已填写。",
            bg=T_PANEL,
            fg=API_SECTION_MUTED,
            font=hint_small,
        ).pack(side=tk.LEFT, padx=(16, 0))

        sec = tk.Frame(inner, bg=T_PAGE)
        sec.pack(fill=tk.X, pady=(8, 12))
        tk.Label(
            sec,
            text="各平台凭据",
            font=(self._font_ui[0], self._font_ui[1], "bold"),
            bg=T_PAGE,
            fg=T_TEXT,
        ).pack(side=tk.LEFT)
        tk.Label(
            sec,
            text="（按需填写）",
            bg=T_PAGE,
            fg=API_SECTION_MUTED,
            font=hint_small,
        ).pack(side=tk.LEFT, padx=(10, 0))

        model_entry_w = 30

        def provider_block(
            parent: tk.Misc,
            title: str,
            key_var: tk.StringVar,
            model_var: tk.StringVar,
            model_hint: str,
            extra_var: tk.StringVar | None = None,
            extra_lbl: str = "",
        ) -> None:
            shell = tk.Frame(parent, bg=T_PAGE)
            shell.pack(fill=tk.X, pady=(0, 12))
            card = tk.Frame(
                shell,
                bg=T_PANEL,
                highlightbackground=T_BORDER,
                highlightthickness=1,
            )
            card.pack(fill=tk.X)
            inner = tk.Frame(card, bg=T_PANEL)
            inner.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
            tk.Label(
                inner,
                text=title,
                font=(self._font_ui[0], self._font_ui[1], "bold"),
                bg=T_PANEL,
                fg=T_ACCENT,
                anchor=tk.W,
            ).pack(fill=tk.X)
            row = tk.Frame(inner, bg=T_PANEL)
            row.pack(fill=tk.X, pady=(12, 0))
            row.grid_columnconfigure(1, weight=1)
            tk.Label(
                row,
                text="API Key",
                bg=T_PANEL,
                fg=T_TEXT,
                font=self._font_ui,
            ).grid(row=0, column=0, sticky=tk.W, padx=(0, 12), pady=2)
            ttk.Entry(row, textvariable=key_var, show="•").grid(
                row=0, column=1, sticky=tk.EW, padx=(0, 20), pady=2
            )
            tk.Label(
                row,
                text="模型 ID",
                bg=T_PANEL,
                fg=T_TEXT,
                font=self._font_ui,
            ).grid(row=0, column=2, sticky=tk.W, padx=(0, 10), pady=2)
            ttk.Entry(row, textvariable=model_var, width=model_entry_w).grid(
                row=0, column=3, sticky=tk.W, pady=2
            )
            tk.Label(
                inner,
                text=model_hint,
                bg=T_PANEL,
                fg=API_SECTION_MUTED,
                font=hint_small,
                wraplength=max(280, hint_wrap - 48),
                justify=tk.LEFT,
            ).pack(anchor=tk.W, pady=(10, 0))
            if extra_var is not None and extra_lbl:
                tk.Label(
                    inner,
                    text=extra_lbl,
                    bg=T_PANEL,
                    fg=T_TEXT,
                    font=self._font_ui,
                ).pack(anchor=tk.W, pady=(14, 6))
                ttk.Entry(inner, textvariable=extra_var).pack(fill=tk.X)

        provider_block(
            inner,
            "Google Gemini",
            self._llm_gemini_key,
            self._llm_gemini_model,
            "留空则自动尝试 gemini-2.5-flash 等",
        )
        provider_block(
            inner,
            "OpenAI（GPT）",
            self._llm_openai_key,
            self._llm_openai_model,
            "默认 gpt-4o-mini",
            self._llm_openai_base,
            "API 根 URL（可选，默认 https://api.openai.com/v1）",
        )
        provider_block(
            inner,
            "Groq",
            self._llm_groq_key,
            self._llm_groq_model,
            "默认 llama-3.3-70b-versatile",
        )
        provider_block(
            inner,
            "Anthropic Claude",
            self._llm_anthropic_key,
            self._llm_anthropic_model,
            "默认 claude-3-5-haiku-20241022",
        )
        provider_block(
            inner,
            "xAI Grok",
            self._llm_xai_key,
            self._llm_xai_model,
            "默认 grok-2-latest；API 为 https://api.x.ai/v1",
        )

        _api_bind_wheel(inner)
        api_canvas.bind("<MouseWheel>", _api_wheel, add="+")
        api_canvas.bind("<Button-4>", _api_wheel, add="+")
        api_canvas.bind("<Button-5>", _api_wheel, add="+")

    def _sync_asr_symbol(self) -> None:
        """与说明同字号：未选 □、已选 ✔（不用系统小复选框，避免画成 X）。"""
        if self.asr_if_no_subs_var.get():
            self._asr_sym.configure(text="✔", fg=T_ACCENT)
        else:
            self._asr_sym.configure(text="□", fg=T_MUTED)

    def _show_view(self, key: str) -> None:
        if key not in self._views:
            return
        self._current_nav = key
        self._views[key].tkraise()
        for k, row in self._nav_rows.items():
            bg = NAV_ACTIVE if k == key else NAV_BG
            row.configure(bg=bg)
            self._nav_labels[k].configure(bg=bg)
        if key == "local_chat":
            try:
                self._local_chat_hub.on_tab_visible()
            except tk.TclError:
                pass

    def _setup_fonts_and_styles(self) -> str:
        """返回主操作按钮的 ttk style 名。"""
        fs = self._font_soft
        ui_pt = max(9, min(11, int(round(9 * fs))))
        content_pt = max(9, min(12, int(round(10 * fs))))
        mono_pt = max(9, min(12, int(round(10 * fs))))

        sans_ui = _pick_sans_cjk(self)
        self._font_ui = (sans_ui, ui_pt)
        self._font_content: tuple[str, int] = (sans_ui, content_pt)
        self._font_log: tuple[str, int] = (_pick_mono_family(self), mono_pt)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        self._clam_bg = T_PAGE

        try:
            style.configure(".", background=T_PAGE, foreground=T_TEXT, fieldbackground=T_ENTRY)
            style.configure("TFrame", background=T_PAGE)
            style.configure("TLabel", background=T_PAGE, foreground=T_TEXT)
            style.configure(
                "TLabelframe",
                background=T_PAGE,
                foreground=T_MUTED,
                borderwidth=1,
                relief="solid",
            )
            style.configure("TLabelframe.Label", background=T_PAGE, foreground=T_MUTED)
            style.configure("TNotebook", background=T_PAGE, borderwidth=0)
            style.configure("TNotebook.Tab", background=T_RAISED, foreground=T_MUTED)
            style.map(
                "TNotebook.Tab",
                background=[("selected", T_PANEL)],
                foreground=[("selected", T_TEXT)],
            )
            style.configure("TEntry", fieldbackground=T_ENTRY, foreground=T_TEXT, bordercolor=T_BORDER)
            style.configure("TSeparator", background=T_BORDER)
            style.configure(
                "TButton",
                font=self._font_ui,
                background=T_RAISED,
                foreground=T_TEXT,
                borderwidth=1,
                relief="flat",
                padding=(10, 5),
            )
            style.map(
                "TButton",
                background=[("active", T_BORDER), ("disabled", T_RAISED)],
                foreground=[("disabled", T_MUTED)],
            )
        except tk.TclError:
            pass

        for name in ("TLabel", "TButton", "TNotebook.Tab", "TEntry", "TLabelframe", "TLabelframe.Label"):
            try:
                style.configure(name, font=self._font_ui)
            except tk.TclError:
                pass

        tab_pad_x = max(10, int(round(10 * fs)))
        tab_pad_y = max(4, int(round(5 * fs)))
        try:
            style.configure("TNotebook.Tab", padding=[tab_pad_x, tab_pad_y])
        except tk.TclError:
            pass

        btn_pad_x = max(10, int(round(12 * fs)))
        btn_pad_y = max(4, int(round(5 * fs)))
        try:
            style.configure(
                "Accent.TButton",
                font=self._font_ui,
                foreground="#ffffff",
                background=T_ACCENT,
                borderwidth=0,
                focusthickness=3,
                focuscolor="none",
                padding=(btn_pad_x, btn_pad_y),
            )
            style.map(
                "Accent.TButton",
                background=[("active", T_ACCENT_ACTIVE), ("disabled", T_RAISED)],
                foreground=[("disabled", T_MUTED)],
            )
            style.configure(
                "Secondary.TButton",
                font=self._font_ui,
                background=T_PANEL,
                foreground=T_TEXT,
                borderwidth=1,
                relief="flat",
                padding=(btn_pad_x, btn_pad_y),
            )
            style.map(
                "Secondary.TButton",
                background=[("active", T_RAISED), ("disabled", T_PANEL)],
                foreground=[("disabled", T_MUTED)],
            )
            self._secondary_button_style = "Secondary.TButton"
            return "Accent.TButton"
        except tk.TclError:
            try:
                style.configure("TButton", padding=(btn_pad_x, btn_pad_y))
            except tk.TclError:
                pass
            self._secondary_button_style = "TButton"
            return "TButton"

    def _build_text_view(self, parent: tk.Misc, *, mono: bool) -> tk.Text:
        pad_txt = max(8, int(round(8 * self._font_soft)))
        inner = ttk.Frame(parent)
        inner.pack(fill=tk.BOTH, expand=True)

        font = self._font_log if mono else self._font_content
        if not mono:
            pad_txt = max(pad_txt, int(round(12 * self._font_soft)))
        txt = tk.Text(
            inner,
            wrap=tk.WORD,
            font=font,
            undo=False,
            padx=pad_txt,
            pady=pad_txt,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=T_BORDER,
            highlightcolor=T_ACCENT,
            bg=T_PANEL,
            fg=T_TEXT,
            insertbackground=T_TEXT,
            selectbackground=T_SELECT,
        )
        scroll = make_text_y_scrollbar(inner, txt, width_px=self._scrollbar_px)
        bind_text_mousewheel(txt, lines_per_notch=self._text_wheel_lines)
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        return txt

    def _configure_report_text_tags(self, txt: tk.Text) -> None:
        fam, sz = self._font_content[0], self._font_content[1]
        sz_i = int(round(sz))
        txt.tag_configure(
            "report_title",
            font=(fam, min(sz_i + 4, 22), "bold"),
            foreground=T_ACCENT,
            spacing1=2,
            spacing3=8,
        )
        txt.tag_configure(
            "report_h1",
            font=(fam, min(sz_i + 2, 18), "bold"),
            foreground="#111827",
            spacing1=10,
            spacing3=4,
        )
        txt.tag_configure(
            "report_subhead",
            font=(fam, min(sz_i + 1, 17), "bold"),
            foreground="#374151",
            spacing1=8,
            spacing3=2,
        )
        txt.tag_configure(
            "report_meta",
            font=(fam, max(8, sz_i - 1)),
            foreground=T_MUTED,
        )

    def _set_report_editor_content(self, raw: str) -> None:
        txt = self.report_txt
        txt.delete("1.0", tk.END)
        content = sanitize_analysis_display(raw)
        for line in content.splitlines(keepends=True):
            if line.endswith("\n"):
                core, nl = line[:-1], "\n"
            else:
                core, nl = line, ""
            tag = report_line_style_tag(core)
            if tag:
                txt.insert(tk.END, core + nl, (tag,))
            else:
                txt.insert(tk.END, core + nl)

    def start_run(self) -> None:
        if self._busy:
            return
        url = self.url_var.get().strip()
        if not is_valid_task_source(url):
            messagebox.showwarning(
                "提示",
                "请输入 B 站 https 链接，或选择/粘贴本机支持的音视频文件路径。\n"
                "常见后缀：mp3、m4a、wav、flac、mp4、mkv、webm 等。",
            )
            return

        self._busy = True
        self.run_btn.configure(state=tk.DISABLED)
        p_try = Path(os.path.expanduser(url.strip('"')))
        local_run = bool(p_try.is_file() and is_supported_local_media(p_try))
        if local_run:
            self.status.configure(text="运行中…（本地转写，无需登录）")
        else:
            self.status.configure(text="运行中…（若弹出 Chromium，请完成登录）")
        self.log_txt.delete("1.0", tk.END)
        self.merged_txt.delete("1.0", tk.END)
        self.report_txt.delete("1.0", tk.END)

        def on_line(line: str) -> None:
            self.after(0, lambda: self._append_log(line))

        def on_done(code: int) -> None:
            self.after(0, lambda: self._finish_run(code))

        wm = self.whisper_model_var.get().strip()
        if wm not in WHISPER_MODEL_CHOICES:
            wm = default_whisper_model_choice()
        self._sync_llm_env_from_form()
        self.agent_session._provider = resolve_provider(self.llm_provider_var.get().strip())
        lp = self.llm_provider_var.get().strip() or "auto"
        run_pipeline(
            url,
            on_line,
            on_done,
            asr_if_no_subs=bool(self.asr_if_no_subs_var.get()),
            whisper_model=wm,
            llm_provider=lp,
        )

    def _append_log(self, line: str) -> None:
        self.log_txt.insert(tk.END, line)
        self.log_txt.see(tk.END)

    def _finish_run(self, code: int) -> None:
        self._busy = False
        self.run_btn.configure(state=tk.NORMAL)
        self.reload_files()
        if code == 0:
            self.status.configure(text="完成")
        else:
            self.status.configure(text=f"进程退出码：{code}（请查看运行日志）")
            messagebox.showerror(
                "未完成",
                f"命令退出码 {code}，请在本页下方「运行日志」中查看详情。",
            )

    def reload_files(self) -> None:
        if MERGED.is_file():
            self.merged_txt.delete("1.0", tk.END)
            self.merged_txt.insert(
                "1.0", MERGED.read_text(encoding="utf-8", errors="replace")
            )
        if ANALYSIS.is_file():
            self._set_report_editor_content(
                ANALYSIS.read_text(encoding="utf-8", errors="replace")
            )


def _resolve_ollama_executable() -> str | None:
    """PATH + Windows 常见安装目录（从资源管理器启动 GUI 时 PATH 往往不含 Ollama）。"""
    for name in ("ollama", "ollama.exe"):
        w = shutil.which(name)
        if w:
            return w
    if sys.platform != "win32":
        return None
    la = os.environ.get("LOCALAPPDATA", "").strip()
    pf = os.environ.get("ProgramFiles", "").strip()
    pfx86 = os.environ.get("ProgramFiles(x86)", "").strip()
    for base in (
        Path(la) / "Programs" / "Ollama" if la else None,
        Path(pf) / "Ollama" if pf else None,
        Path(pfx86) / "Ollama" if pfx86 else None,
    ):
        if base is None:
            continue
        for exe in ("ollama.exe", "Ollama.exe"):
            cand = base / exe
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                pass
    return None


class LocalInferenceServerPanel(ttk.Frame):
    """在「本地对话」页内启动本机推理进程（如 ollama），显示输出并可选回填服务 URL。"""

    _PRESET_OLLAMA = "ollama"
    _PRESET_LMSTUDIO = "lmstudio"
    _PRESET_CUSTOM = "custom"
    _OLLAMA_BASE = "http://127.0.0.1:11434/v1"

    def __init__(self, parent: tk.Misc, chat_hub: object) -> None:
        super().__init__(parent)
        self._chat_hub = chat_hub

        self._proc: subprocess.Popen | None = None
        pre = self._load_prefs()
        self._preset_var = tk.StringVar(value=str(pre.get("local_inf_preset") or self._PRESET_OLLAMA))
        self._exe_var = tk.StringVar(value=str(pre.get("local_inf_exe") or ""))
        self._args_var = tk.StringVar(value=str(pre.get("local_inf_args") or ""))
        self._cwd_var = tk.StringVar(value=str(pre.get("local_inf_cwd") or ""))
        self._autofill_var = tk.BooleanVar(value=bool(pre.get("local_inf_autofill", True)))
        self._autofill_url_var = tk.StringVar(
            value=_local_openai_base_for_ui(str(pre.get("local_inf_autofill_url") or ""))
        )

        app = chat_hub._app
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            outer,
            text="在此启动的子进程日志会显示在下方。切换回「对话」标签不会停止服务；关闭主程序时子进程将结束。",
            wraplength=700,
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(0, 10))

        pf = ttk.LabelFrame(outer, text="方式", padding=(8, 6))
        pf.pack(fill=tk.X, pady=(0, 8))
        for val, label in (
            (self._PRESET_OLLAMA, "Ollama：执行 ollama serve（API 一般为 http://127.0.0.1:11434/v1）"),
            (self._PRESET_LMSTUDIO, "仅打开 LM Studio（需在软件内点击 Start Server）"),
            (self._PRESET_CUSTOM, "自定义：可执行文件 + 参数（如 llama-server、其他 OpenAI 兼容服务）"),
        ):
            ttk.Radiobutton(
                pf, text=label, value=val, variable=self._preset_var, command=self._sync_preset_ui
            ).pack(anchor=tk.W, pady=2)

        self._custom_fr = ttk.Frame(outer)
        r1 = ttk.Frame(self._custom_fr)
        r1.pack(fill=tk.X)
        ttk.Label(r1, text="程序", width=8).pack(side=tk.LEFT)
        ttk.Entry(r1, textvariable=self._exe_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
        ttk.Button(r1, text="浏览…", command=self._browse_exe, width=8).pack(side=tk.LEFT)
        r2 = ttk.Frame(self._custom_fr)
        r2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r2, text="参数").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self._args_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        r3 = ttk.Frame(self._custom_fr)
        r3.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(r3, text="工作目录").pack(side=tk.LEFT)
        ttk.Entry(r3, textvariable=self._cwd_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        af = ttk.Frame(outer)
        af.pack(fill=tk.X, pady=(0, 8))
        ttk.Checkbutton(
            af, text="启动成功后写入「本地对话」页的服务 URL", variable=self._autofill_var
        ).pack(anchor=tk.W)
        ttk.Label(
            af,
            text=(
                "自定义 URL（仅「自定义」预设使用；Ollama 固定 11434/v1；"
                "Gemma 4-bit 默认同 Vision 副本：18090/v1）"
            ),
            foreground=T_MUTED,
        ).pack(anchor=tk.W, pady=(4, 0))
        ttk.Entry(af, textvariable=self._autofill_url_var).pack(fill=tk.X, pady=(2, 0))

        bar = ttk.Frame(outer)
        bar.pack(fill=tk.X, pady=(0, 8))
        self._btn_start = ttk.Button(bar, text="启动", command=self._start)
        self._btn_start.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_stop = ttk.Button(bar, text="停止", command=self._stop, state=tk.DISABLED)
        self._btn_stop.pack(side=tk.LEFT)
        self._status = ttk.Label(bar, text="未运行")
        self._status.pack(side=tk.LEFT, padx=(16, 0))

        log_fr = ttk.Frame(outer)
        log_fr.pack(fill=tk.BOTH, expand=True)
        ttk.Label(log_fr, text="进程输出").pack(anchor=tk.W)
        sb = ttk.Scrollbar(log_fr)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log = tk.Text(
            log_fr,
            height=14,
            wrap=tk.WORD,
            font=app._font_log,
            bg=T_PANEL,
            fg=T_TEXT,
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=T_BORDER,
        )
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.configure(command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        bind_text_mousewheel(self._log, lines_per_notch=app._text_wheel_lines)

        self._sync_preset_ui()
        self.bind("<Destroy>", self._on_panel_destroy)

    def _on_panel_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass
        self._save_prefs()

    def _load_prefs(self) -> dict:
        try:
            if LLM_GUI_PREF_JSON.is_file():
                raw = json.loads(LLM_GUI_PREF_JSON.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return {}

    def _save_prefs(self) -> None:
        m = self._load_prefs()
        m.update(
            {
                "local_inf_preset": self._preset_var.get(),
                "local_inf_exe": self._exe_var.get().strip(),
                "local_inf_args": self._args_var.get().strip(),
                "local_inf_cwd": self._cwd_var.get().strip(),
                "local_inf_autofill": self._autofill_var.get(),
                "local_inf_autofill_url": self._autofill_url_var.get().strip(),
            }
        )
        try:
            LLM_GUI_PREF_JSON.write_text(
                json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass

    def _sync_preset_ui(self) -> None:
        p = self._preset_var.get()
        if p == self._PRESET_CUSTOM:
            self._custom_fr.pack(fill=tk.X, pady=(0, 6))
        else:
            self._custom_fr.pack_forget()

    def _browse_exe(self) -> None:
        p = filedialog.askopenfilename(
            parent=self.winfo_toplevel(),
            title="选择可执行文件",
            filetypes=[("可执行文件", "*.exe;*.bat;*.cmd"), ("全部", "*.*")],
        )
        if p:
            self._exe_var.set(p)

    def _log_line(self, s: str) -> None:
        try:
            self._log.insert(tk.END, s)
            self._log.see(tk.END)
        except tk.TclError:
            pass

    def _set_status(self, t: str) -> None:
        try:
            self._status.configure(text=t)
        except tk.TclError:
            pass

    def _launch_lm_studio(self) -> None:
        cands = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "LM Studio" / "LM Studio.exe",
            Path(os.environ.get("ProgramFiles", "")) / "LM Studio" / "LM Studio.exe",
            Path(os.environ.get("ProgramFiles(x86)", "")) / "LM Studio" / "LM Studio.exe",
        ]
        for p in cands:
            if p.is_file():
                try:
                    subprocess.Popen([str(p)], cwd=str(p.parent))
                except OSError as e:
                    messagebox.showerror("失败", str(e), parent=self.winfo_toplevel())
                    return
                self._log_line(
                    f"[gui] 已启动 LM Studio：{p}\n请在软件内开启 Local Server，再把地址填到「服务 URL」。\n"
                )
                self._set_status("已打开 LM Studio")
                return
        messagebox.showinfo(
            "未找到",
            "未在常见安装路径找到 LM Studio.exe。请手动打开 LM Studio，或改用 Ollama / 自定义。",
            parent=self.winfo_toplevel(),
        )

    def _start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            messagebox.showinfo(
                "提示", "当前已有进程在运行，请先停止。", parent=self.winfo_toplevel()
            )
            return
        preset = self._preset_var.get()
        cmd: list[str]
        cwd: str | None = None
        if preset == self._PRESET_OLLAMA:
            exe = _resolve_ollama_executable()
            if not exe:
                messagebox.showerror(
                    "未找到 ollama",
                    "未在系统 PATH 及常见安装目录中找到 ollama.exe。\n"
                    "若已安装 Ollama，可改用「自定义」：程序选 ollama.exe，参数填 serve。\n"
                    "安装见 https://ollama.com",
                    parent=self.winfo_toplevel(),
                )
                return
            cmd = [exe, "serve"]
        elif preset == self._PRESET_LMSTUDIO:
            self._save_prefs()
            self._launch_lm_studio()
            return
        else:
            exe = self._exe_var.get().strip().strip('"')
            if not exe:
                messagebox.showwarning(
                    "提示", "请填写可执行文件路径。", parent=self.winfo_toplevel()
                )
                return
            if not Path(exe).is_file():
                messagebox.showwarning(
                    "提示", f"找不到文件：{exe}", parent=self.winfo_toplevel()
                )
                return
            arg_s = self._args_var.get().strip()
            try:
                parts = shlex.split(arg_s, posix=(os.name != "nt")) if arg_s else []
            except ValueError as e:
                messagebox.showerror(
                    "参数解析失败", str(e), parent=self.winfo_toplevel()
                )
                return
            cmd = [exe] + parts
            cw = self._cwd_var.get().strip()
            if cw:
                if not Path(cw).is_dir():
                    messagebox.showwarning(
                        "提示", f"工作目录无效：{cw}", parent=self.winfo_toplevel()
                    )
                    return
                cwd = cw

        self._save_prefs()
        kw: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
        }
        if cwd:
            kw["cwd"] = cwd
        if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._proc = subprocess.Popen(cmd, **kw)
        except OSError as e:
            messagebox.showerror("启动失败", str(e), parent=self.winfo_toplevel())
            return

        threading.Thread(target=self._drain_stdout, daemon=True).start()
        self._btn_stop.configure(state=tk.NORMAL)
        self._btn_start.configure(state=tk.DISABLED)
        self._set_status(f"运行中 · PID {self._proc.pid}")
        self._log_line("[gui] 已启动: " + " ".join(cmd) + "\n")
        if self._autofill_var.get():
            if preset == self._PRESET_OLLAMA:
                url = self._OLLAMA_BASE
            else:
                url = self._autofill_url_var.get().strip().rstrip("/")
            if url:

                def _apply(u: str = url) -> None:
                    self._chat_hub._loc_base.set(u)

                self.after(500, _apply)

    def _drain_stdout(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")

                def _append(t: str = text) -> None:
                    self._log_line(t)

                self.after(0, _append)
        finally:
            self.after(0, self._on_proc_finished)

    def _on_proc_finished(self) -> None:
        code = None
        if self._proc is not None:
            code = self._proc.poll()
        self._proc = None
        try:
            self._btn_stop.configure(state=tk.DISABLED)
            self._btn_start.configure(state=tk.NORMAL)
        except tk.TclError:
            pass
        self._set_status("未运行" if code is None else f"已退出 · 代码 {code}")
        self._log_line(f"\n[gui] 进程已结束，返回码 {code}\n")

    def _stop(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            if self._proc:
                self._proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                if self._proc:
                    self._proc.kill()
            except OSError:
                pass


class LocalChatHub(ttk.Frame):
    """本地 OpenAI 兼容对话：侧栏会话历史、多轮记忆、图片附件，风格贴近网页聊天。"""

    def __init__(self, parent: tk.Misc, app: App) -> None:
        super().__init__(parent)
        self._app = app
        self._busy = False
        self._current_sid: str | None = None
        self._meta: dict = {}
        self._pending_names: list[str] = []
        self._lb_ids: list[str] = []
        self._typing_row: tk.Frame | None = None
        self._wrap_labels: list[tuple[tk.Misc, str]] = []
        self._title_var = tk.StringVar(value="新对话")
        self._include_merged = tk.BooleanVar(value=False)
        self._loc_base = tk.StringVar(value="")
        self._loc_model = tk.StringVar(value="")
        self._loc_key = tk.StringVar(value="")
        self._loc_stream = tk.BooleanVar(value=True)
        self._stream_pending = ""
        self._stream_after_id: str | None = None
        self._stream_body: tk.Text | None = None

        fam, sz = app._font_content[0], int(round(app._font_content[1]))
        self._font_small = (fam, max(8, sz - 1))

        top_nb = ttk.Notebook(self)
        top_nb.pack(fill=tk.BOTH, expand=True)
        tab_chat = ttk.Frame(top_nb, padding=0)
        tab_srv = ttk.Frame(top_nb, padding=0)
        top_nb.add(tab_chat, text=" 对话 ")
        top_nb.add(tab_srv, text=" 本机推理服务 ")
        self._local_hub_nb = top_nb
        LocalInferenceServerPanel(tab_srv, chat_hub=self).pack(
            fill=tk.BOTH, expand=True, padx=0, pady=0
        )

        shell = tk.Frame(tab_chat, bg=T_PAGE)
        shell.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(shell, bg=NAV_BG, width=int(236 * min(app._geom_scale, 1.35)))
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)
        right = tk.Frame(shell, bg=T_PAGE)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        right_body = tk.Frame(right, bg=T_PAGE)
        right_body.pack(fill=tk.BOTH, expand=True, padx=(10, 16), pady=(10, 12))
        pv = ttk.Panedwindow(right_body, orient=tk.VERTICAL)
        pv.pack(fill=tk.BOTH, expand=True)
        settings_pane = tk.Frame(pv, bg=T_PAGE)
        chat_stack = tk.Frame(pv, bg=T_PAGE)
        chat_stack.grid_rowconfigure(0, weight=1)
        chat_stack.grid_columnconfigure(0, weight=1)
        pv.add(settings_pane, weight=0)
        pv.add(chat_stack, weight=1)

        tk.Label(
            left,
            text="会话",
            font=(app._font_ui[0], max(8, int(round(9 * app._font_soft))), "bold"),
            bg=NAV_BG,
            fg=T_MUTED,
        ).pack(anchor=tk.W, padx=12, pady=(14, 6))
        nb_row = tk.Frame(left, bg=NAV_BG)
        nb_row.pack(fill=tk.X, padx=8, pady=(0, 6))
        ttk.Button(nb_row, text="＋ 新对话", command=self._new_session).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4)
        )
        ttk.Button(nb_row, text="删除", command=self._delete_current, width=6).pack(
            side=tk.LEFT
        )
        lb_fr = tk.Frame(left, bg=NAV_BG)
        lb_fr.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 12))
        sb_l = tk.Scrollbar(
            lb_fr,
            orient=tk.VERTICAL,
            width=app._scrollbar_px,
            troughcolor=SCROLL_TROUGH,
        )
        self._listbox = tk.Listbox(
            lb_fr,
            activestyle="dotbox",
            bg=NAV_ACTIVE,
            fg=T_TEXT,
            selectbackground=T_SELECT,
            highlightthickness=0,
            borderwidth=0,
            font=app._font_ui,
        )
        self._listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb_l.pack(side=tk.RIGHT, fill=tk.Y)
        self._listbox.configure(yscrollcommand=sb_l.set)
        sb_l.configure(command=self._listbox.yview)
        self._listbox.bind("<<ListboxSelect>>", self._on_sidebar_select)

        head = tk.Frame(settings_pane, bg=T_PAGE)
        head.pack(fill=tk.X, pady=(0, 10))
        tk.Label(head, text="标题", bg=T_PAGE, fg=T_MUTED, font=self._font_small).pack(
            side=tk.LEFT, padx=(0, 10)
        )
        ttk.Entry(head, textvariable=self._title_var, width=36).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self._title_var.trace_add("write", lambda *_: self._debounced_save_title())

        api_row = ttk.Frame(settings_pane, padding=(0, 0, 0, 4))
        api_row.pack(fill=tk.X)
        row_gap = max(8, int(round(10 * app._font_soft)))
        ttk.Label(api_row, text="服务 URL（须含 /v1）").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(api_row, textvariable=self._loc_base, width=52).grid(
            row=0, column=1, sticky="ew", padx=(10, 0)
        )
        ttk.Label(api_row, text="模型 ID").grid(row=1, column=0, sticky=tk.W, pady=(row_gap, 0))
        ttk.Entry(api_row, textvariable=self._loc_model, width=28).grid(
            row=1, column=1, sticky="ew", padx=(10, 0), pady=(row_gap, 0)
        )
        ttk.Label(api_row, text="API Key").grid(row=2, column=0, sticky=tk.W, pady=(row_gap, 0))
        ttk.Entry(api_row, textvariable=self._loc_key, width=36, show="•").grid(
            row=2, column=1, sticky="ew", padx=(10, 0), pady=(row_gap, 0)
        )
        api_row.columnconfigure(1, weight=1)
        ttk.Label(
            settings_pane,
            text=(
                f"Gemma 4-bit（Vision 目录 SERVE_GEMMA4_4BIT.bat）：URL {DEFAULT_LOCAL_OPENAI_BASE} ，"
                f"模型 {DEFAULT_LOCAL_OPENAI_MODEL_ID}"
            ),
            font=self._font_small,
            foreground=T_MUTED,
            wraplength=int(min(920, 520 + 220 * app._geom_scale)),
        ).pack(anchor=tk.W, pady=(2, 4))
        srv_row = ttk.Frame(settings_pane)
        srv_row.pack(fill=tk.X, pady=(max(4, row_gap // 2), 0))
        ttk.Button(
            srv_row,
            text="切换到「本机推理服务」标签…",
            command=self._show_local_inference_tab,
        ).pack(anchor=tk.W)
        hint_pt = max(8, min(10, int(round(8 * app._font_soft))))
        ttk.Label(
            settings_pane,
            text="与网页版类似：左侧切换历史；点「＋」贴图；记忆含本会话多轮，可选附带合并文稿。",
            font=(app._font_ui[0], hint_pt),
            foreground=T_MUTED,
            wraplength=int(min(920, 520 + 220 * app._geom_scale)),
        ).pack(anchor=tk.W, pady=(8, 8))

        opt_row = tk.Frame(settings_pane, bg=T_PAGE)
        opt_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Checkbutton(
            opt_row,
            text="附带 out/transcript_merged.txt 作背景（长上下文，适合追问视频）",
            variable=self._include_merged,
        ).pack(anchor=tk.W)
        ttk.Checkbutton(
            opt_row,
            text="流式输出（SSE，Vision 目录 Gemma serve 支持；其它服务若报错请取消勾选）",
            variable=self._loc_stream,
        ).pack(anchor=tk.W, pady=(4, 0))

        chat_row = tk.Frame(chat_stack, bg=T_PAGE)
        chat_row.grid(row=0, column=0, sticky="nsew", pady=(0, 10))
        chat_row.grid_rowconfigure(0, weight=1)
        chat_row.grid_columnconfigure(0, weight=1)
        y_inc = max(18, int(round(app._font_content[1] * 2.4)))
        self._chat_canvas = tk.Canvas(
            chat_row,
            bg=T_SURFACE,
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=y_inc,
        )
        sb = tk.Scrollbar(
            chat_row,
            command=self._chat_canvas.yview,
            width=app._scrollbar_px,
            troughcolor=SCROLL_TROUGH,
        )
        self._chat_canvas.configure(yscrollcommand=sb.set)
        self._msgs = tk.Frame(self._chat_canvas, bg=T_SURFACE)
        self._msg_win = self._chat_canvas.create_window((0, 0), window=self._msgs, anchor="nw")

        def _sync(_e: object | None = None) -> None:
            self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))

        def _on_cv_cfg(event: tk.Misc) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 8:
                self._chat_canvas.itemconfigure(self._msg_win, width=w)
                self._refresh_bubble_wrap(w)

        self._msgs.bind("<Configure>", lambda e: _sync())
        self._chat_canvas.bind("<Configure>", _on_cv_cfg)
        for w in (self._chat_canvas, self._msgs):
            w.bind("<MouseWheel>", self._on_chat_wheel)
            w.bind("<Button-4>", self._on_chat_wheel)
            w.bind("<Button-5>", self._on_chat_wheel)
        self._chat_canvas.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        comp_outer = tk.Frame(chat_stack, bg=T_PAGE)
        comp_outer.grid(row=1, column=0, sticky="ew")
        self._composer = tk.Frame(
            comp_outer,
            bg=A_COMPOSER,
            highlightbackground=A_CARD_BORDER,
            highlightthickness=1,
        )
        self._composer.pack(fill=tk.X, ipadx=2, ipady=2)
        top_c = tk.Frame(self._composer, bg=A_COMPOSER)
        top_c.pack(fill=tk.X, padx=6, pady=(8, 4))
        ttk.Button(top_c, text="＋", width=3, command=self._pick_images).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self._inp = tk.Text(
            top_c,
            height=4,
            wrap=tk.WORD,
            font=app._font_content,
            padx=10,
            pady=8,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=A_COMPOSER,
            fg=A_TEXT,
            insertbackground=A_TEXT,
        )
        self._inp.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        bind_text_mousewheel(self._inp, lines_per_notch=app._text_wheel_lines)
        self._attach_lbl = tk.Label(
            self._composer,
            text="",
            font=self._font_small,
            bg=A_COMPOSER,
            fg=A_META,
            anchor=tk.W,
        )
        self._attach_lbl.pack(fill=tk.X, padx=12, pady=(0, 4))
        bot_c = tk.Frame(self._composer, bg=A_COMPOSER)
        bot_c.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._status = tk.Label(
            bot_c,
            text="Ctrl+Enter 发送 · 图片将复制到会话目录",
            font=self._font_small,
            bg=A_COMPOSER,
            fg=A_META,
        )
        self._status.pack(side=tk.LEFT)
        ttk.Button(bot_c, text="清除附件", command=self._clear_pending).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(
            bot_c,
            text="发送",
            command=self._on_send,
            style=app._run_button_style,
            width=8,
        ).pack(side=tk.RIGHT)
        self._inp.bind("<Control-Return>", self._on_ctrl_enter)

        self._title_save_after: str | None = None

    def _show_local_inference_tab(self) -> None:
        try:
            self._local_hub_nb.select(1)
        except (tk.TclError, AttributeError):
            pass

    def on_tab_visible(self) -> None:
        self._sync_api_fields_from_app()
        self._refresh_sidebar()

    def _sync_api_fields_from_app(self) -> None:
        if not self._loc_base.get().strip():
            self._loc_base.set(
                _local_openai_base_for_ui(self._app._llm_openai_base.get().strip() or None)
            )
        else:
            cur = self._loc_base.get().strip()
            nxt = _maybe_rewrite_lm_studio_local_url(cur) or cur
            if nxt != cur:
                self._loc_base.set(nxt)
        if not self._loc_model.get().strip():
            self._loc_model.set(
                (self._app._llm_openai_model.get() or DEFAULT_LOCAL_OPENAI_MODEL_ID).strip()
            )
        if not self._loc_key.get().strip():
            self._loc_key.set(self._app._llm_openai_key.get().strip())

    def _debounced_save_title(self) -> None:
        if self._title_save_after:
            try:
                self._app.after_cancel(self._title_save_after)
            except tk.TclError:
                pass
        self._title_save_after = self._app.after(
            400, lambda: self._flush_title_to_meta()
        )

    def _flush_title_to_meta(self) -> None:
        self._title_save_after = None
        if not self._current_sid:
            return
        try:
            self._meta["title"] = self._title_var.get().strip() or "对话"
            self._save_meta()
            self._refresh_sidebar()
        except (OSError, TypeError, KeyError):
            pass

    def _session_dir(self, sid: str) -> Path:
        return LOCAL_CHAT_ROOT / sid

    def _meta_path(self, sid: str) -> Path:
        return self._session_dir(sid) / "meta.json"

    def _files_dir(self, sid: str) -> Path:
        return self._session_dir(sid) / "files"

    def _list_session_ids(self) -> list[str]:
        LOCAL_CHAT_ROOT.mkdir(parents=True, exist_ok=True)
        rows: list[tuple[str, str]] = []
        for p in LOCAL_CHAT_ROOT.iterdir():
            if not p.is_dir():
                continue
            mp = p / "meta.json"
            if not mp.is_file():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                rows.append((str(m.get("updated") or ""), p.name))
            except (OSError, json.JSONDecodeError):
                rows.append(("", p.name))
        rows.sort(key=lambda x: x[0], reverse=True)
        return [r[1] for r in rows]

    def _refresh_sidebar(self) -> None:
        self._lb_ids = self._list_session_ids()
        self._listbox.delete(0, tk.END)
        for sid in self._lb_ids:
            try:
                m = json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
                title = str(m.get("title") or sid)[:48]
            except (OSError, json.JSONDecodeError):
                title = sid
            self._listbox.insert(tk.END, title)
        if self._current_sid and self._current_sid in self._lb_ids:
            idx = self._lb_ids.index(self._current_sid)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.see(idx)

    def _select_sid_in_list(self, sid: str) -> None:
        if sid in self._lb_ids:
            idx = self._lb_ids.index(sid)
            self._listbox.selection_clear(0, tk.END)
            self._listbox.selection_set(idx)
            self._listbox.see(idx)

    def _on_sidebar_select(self, _event: object) -> None:
        sel = self._listbox.curselection()
        if not sel:
            return
        i = int(sel[0])
        if 0 <= i < len(self._lb_ids):
            self._load_session(self._lb_ids[i])

    def _new_session(self) -> None:
        sid = uuid.uuid4().hex[:12]
        d = self._session_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        self._files_dir(sid).mkdir(exist_ok=True)
        self._meta = {
            "id": sid,
            "title": "新对话",
            "updated": datetime.now().isoformat(timespec="seconds"),
            "include_merged": False,
            "turns": [],
        }
        self._meta_path(sid).write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._current_sid = sid
        self._title_var.set("新对话")
        self._include_merged.set(False)
        self._pending_names.clear()
        self._clear_chat_surface()
        self._refresh_sidebar()
        self._select_sid_in_list(sid)

    def _load_session(self, sid: str) -> None:
        try:
            self._meta = json.loads(self._meta_path(sid).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            messagebox.showerror("错误", "无法读取会话文件。")
            return
        self._current_sid = sid
        self._title_var.set(str(self._meta.get("title") or "对话"))
        self._include_merged.set(bool(self._meta.get("include_merged")))
        self._pending_names.clear()
        self._clear_chat_surface()
        for t in self._meta.get("turns") or []:
            imgs = t.get("imgs") or []
            self._add_user_bubble(str(t.get("u") or ""), [str(x) for x in imgs])
            self._add_assistant_bubble(str(t.get("a") or ""))
        self._scroll_bottom()

    def _save_meta(self) -> None:
        if not self._current_sid:
            return
        self._meta["id"] = self._current_sid
        self._meta["title"] = self._title_var.get().strip() or "对话"
        self._meta["include_merged"] = bool(self._include_merged.get())
        self._meta["updated"] = datetime.now().isoformat(timespec="seconds")
        self._meta.setdefault("turns", [])
        self._meta_path(self._current_sid).write_text(
            json.dumps(self._meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _ensure_session(self) -> str:
        if self._current_sid and self._meta_path(self._current_sid).is_file():
            return self._current_sid
        self._new_session()
        return self._current_sid or ""

    def _delete_current(self) -> None:
        if not self._current_sid:
            messagebox.showinfo("提示", "请先选择一个会话。")
            return
        if not messagebox.askyesno("确认", "删除当前会话及其中的附件图片？"):
            return
        shutil.rmtree(self._session_dir(self._current_sid), ignore_errors=True)
        self._current_sid = None
        self._meta = {}
        self._pending_names.clear()
        self._clear_chat_surface()
        self._title_var.set("新对话")
        self._refresh_sidebar()

    def _clear_chat_surface(self) -> None:
        self._hide_typing()
        self._wrap_labels.clear()
        for w in self._msgs.winfo_children():
            w.destroy()

    def _clear_pending(self) -> None:
        self._pending_names.clear()
        self._refresh_attach_label()

    def _refresh_attach_label(self) -> None:
        if self._pending_names:
            self._attach_lbl.configure(
                text="已选附件：" + "、".join(self._pending_names[:8])
                + (" …" if len(self._pending_names) > 8 else "")
            )
        else:
            self._attach_lbl.configure(text="")

    def _pick_images(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self._app,
            title="选择图片",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("全部", "*.*"),
            ],
        )
        if not paths:
            return
        sid = self._ensure_session()
        fd = self._files_dir(sid)
        fd.mkdir(parents=True, exist_ok=True)
        for src in paths:
            sp = Path(src)
            if not sp.is_file():
                continue
            ext = sp.suffix.lower() or ".png"
            dest = fd / f"img_{uuid.uuid4().hex[:10]}{ext}"
            try:
                shutil.copy2(sp, dest)
                self._pending_names.append(dest.name)
            except OSError as e:
                messagebox.showerror("复制失败", str(e))
        self._refresh_attach_label()

    def _bubble_dims(self, canvas_w: int) -> tuple[int, int]:
        wu = max(200, int((canvas_w - 72) * 0.70))
        wa = max(240, int((canvas_w - 40) * 0.92))
        return wu, wa

    def _chars_for_wrap(self, px: int) -> int:
        f = tkfont.Font(font=self._app._font_content)
        w = max(f.measure("测"), f.measure("W"), 6)
        return max(12, int(px // w))

    def _bubble_text(
        self,
        parent: tk.Misc,
        content: str,
        *,
        bg: str,
        wrap_px: int,
    ) -> tk.Text:
        chars = self._chars_for_wrap(wrap_px)
        t = tk.Text(
            parent,
            wrap=tk.WORD,
            width=chars,
            height=1,
            font=self._app._font_content,
            bg=bg,
            fg=A_TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
            cursor="xterm",
            undo=False,
            maxundo=0,
        )
        t.insert("1.0", (content.strip() or "\u00a0"))
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=max(1, lines))

        def on_key(event: tk.Event) -> str | None:
            st = int(getattr(event, "state", 0) or 0)
            sym = (event.keysym or "").lower()
            if st & 0x4 and sym in ("a", "c"):
                return None
            return "break"

        t.bind("<Key>", on_key, add=True)
        t.bind("<<Paste>>", lambda _e: "break", add=True)
        return t

    def _refresh_bubble_wrap(self, canvas_w: int) -> None:
        wu, wa = self._bubble_dims(canvas_w)
        for w, role in self._wrap_labels:
            if isinstance(w, tk.Text):
                px = wu if role == "user" else wa
                w.configure(width=self._chars_for_wrap(px))
                try:
                    lines = int(w.index("end-1c").split(".")[0])
                    w.configure(height=max(1, lines))
                except tk.TclError:
                    pass

    def _on_chat_wheel(self, event: tk.Event) -> None:
        if _widget_under_ancestor(getattr(event, "widget", None), self._composer):
            return
        lines = self._app._text_wheel_lines
        d = getattr(event, "delta", 0) or 0
        if d:
            steps = int(-d * lines / 120.0) if sys.platform == "win32" else int(-d * lines / 120.0)
            if steps == 0:
                steps = -lines if d > 0 else lines
            if steps:
                self._chat_canvas.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            self._chat_canvas.yview_scroll(-lines, "units")
        elif n == 5:
            self._chat_canvas.yview_scroll(lines, "units")

    def _scroll_bottom(self) -> None:
        self.update_idletasks()
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))
        self._chat_canvas.yview_moveto(1.0)

    def _add_user_bubble(self, text: str, img_names: list[str]) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        ts = datetime.now().strftime("%H:%M")
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, 12))
        tk.Label(
            meta,
            text=f"You  ·  {ts}",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(side=tk.RIGHT)
        bubble = tk.Frame(row, bg=A_USER)
        bubble.pack(anchor=tk.E, padx=(40, 8), pady=(2, 4))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_dims(cw)
        body = self._bubble_text(bubble, text, bg=A_USER, wrap_px=wu)
        body.pack(padx=12, pady=10, anchor=tk.W)
        self._wrap_labels.append((body, "user"))
        if img_names:
            tk.Label(
                bubble,
                text="📎 " + "、".join(img_names[:6])
                + (" …" if len(img_names) > 6 else ""),
                font=self._font_small,
                bg=A_USER,
                fg=A_META,
                anchor=tk.W,
            ).pack(anchor=tk.W, padx=12, pady=(0, 8))
        self._scroll_bottom()

    def _add_assistant_bubble(self, text: str, *, error: bool = False) -> None:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        tk.Label(
            row,
            text="Assistant",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(8, 0))
        bg = A_ERR if error else A_CARD
        border = "#fecaca" if error else A_CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_dims(cw)
        body = self._bubble_text(card, text, bg=bg, wrap_px=wa)
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._scroll_bottom()

    def _show_typing(self) -> None:
        if self._typing_row is not None:
            return
        z = T_SURFACE
        self._typing_row = tk.Frame(self._msgs, bg=z)
        self._typing_row.pack(fill=tk.X, pady=(4, 8))
        tk.Label(
            self._typing_row,
            text="正在回复…",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(12, 0))
        self._scroll_bottom()

    def _hide_typing(self) -> None:
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None

    def _reset_stream_coalesce(self) -> None:
        self._stream_pending = ""
        self._stream_body = None
        aid = self._stream_after_id
        self._stream_after_id = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _enqueue_stream_delta(self, body: tk.Text, chunk: str) -> None:
        self._stream_body = body
        self._stream_pending += chunk
        if self._stream_after_id is None:
            self._stream_after_id = self.after(
                40, lambda b=body: self._flush_stream_pending(b)
            )

    def _flush_stream_pending(self, body: tk.Text) -> None:
        self._stream_after_id = None
        chunk = self._stream_pending
        self._stream_pending = ""
        if not chunk:
            return
        try:
            cur = body.get("1.0", "end-1c")
            if cur.strip() in ("", "\u00a0"):
                body.delete("1.0", tk.END)
            body.insert(tk.END, chunk)
            lines = int(body.index("end-1c").split(".")[0])
            body.configure(height=max(1, lines))
            self._scroll_bottom()
        except tk.TclError:
            pass

    def _flush_stream_delta_buffer(self) -> None:
        b = self._stream_body
        if b is not None and self._stream_pending:
            self._flush_stream_pending(b)
        self._stream_pending = ""
        aid = self._stream_after_id
        self._stream_after_id = None
        if aid is not None:
            try:
                self.after_cancel(aid)
            except tk.TclError:
                pass

    def _add_assistant_stream_shell(self) -> tk.Text:
        z = T_SURFACE
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        tk.Label(
            row,
            text="Assistant",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(8, 0))
        bg = A_CARD
        card = tk.Frame(row, bg=bg, highlightbackground=A_CARD_BORDER, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_dims(cw)
        body = self._bubble_text(card, "\u00a0", bg=bg, wrap_px=wa)
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._scroll_bottom()
        return body

    def _on_ctrl_enter(self, _event: object) -> str:
        self._on_send()
        return "break"

    def _on_send(self) -> None:
        if self._busy:
            return
        text = self._inp.get("1.0", tk.END).strip()
        if not text and not self._pending_names:
            return
        base = self._loc_base.get().strip().rstrip("/")
        model = self._loc_model.get().strip()
        key = self._loc_key.get().strip() or "EMPTY"
        if not base:
            messagebox.showwarning(
                "提示",
                f"请填写服务根 URL（须含 /v1）。Gemma 本地默认同 Vision 副本：\n{DEFAULT_LOCAL_OPENAI_BASE}",
            )
            return
        if not model:
            messagebox.showwarning("提示", "请填写模型 ID（须与本地 serve 一致）。")
            return
        sid = self._ensure_session()
        fd = self._files_dir(sid)
        pending_paths = [fd / n for n in self._pending_names]
        prior: list[tuple[str, str, list[Path]]] = []
        for t in self._meta.get("turns") or []:
            names = [str(x) for x in (t.get("imgs") or []) if x]
            imgs = [fd / n for n in names]
            prior.append((str(t.get("u") or ""), str(t.get("a") or ""), imgs))
        sys_p = SYSTEM_LOCAL_CHAT_ZH
        if self._include_merged.get() and MERGED.is_file():
            body = MERGED.read_text(encoding="utf-8", errors="replace")
            if len(body) > MAX_LOCAL_CHAT_MERGED_SNIPPET:
                body = body[: MAX_LOCAL_CHAT_MERGED_SNIPPET] + "\n…（已截断）"
            sys_p += "\n\n【背景：视频合并文稿（可选参考）】\n" + body
        self._busy = True
        self._add_user_bubble(text, list(self._pending_names))
        self._inp.delete("1.0", tk.END)
        self._pending_names.clear()
        self._refresh_attach_label()
        use_stream = bool(self._loc_stream.get())
        stream_body: tk.Text | None = None
        if use_stream:
            stream_body = self._add_assistant_stream_shell()
        else:
            self._show_typing()
        self._status.configure(text="请求中…")
        for w in (self._inp,):
            try:
                w.configure(state=tk.DISABLED)
            except tk.TclError:
                pass
        threading.Thread(
            target=self._worker_send,
            args=(text, pending_paths, prior, base, key, model, sys_p, sid, use_stream, stream_body),
            daemon=True,
        ).start()

    def _worker_send(
        self,
        text: str,
        pending_paths: list[Path],
        prior: list[tuple[str, str, list[Path]]],
        base: str,
        key: str,
        model: str,
        sys_p: str,
        sid: str,
        use_stream: bool,
        stream_body: tk.Text | None,
    ) -> None:
        try:
            names = [p.name for p in pending_paths if p.is_file()]
            if use_stream and stream_body is not None:
                def push(c: str) -> None:
                    self._app.after(
                        0, lambda c=c: self._enqueue_stream_delta(stream_body, c)
                    )

                reply, used = local_openai_compatible_chat_round(
                    base_url=base,
                    api_key=key,
                    model=model,
                    system_prompt=sys_p,
                    prior=prior,
                    user_text=text,
                    user_images=pending_paths,
                    stream=True,
                    on_delta=push,
                )
                self._app.after(0, self._flush_stream_delta_buffer)
                self._app.after(
                    0,
                    lambda: self._finish_ok(
                        text, reply, used, names, sid, stream_bubble=stream_body
                    ),
                )
            else:
                reply, used = local_openai_compatible_chat_round(
                    base_url=base,
                    api_key=key,
                    model=model,
                    system_prompt=sys_p,
                    prior=prior,
                    user_text=text,
                    user_images=pending_paths,
                )
                self._app.after(
                    0,
                    lambda: self._finish_ok(text, reply, used, names, sid),
                )
        except Exception as e:
            self._app.after(
                0,
                lambda err=str(e), sb=stream_body: self._finish_err(err, stream_bubble=sb),
            )

    def _finish_ok(
        self,
        user_text: str,
        reply: str,
        used_model: str,
        img_names: list[str],
        sid: str,
        *,
        stream_bubble: tk.Text | None = None,
    ) -> None:
        self._reset_stream_coalesce()
        self._busy = False
        for w in (self._inp,):
            try:
                w.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._hide_typing()
        mp = self._meta_path(sid)
        try:
            disk = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            disk = {"id": sid, "title": "对话", "turns": []}
        disk.setdefault("turns", []).append(
            {"u": user_text, "a": reply, "imgs": img_names}
        )
        disk["updated"] = datetime.now().isoformat(timespec="seconds")
        turns = disk["turns"]
        if len(turns) == 1 and str(disk.get("title") or "") in ("", "新对话"):
            short = user_text.replace("\n", " ").strip()[:40]
            if short:
                disk["title"] = short + ("…" if len(user_text) > 40 else "")
        try:
            mp.write_text(
                json.dumps(disk, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

        if self._current_sid == sid:
            if stream_bubble is None:
                self._add_assistant_bubble(reply)
            self._meta = disk
            self._title_var.set(str(disk.get("title") or "对话"))
            self._save_meta()
            self._status.configure(text=f"就绪 · {used_model}")
        else:
            self._status.configure(
                text=f"就绪 · {used_model}（已切换会话，回复已写入左侧对应历史）"
            )
        self._refresh_sidebar()

    def _finish_err(self, err: str, *, stream_bubble: tk.Text | None = None) -> None:
        self._reset_stream_coalesce()
        self._busy = False
        for w in (self._inp,):
            try:
                w.configure(state=tk.NORMAL)
            except tk.TclError:
                pass
        self._hide_typing()
        if stream_bubble is not None:
            try:
                stream_bubble.master.master.destroy()
            except tk.TclError:
                pass
        self._add_assistant_bubble(err, error=True)
        self._status.configure(text="出错 · 请检查 URL / 模型 / 服务是否支持 SSE 与 vision")


class AgentSession:
    """分析报告底部对话区：单面板会话与 API 轮次。"""

    def __init__(self, app: App) -> None:
        self._app = app
        self._provider = resolve_provider(app.llm_provider_var.get().strip())
        self._turns: list[tuple[str, str, bool]] = []
        self._panels: list[AgentChatPanel] = []
        self._busy = False

    def attach(self, panel: AgentChatPanel) -> None:
        if panel not in self._panels:
            self._panels.append(panel)

    def detach(self, panel: AgentChatPanel) -> None:
        try:
            self._panels.remove(panel)
        except ValueError:
            pass

    def clear(self) -> None:
        self._turns.clear()
        self._busy = False
        for p in list(self._panels):
            try:
                p._reset_chat_ui()
            except tk.TclError:
                self.detach(p)

    def submit(self, user_visible: str) -> None:
        self._app._sync_llm_env_from_form()
        self._provider = resolve_provider(self._app.llm_provider_var.get().strip())
        if self._provider is None or self._busy:
            if self._provider is None and not self._busy:
                messagebox.showwarning(
                    "无法发送",
                    "请先在「API 与模型」填写对应平台的 Key 并点击「应用」，"
                    "或将「首选提供商」改为「自动」并至少填写一个平台的 Key。",
                )
            return
        if not MERGED.is_file():
            messagebox.showwarning(
                "无法发送",
                "尚未生成合并文稿 out/transcript_merged.txt。\n请先点击「开始提取与分析」。",
            )
            return
        u = user_visible.strip()
        if not u:
            return
        self._busy = True
        for p in self._panels:
            p._inp.delete("1.0", tk.END)
        for p in self._panels:
            p._add_user_bubble(u)
        for p in self._panels:
            p._show_typing()
        for p in self._panels:
            p._send_btn.configure(state=tk.DISABLED)
            p._clear_btn.configure(state=tk.DISABLED)
            p._status.configure(text="请求中…")
        threading.Thread(target=self._worker, args=(u,), daemon=True).start()

    def _worker(self, user_visible: str) -> None:
        try:
            merged = MERGED.read_text(encoding="utf-8", errors="replace")
            analysis = ""
            if ANALYSIS.is_file():
                analysis = ANALYSIS.read_text(encoding="utf-8", errors="replace")
            vision_paths = collect_timeline_frame_paths(ANALYSIS_DEEP_JSON)
            attach_vis = bool(self._app.report_chat_attach_vision_var.get())
            reply, tag, payload = chat_followup(
                self._provider,
                merged,
                user_visible,
                analysis_excerpt=analysis or None,
                prior_turns=self._turns,
                attach_vision_current=attach_vis,
                vision_frame_paths=vision_paths,
            )
            self._turns.append((payload, reply, attach_vis))
            self._app.after(0, lambda r=reply, t=tag: self._finish_ok(r, t))
        except Exception as e:
            self._app.after(0, lambda err=str(e): self._finish_err(err))

    def _finish_ok(self, reply: str, tag: str) -> None:
        self._busy = False
        for p in self._panels:
            p._hide_typing()
            p._send_btn.configure(state=tk.NORMAL)
            p._clear_btn.configure(state=tk.NORMAL)
            p._status.configure(text=f"就绪 · {tag}")
            p._add_assistant_bubble(reply, f"Agent · {tag}")

    def _finish_err(self, err: str) -> None:
        self._busy = False
        for p in self._panels:
            p._hide_typing()
            p._send_btn.configure(state=tk.NORMAL)
            p._clear_btn.configure(state=tk.NORMAL)
            p._status.configure(text="出错 · 请重试")
            p._add_assistant_bubble(err, "Error", error=True)


class AgentChatPanel(tk.Frame):
    """分析报告页底部对话区（聊天记录 + 输入框）。"""

    def __init__(
        self,
        parent: tk.Misc,
        app: App,
        session: AgentSession,
        *,
        compact: bool = True,
    ) -> None:
        super().__init__(parent, bg=T_BG if compact else A_BG)
        self._app = app
        self._session = session
        self._compact = compact
        self._provider = session._provider
        self._wrap_labels: list[tuple[tk.Label, str]] = []
        self._typing_row: tk.Frame | None = None

        self._font_body = app._font_content
        fam, sz = self._font_body[0], int(round(self._font_body[1]))
        self._font_small = (fam, max(8, sz - 1))

        pad_x = 0 if compact else 10
        pad_y = (0, 2) if compact else (8, 6)
        top = tk.Frame(self, bg=T_BG if compact else A_BG)
        top.pack(fill=tk.BOTH, expand=True, padx=pad_x, pady=pad_y)

        if self._provider is None:
            ttk.Label(
                top,
                text=(
                    "未配置可用的 API Key，或当前「首选提供商」与已填 Key 不匹配。\n\n"
                    "请打开左侧「API 与模型」填写 Key 并点击「应用」；"
                    "也可使用 local_api_keys.py（参考 local_api_keys.example.py）。"
                ),
                wraplength=320,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, padx=4)
            return

        chat_bg = T_SURFACE if compact else A_BG
        self._chat_zone_bg = chat_bg
        chat_row = tk.Frame(top, bg=chat_bg)

        inp_h = 3
        szf = float(self._font_body[1])
        line_h = max(18, int(round(szf * 1.55)))
        composer_minsize = max(168, 18 + inp_h * line_h + 50)
        top.grid_rowconfigure(0, weight=1, minsize=40)
        top.grid_rowconfigure(1, weight=0, minsize=composer_minsize)
        top.grid_columnconfigure(0, weight=1)
        chat_row.grid(row=0, column=0, sticky="nsew")

        y_inc = max(18, int(round(app._font_content[1] * 2.4)))
        self._chat_canvas = tk.Canvas(
            chat_row,
            bg=chat_bg,
            highlightthickness=0,
            borderwidth=0,
            yscrollincrement=y_inc,
        )
        sb = tk.Scrollbar(
            chat_row,
            command=self._chat_canvas.yview,
            width=app._scrollbar_px,
            borderwidth=0,
            troughcolor=SCROLL_TROUGH,
            bg="#c4c4c4",
            activebackground="#a8a8a8",
            highlightthickness=0,
            relief="flat",
            jump=1,
        )
        self._chat_canvas.configure(yscrollcommand=sb.set)
        self._msgs = tk.Frame(self._chat_canvas, bg=chat_bg)
        self._msg_win = self._chat_canvas.create_window((0, 0), window=self._msgs, anchor="nw")

        def _sync_scroll(_event: object | None = None) -> None:
            self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))

        def _on_canvas_cfg(event: tk.Misc) -> None:
            w = int(getattr(event, "width", 0) or 0)
            if w > 8:
                self._chat_canvas.itemconfigure(self._msg_win, width=w)
                self._refresh_bubble_wrap(w)

        self._msgs.bind("<Configure>", lambda e: _sync_scroll())
        self._chat_canvas.bind("<Configure>", _on_canvas_cfg)

        self._attach_chat_wheel(self._chat_canvas)
        self._attach_chat_wheel(self._msgs)

        self._chat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        composer_outer = tk.Frame(top, bg=T_BG if compact else A_BG)
        composer_outer.grid(row=1, column=0, sticky="ew", pady=(6, 2))
        self._composer = tk.Frame(
            composer_outer,
            bg=A_COMPOSER,
            highlightbackground=A_CARD_BORDER,
            highlightthickness=1,
        )
        self._composer.pack(fill=tk.X, ipadx=1, ipady=1)
        self._inp = tk.Text(
            self._composer,
            height=inp_h,
            wrap=tk.WORD,
            font=self._font_body,
            padx=10,
            pady=6 if compact else 8,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            bg=A_COMPOSER,
            fg=A_TEXT,
            insertbackground=A_TEXT,
        )
        self._inp.pack(fill=tk.X, padx=4, pady=(4, 2))
        bind_text_mousewheel(self._inp, lines_per_notch=app._text_wheel_lines)

        bottom_bar = tk.Frame(self._composer, bg=A_COMPOSER)
        bottom_bar.pack(fill=tk.X, padx=6, pady=(0, 6))
        self._status = tk.Label(
            bottom_bar,
            text="Ctrl+Enter 发送",
            font=self._font_small,
            bg=A_COMPOSER,
            fg=A_META,
        )
        self._status.pack(side=tk.LEFT)
        ttk.Checkbutton(
            bottom_bar,
            text="本轮附带画面帧（不勾选则模型只看文字）",
            variable=app.report_chat_attach_vision_var,
        ).pack(side=tk.LEFT, padx=(10, 4))
        self._send_btn = ttk.Button(
            bottom_bar,
            text="发送",
            command=self._on_send,
            style=app._run_button_style,
            width=8,
        )
        self._send_btn.pack(side=tk.RIGHT, padx=(6, 0))
        self._clear_btn = ttk.Button(
            bottom_bar,
            text="清空",
            command=self._clear_turns,
            width=6,
        )
        self._clear_btn.pack(side=tk.RIGHT, padx=(0, 8))

        self._inp.bind("<Control-Return>", self._on_ctrl_enter)

        self.bind("<Destroy>", self._on_panel_destroy)
        self._session.attach(self)

    def _bubble_wraps(self, canvas_w: int) -> tuple[int, int]:
        wu = max(200, int((canvas_w - 72) * 0.70))
        wa = max(240, int((canvas_w - 40) * 0.92))
        return wu, wa

    def _chars_for_wrap_px(self, px: int) -> int:
        f = tkfont.Font(font=self._font_body)
        w = max(f.measure("测"), f.measure("W"), 6)
        return max(12, int(px // w))

    def _bind_bubble_readonly_text(self, w: tk.Text) -> None:
        """可选中、Ctrl+C 复制；禁止编辑与粘贴。"""

        def on_key(event: tk.Event) -> str | None:
            st = int(getattr(event, "state", 0) or 0)
            sym = (event.keysym or "").lower()
            if st & 0x4 and sym in ("a", "c"):
                return None
            return "break"

        w.bind("<Key>", on_key, add=True)
        w.bind("<<Paste>>", lambda _e: "break", add=True)
        w.bind("<<Cut>>", lambda _e: "break", add=True)

    def _make_bubble_text(
        self,
        parent: tk.Misc,
        content: str,
        *,
        bg: str,
        fg: str,
        wrap_px: int,
    ) -> tk.Text:
        chars = self._chars_for_wrap_px(wrap_px)
        t = tk.Text(
            parent,
            wrap=tk.WORD,
            width=chars,
            height=1,
            font=self._font_body,
            bg=bg,
            fg=fg,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=2,
            pady=4,
            cursor="xterm",
            undo=False,
            maxundo=0,
        )
        t.insert("1.0", (content.strip() or "\u00a0"))
        lines = int(t.index("end-1c").split(".")[0])
        t.configure(height=max(1, lines))
        self._bind_bubble_readonly_text(t)
        return t

    def _refresh_bubble_wrap(self, canvas_w: int) -> None:
        wu, wa = self._bubble_wraps(canvas_w)
        for w, role in self._wrap_labels:
            if isinstance(w, tk.Text):
                px = wu if role == "user" else wa
                chars = self._chars_for_wrap_px(px)
                w.configure(width=chars)
                lines = int(w.index("end-1c").split(".")[0])
                w.configure(height=max(1, lines))
            else:
                w.configure(wraplength=wu if role == "user" else wa)

    def _attach_chat_wheel(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_chat_wheel)
        widget.bind("<Button-4>", self._on_chat_wheel)
        widget.bind("<Button-5>", self._on_chat_wheel)

    def _wire_bubble_wheel(self, root: tk.Misc) -> None:
        self._attach_chat_wheel(root)
        for c in root.winfo_children():
            self._wire_bubble_wheel(c)

    def _on_chat_wheel(self, event: tk.Event) -> None:
        if _widget_under_ancestor(getattr(event, "widget", None), self._composer):
            return
        lines = self._app._text_wheel_lines
        d = getattr(event, "delta", 0) or 0
        if d:
            if sys.platform == "win32":
                steps = int(-d * lines / 120.0)
                if steps == 0:
                    steps = -lines if d > 0 else lines
            else:
                steps = int(-d * lines / 120.0)
                if steps == 0:
                    steps = -lines if d > 0 else lines
            if steps:
                self._chat_canvas.yview_scroll(steps, "units")
            return
        n = getattr(event, "num", 0)
        if n == 4:
            self._chat_canvas.yview_scroll(-lines, "units")
        elif n == 5:
            self._chat_canvas.yview_scroll(lines, "units")

    def _scroll_chat_bottom(self) -> None:
        self.update_idletasks()
        self._chat_canvas.configure(scrollregion=self._chat_canvas.bbox("all"))
        self._chat_canvas.yview_moveto(1.0)

    def _add_user_bubble(self, text: str) -> None:
        z = self._chat_zone_bg
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        meta = tk.Frame(row, bg=z)
        meta.pack(fill=tk.X, padx=(0, 12))
        ts = datetime.now().strftime("%H:%M")
        tk.Label(
            meta,
            text=f"You  ·  {ts}",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(side=tk.RIGHT)

        bubble = tk.Frame(row, bg=A_USER)
        bubble.pack(anchor=tk.E, padx=(40, 8), pady=(2, 4))
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        wu, _ = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            bubble, text, bg=A_USER, fg=A_TEXT, wrap_px=wu
        )
        body.pack(padx=12, pady=10, anchor=tk.W)
        self._wrap_labels.append((body, "user"))
        self._wire_bubble_wheel(row)
        self._scroll_chat_bottom()

    def _add_assistant_bubble(
        self,
        text: str,
        subtitle: str,
        *,
        muted: bool = False,
        error: bool = False,
    ) -> None:
        z = self._chat_zone_bg
        row = tk.Frame(self._msgs, bg=z)
        row.pack(fill=tk.X, pady=(10, 2))
        head = tk.Label(
            row,
            text=subtitle,
            font=self._font_small,
            bg=z,
            fg=A_META if muted else "#374151",
        )
        head.pack(anchor=tk.W, padx=(8, 0))
        bg = A_ERR if error else A_CARD
        border = "#fecaca" if error else A_CARD_BORDER
        card = tk.Frame(row, bg=bg, highlightbackground=border, highlightthickness=1)
        card.pack(anchor=tk.W, padx=(8, 12), pady=(4, 6), fill=tk.X)
        cw = max(280, self._chat_canvas.winfo_width() or 320)
        _, wa = self._bubble_wraps(cw)
        body = self._make_bubble_text(
            card, text, bg=bg, fg=A_TEXT, wrap_px=wa
        )
        body.pack(padx=14, pady=12, anchor=tk.W, fill=tk.X)
        self._wrap_labels.append((body, "assistant"))
        self._wire_bubble_wheel(row)
        self._scroll_chat_bottom()

    def _show_typing(self) -> None:
        if self._typing_row is not None:
            return
        z = self._chat_zone_bg
        self._typing_row = tk.Frame(self._msgs, bg=z)
        self._typing_row.pack(fill=tk.X, pady=(4, 8))
        tk.Label(
            self._typing_row,
            text="正在回复…",
            font=self._font_small,
            bg=z,
            fg=A_META,
        ).pack(anchor=tk.W, padx=(12, 0))
        self._wire_bubble_wheel(self._typing_row)
        self._scroll_chat_bottom()

    def _hide_typing(self) -> None:
        if self._typing_row is not None:
            self._typing_row.destroy()
            self._typing_row = None

    def _on_ctrl_enter(self, _event: object) -> str:
        self._on_send()
        return "break"

    def _on_panel_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._session.detach(self)

    def _reset_chat_ui(self) -> None:
        if self._provider is None:
            return
        self._wrap_labels.clear()
        self._hide_typing()
        for w in self._msgs.winfo_children():
            w.destroy()
        self._status.configure(text="Ctrl+Enter 发送")
        self._send_btn.configure(state=tk.NORMAL)
        self._clear_btn.configure(state=tk.NORMAL)

    def _clear_turns(self) -> None:
        self._session.clear()

    def _on_send(self) -> None:
        if self._provider is None:
            return
        text = self._inp.get("1.0", tk.END).strip()
        self._session.submit(text)


def main() -> None:
    _win_set_per_monitor_dpi()
    app = App()
    app.reload_files()
    app.mainloop()


if __name__ == "__main__":
    main()
