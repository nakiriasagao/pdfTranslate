"""Tkinter 桌面界面（纯标准库，双击即用）。

界面分四块：文件 → 引擎 → 设置 → 输出与进度。
所有耗时操作都在后台线程执行，主线程只负责刷新界面，因此不会卡死。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import (
    BOTH, BOTTOM, END, HORIZONTAL, LEFT, RIGHT, TOP, VERTICAL, X, Y,
    BooleanVar, DoubleVar, IntVar, StringVar,
)
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from . import APP_TITLE, __version__
from .config import (
    ENGINE_PRESETS,
    MODE_BILINGUAL,
    MODE_REPLACED,
    TARGET_LANGUAGES,
    Settings,
)
from .extractor import PdfExtractor
from .pipeline import CancelledError, Pipeline, StageProgress

PAD = 8
_CANCELLED = "__cancelled__"

LANG_CHOICES = [(code, label) for code, label in TARGET_LANGUAGES.items()]
LANG_LABEL_TO_CODE = {label: code for code, label in LANG_CHOICES}
LANG_CODE_TO_LABEL = {code: label for code, label in LANG_CHOICES}


def open_in_explorer(path: str | Path) -> None:
    """在资源管理器里打开目录 / 选中文件。"""
    target = Path(path)
    try:
        if os.name == "nt":
            if target.is_dir():
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target if target.is_dir() else target.parent)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    """让窗口在高 DPI 屏幕上保持清晰。

    不声明 DPI 感知的话，Windows 会把整个窗口当成低分辨率位图拉伸，
    在 125%/150% 缩放的屏幕上字会发虚；而且 tkinter 与系统 GDI 的坐标
    体系会不一致（截图、控件定位都会错位）。必须在创建 ``tk.Tk()`` 之前调用。
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        try:  # PROCESS_SYSTEM_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class PdfTranslatorApp(tk.Tk):
    """主窗口。"""

    # ------------------------------------------------------------------ #
    def __init__(self) -> None:
        enable_dpi_awareness()  # 必须早于 Tk() 创建
        super().__init__()
        self.title(f"{APP_TITLE} v{__version__}")

        self.settings = Settings.load()
        self.files: list[str] = []
        self.messages: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self._last_outputs: list[str] = []

        self._init_style()
        self._build_ui()
        self._load_into_ui()
        self._fit_to_screen()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain_messages)

    # ------------------------------------------------------------------ #
    def _init_style(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        for candidate in ("vista", "winnative", "clam"):
            if candidate in available:
                try:
                    style.theme_use(candidate)
                    break
                except tk.TclError:
                    continue
        base_font = ("Microsoft YaHei UI", 9) if os.name == "nt" else ("Helvetica", 10)
        # Tcl 里带空格的字体名要用花括号包起来
        self.option_add("*Font", f"{{{base_font[0]}}} {base_font[1]}")
        style.configure(".", font=base_font)
        style.configure("Heading.TLabel", font=(base_font[0], 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Go.TButton", font=(base_font[0], 10, "bold"))

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=PAD)
        outer.pack(fill=BOTH, expand=True)

        # 关键：底部「进度条 + 开始翻译」区域**最先** pack。
        # Tk 按 pack 顺序分配空间，窗口高度不足时最后 pack 的才会被裁掉，
        # 所以最先 pack 的按钮栏永远露得出来。
        self._build_bottom(outer)

        # 内容改成标签页：小屏（如 1536x864 的笔记本）也放得下，
        # 单页平铺需要 1000px 以上高度，会被挤到看不见按钮和日志。
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=BOTH, expand=True)

        page_files = ttk.Frame(self.notebook, padding=PAD)
        page_options = ttk.Frame(self.notebook, padding=PAD)
        page_log = ttk.Frame(self.notebook, padding=PAD)
        self.notebook.add(page_files, text="   ① 文件与引擎   ")
        self.notebook.add(page_options, text="   ② 翻译设置   ")
        self.notebook.add(page_log, text="   ③ 运行日志   ")
        self._log_page = page_log

        self._build_files(page_files)
        self._build_engine(page_files)
        self._build_options(page_options)
        self._build_output(page_options)
        self._build_log(page_log)

    # ------------------------------------------------------------------ #
    def _show_log_page(self) -> None:
        """切到「运行日志」页，让用户马上看到进度。"""
        try:
            self.notebook.select(self._log_page)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    def _fit_to_screen(self) -> None:
        """按屏幕可用空间和内容实际需要摆放窗口，保证不超出屏幕。"""
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        avail_w = max(640, screen_w - 60)
        avail_h = max(460, screen_h - 100)  # 给任务栏和标题栏留余量

        width = min(max(820, self.winfo_reqwidth()), avail_w)
        height = min(self.winfo_reqheight(), avail_h)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 4)
        self.geometry(f"{width}x{height}+{x}+{y}")
        # 不允许缩到内容放不下的尺寸，否则用户一拖拽就会「丢控件」
        self.minsize(min(max(760, self.winfo_reqwidth() - 40), avail_w),
                     min(max(560, self.winfo_reqheight() - 120), avail_h))

    # ------------------------------------------------------------------ #
    def _build_files(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 1. 选择 PDF 文件 ", padding=PAD)
        frame.pack(fill=X, pady=(0, PAD))

        body = ttk.Frame(frame)
        body.pack(fill=X)
        listbox_frame = ttk.Frame(body)
        listbox_frame.pack(side=LEFT, fill=BOTH, expand=True)

        self.file_list = tk.Listbox(listbox_frame, height=4, selectmode=tk.EXTENDED,
                                    activestyle="none", exportselection=False)
        self.file_list.pack(side=LEFT, fill=BOTH, expand=True)
        scroll = ttk.Scrollbar(listbox_frame, orient=VERTICAL, command=self.file_list.yview)
        scroll.pack(side=RIGHT, fill=Y)
        self.file_list.configure(yscrollcommand=scroll.set)
        self.file_list.bind("<Double-Button-1>", lambda _e: self._open_selected())

        buttons = ttk.Frame(body)
        buttons.pack(side=RIGHT, fill=Y, padx=(PAD, 0))
        ttk.Button(buttons, text="添加文件…", command=self._add_files, width=12).pack(pady=(0, 4))
        ttk.Button(buttons, text="添加文件夹…", command=self._add_folder, width=12).pack(pady=(0, 4))
        ttk.Button(buttons, text="移除选中", command=self._remove_selected, width=12).pack(pady=(0, 4))
        ttk.Button(buttons, text="清空", command=self._clear_files, width=12).pack()

        ttk.Label(
            frame, style="Hint.TLabel",
            text="提示：可多选；会把每个 PDF 分别翻译，结果输出到下面的目录。",
        ).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------ #
    def _build_engine(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 2. AI 引擎（DeepSeek / 任意 OpenAI 兼容接口） ", padding=PAD)
        frame.pack(fill=X, pady=(0, PAD))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="服务商：").grid(row=0, column=0, sticky="w", pady=3)
        self.engine_var = StringVar()
        self.engine_combo = ttk.Combobox(
            frame, textvariable=self.engine_var, state="readonly",
            values=[f"{key}  |  {preset['label']}" for key, preset in ENGINE_PRESETS.items()],
        )
        self.engine_combo.grid(row=0, column=1, sticky="ew", pady=3, padx=(4, 0))
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine_change)

        ttk.Label(frame, text="Base URL：").grid(row=1, column=0, sticky="w", pady=3)
        self.base_url_var = StringVar()
        ttk.Entry(frame, textvariable=self.base_url_var).grid(row=1, column=1, sticky="ew", pady=3, padx=(4, 0))

        ttk.Label(frame, text="API Key：").grid(row=2, column=0, sticky="w", pady=3)
        key_frame = ttk.Frame(frame)
        key_frame.grid(row=2, column=1, sticky="ew", pady=3, padx=(4, 0))
        key_frame.columnconfigure(0, weight=1)
        self.api_key_var = StringVar()
        self.key_entry = ttk.Entry(key_frame, textvariable=self.api_key_var, show="●")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_var = BooleanVar(value=False)
        ttk.Checkbutton(key_frame, text="显示", variable=self.show_key_var,
                        command=self._toggle_key).grid(row=0, column=1, padx=(6, 0))
        self.remember_key_var = BooleanVar(value=True)
        ttk.Checkbutton(key_frame, text="记住", variable=self.remember_key_var).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(frame, text="模型：").grid(row=3, column=0, sticky="w", pady=3)
        model_frame = ttk.Frame(frame)
        model_frame.grid(row=3, column=1, sticky="ew", pady=3, padx=(4, 0))
        model_frame.columnconfigure(0, weight=1)
        self.model_var = StringVar()
        ttk.Entry(model_frame, textvariable=self.model_var).grid(row=0, column=0, sticky="ew")
        self.test_button = ttk.Button(model_frame, text="测试连接", command=self._test_connection, width=10)
        self.test_button.grid(row=0, column=1, padx=(6, 0))

        self.engine_note = ttk.Label(frame, style="Hint.TLabel", text="", wraplength=700, justify="left")
        self.engine_note.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------------ #
    def _build_options(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 3. 翻译与排版设置 ", padding=PAD)
        frame.pack(fill=X, pady=(0, PAD))
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        ttk.Label(frame, text="目标语言：").grid(row=0, column=0, sticky="w", pady=3)
        self.lang_var = StringVar(value=LANG_CODE_TO_LABEL["zh"])
        ttk.Combobox(frame, textvariable=self.lang_var, state="readonly",
                     values=[label for _code, label in LANG_CHOICES]).grid(
            row=0, column=1, sticky="ew", pady=3, padx=(4, 12))

        ttk.Label(frame, text="页码范围：").grid(row=0, column=2, sticky="w", pady=3)
        self.pages_var = StringVar()
        ttk.Entry(frame, textvariable=self.pages_var).grid(row=0, column=3, sticky="ew", pady=3, padx=(4, 0))

        ttk.Label(frame, text="并发请求数：").grid(row=1, column=0, sticky="w", pady=3)
        self.concurrency_var = IntVar(value=4)
        ttk.Spinbox(frame, from_=1, to=16, textvariable=self.concurrency_var, width=6).grid(
            row=1, column=1, sticky="w", pady=3, padx=(4, 12))

        ttk.Label(frame, text="译文最小字号：").grid(row=1, column=2, sticky="w", pady=3)
        self.min_scale_var = DoubleVar(value=0.62)
        ttk.Spinbox(frame, from_=0.3, to=1.0, increment=0.02, textvariable=self.min_scale_var, width=6).grid(
            row=1, column=3, sticky="w", pady=3, padx=(4, 0))

        ttk.Label(frame, text="术语表文件：").grid(row=2, column=0, sticky="w", pady=3)
        glossary_frame = ttk.Frame(frame)
        glossary_frame.grid(row=2, column=1, columnspan=3, sticky="ew", pady=3, padx=(4, 0))
        glossary_frame.columnconfigure(0, weight=1)
        self.glossary_var = StringVar()
        ttk.Entry(glossary_frame, textvariable=self.glossary_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(glossary_frame, text="浏览…", width=8,
                   command=self._pick_glossary).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(glossary_frame, text="清空", width=6,
                   command=lambda: self.glossary_var.set("")).grid(row=0, column=2, padx=(4, 0))

        ttk.Label(frame, text="附加要求：").grid(row=3, column=0, sticky="nw", pady=3)
        self.extra_prompt_text = tk.Text(frame, height=2, wrap="word", relief="solid", borderwidth=1)
        self.extra_prompt_text.grid(row=3, column=1, columnspan=3, sticky="ew", pady=3, padx=(4, 0))

        checks = ttk.Frame(frame)
        checks.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.translate_tables_var = BooleanVar(value=False)
        self.translate_figures_var = BooleanVar(value=False)
        self.translate_headers_var = BooleanVar(value=True)
        self.expand_var = BooleanVar(value=True)
        self.cache_var = BooleanVar(value=True)
        self.notes_var = BooleanVar(value=True)
        ttk.Checkbutton(checks, text="翻译表格内文字", variable=self.translate_tables_var).pack(side=LEFT)
        ttk.Checkbutton(checks, text="翻译图表标注与公式", variable=self.translate_figures_var).pack(side=LEFT, padx=(12, 0))
        ttk.Checkbutton(checks, text="翻译页眉页脚", variable=self.translate_headers_var).pack(side=LEFT, padx=(12, 0))

        checks2 = ttk.Frame(frame)
        checks2.grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(checks2, text="允许译文向下扩展", variable=self.expand_var).pack(side=LEFT)
        ttk.Checkbutton(checks2, text="使用译文缓存（省 token）", variable=self.cache_var).pack(side=LEFT, padx=(12, 0))
        ttk.Checkbutton(checks2, text="生成阅读笔记", variable=self.notes_var).pack(side=LEFT, padx=(12, 0))

        ttk.Label(
            frame, style="Hint.TLabel",
            text="图表（含坐标轴、图例）和独立公式默认原样保留；勾第二项才会一起翻。"
                 "「生成阅读笔记」会额外输出一份 .md，按「问题定义 / 应用场合 / 贡献」三节速读。",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))

    # ------------------------------------------------------------------ #
    def _build_output(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 4. 输出 ", padding=PAD)
        frame.pack(fill=X, pady=(0, PAD))
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="输出目录：").grid(row=0, column=0, sticky="w", pady=3)
        out_frame = ttk.Frame(frame)
        out_frame.grid(row=0, column=1, sticky="ew", pady=3, padx=(4, 0))
        out_frame.columnconfigure(0, weight=1)
        self.output_var = StringVar()
        ttk.Entry(out_frame, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_frame, text="选择…", width=8,
                   command=self._pick_output_dir).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(out_frame, text="打开", width=6,
                   command=lambda: open_in_explorer(self.output_var.get() or ".")).grid(row=0, column=2, padx=(4, 0))

        modes = ttk.Frame(frame)
        modes.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.mode_replaced_var = BooleanVar(value=True)
        self.mode_bilingual_var = BooleanVar(value=True)
        ttk.Checkbutton(modes, text="译文替换版（保留原排版，正文变译文）",
                        variable=self.mode_replaced_var).pack(side=LEFT)
        ttk.Checkbutton(modes, text="双语对照版（左侧原文 / 右侧译文）",
                        variable=self.mode_bilingual_var).pack(side=LEFT, padx=(16, 0))

        ttk.Label(frame, style="Hint.TLabel",
                  text="清除原文方式与双语版方向等高级选项，可在命令行用 --replace-mode / --bilingual-split 调整。"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ------------------------------------------------------------------ #
    def _build_bottom(self, parent: ttk.Frame) -> None:
        """底部固定区：进度条 + 操作按钮。

        这一块**最先** pack 到底部。Tk 按 pack 顺序分配空间，空间不足时最后 pack
        的才会被裁掉，所以「开始翻译」按钮在任何窗口尺寸下都露得出来。
        """
        bottom = ttk.Frame(parent)
        bottom.pack(side=BOTTOM, fill=X)

        bar_frame = ttk.Frame(bottom)
        bar_frame.pack(fill=X, pady=(PAD, 0))
        self.progress_var = DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(bar_frame, variable=self.progress_var, maximum=100.0)
        self.progress_bar.pack(side=LEFT, fill=X, expand=True)
        self.status_var = StringVar(value="就绪")
        ttk.Label(bar_frame, textvariable=self.status_var, width=26, anchor="e").pack(
            side=RIGHT, padx=(PAD, 0))

        actions = ttk.Frame(bottom)
        actions.pack(fill=X, pady=(PAD, 0))
        self.scan_button = ttk.Button(actions, text="仅扫描统计", command=self._scan_only, width=12)
        self.scan_button.pack(side=LEFT)
        self.stop_button = ttk.Button(actions, text="停止", command=self._stop, width=10, state="disabled")
        self.stop_button.pack(side=LEFT, padx=(PAD, 0))
        self.open_button = ttk.Button(actions, text="打开输出目录", command=self._open_output_dir,
                                      width=14, state="disabled")
        self.open_button.pack(side=LEFT, padx=(PAD, 0))
        self.cache_button = ttk.Button(actions, text="清理缓存", command=self._purge_cache, width=10)
        self.cache_button.pack(side=LEFT, padx=(PAD, 0))
        self.start_button = ttk.Button(actions, text="▶  开始翻译", style="Go.TButton",
                                       command=self._start, width=18)
        self.start_button.pack(side=RIGHT)

    # ------------------------------------------------------------------ #
    def _build_log(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text=" 运行日志 ", padding=4)
        frame.pack(fill=BOTH, expand=True, pady=(PAD, 0))

        self.log_text = tk.Text(frame, height=8, wrap="word", state="disabled",
                                background="#1e1e1e", foreground="#d4d4d4",
                                insertbackground="#d4d4d4", relief="flat")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll = ttk.Scrollbar(frame, orient=VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=RIGHT, fill=Y)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("warn", foreground="#e5c07b")
        self.log_text.tag_configure("error", foreground="#e06c75")
        self.log_text.tag_configure("ok", foreground="#98c379")
        self.log_text.tag_configure("info", foreground="#61afef")

    # ------------------------------------------------------------------ #
    # 设置 ↔ 界面
    # ------------------------------------------------------------------ #
    def _load_into_ui(self) -> None:
        settings = self.settings
        self.engine_var.set(
            f"{settings.engine}  |  {ENGINE_PRESETS.get(settings.engine, {}).get('label', '')}"
        )
        self.base_url_var.set(settings.base_url)
        self.api_key_var.set(settings.api_key)
        self.model_var.set(settings.model)
        self.lang_var.set(LANG_CODE_TO_LABEL.get(settings.target_lang, LANG_CODE_TO_LABEL["zh"]))
        self.pages_var.set(settings.page_range)
        self.concurrency_var.set(settings.concurrency)
        self.min_scale_var.set(settings.min_font_scale)
        self.glossary_var.set(settings.glossary_path)
        self.output_var.set(settings.output_dir)
        self.translate_tables_var.set(settings.translate_tables)
        self.translate_figures_var.set(settings.translate_figures)
        self.notes_var.set(settings.generate_notes)
        self.translate_headers_var.set(settings.translate_headers)
        self.expand_var.set(settings.allow_expand_down)
        self.cache_var.set(settings.use_cache)
        if settings.extra_prompt:
            self.extra_prompt_text.insert("1.0", settings.extra_prompt)
        self._update_engine_note()

    def _collect_settings(self) -> Settings:
        settings = self.settings
        engine_key = self.engine_var.get().split("|")[0].strip() or settings.engine
        settings.engine = engine_key
        settings.base_url = self.base_url_var.get().strip()
        settings.model = self.model_var.get().strip()
        settings.api_key = self.api_key_var.get().strip() if self.remember_key_var.get() else ""
        settings.target_lang = LANG_LABEL_TO_CODE.get(self.lang_var.get(), "zh")
        settings.page_range = self.pages_var.get().strip()
        settings.concurrency = int(self.concurrency_var.get() or 4)
        settings.min_font_scale = float(self.min_scale_var.get() or 0.62)
        settings.glossary_path = self.glossary_var.get().strip()
        settings.output_dir = self.output_var.get().strip()
        settings.translate_tables = bool(self.translate_tables_var.get())
        settings.translate_figures = bool(self.translate_figures_var.get())
        settings.generate_notes = bool(self.notes_var.get())
        settings.translate_headers = bool(self.translate_headers_var.get())
        settings.allow_expand_down = bool(self.expand_var.get())
        settings.use_cache = bool(self.cache_var.get())
        settings.extra_prompt = self.extra_prompt_text.get("1.0", "end").strip()
        settings.normalise()
        return settings

    # ------------------------------------------------------------------ #
    # 文件操作
    # ------------------------------------------------------------------ #
    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择要翻译的 PDF",
            initialdir=self.settings.last_open_dir or None,
            filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        )
        self._append_files(list(paths))

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(
            title="选择包含 PDF 的文件夹", initialdir=self.settings.last_open_dir or None
        )
        if not folder:
            return
        found = sorted(str(p) for p in Path(folder).glob("*.pdf"))
        if not found:
            messagebox.showinfo(APP_TITLE, "该文件夹里没有找到 PDF 文件。")
            return
        self._append_files(found)

    def _append_files(self, paths: list[str]) -> None:
        for path in paths:
            if path and path not in self.files:
                self.files.append(path)
                self.file_list.insert(END, Path(path).name)
        if paths:
            self.settings.last_open_dir = str(Path(paths[0]).parent)
            if not self.output_var.get().strip():
                self.output_var.set(str(Path(paths[0]).parent / "译文输出"))

    def _remove_selected(self) -> None:
        for index in sorted(self.file_list.curselection(), reverse=True):
            self.file_list.delete(index)
            del self.files[index]

    def _clear_files(self) -> None:
        self.file_list.delete(0, END)
        self.files.clear()

    def _open_selected(self) -> None:
        selection = self.file_list.curselection()
        if selection:
            open_in_explorer(self.files[selection[0]])

    def _pick_glossary(self) -> None:
        path = filedialog.askopenfilename(
            title="选择术语表",
            filetypes=[("术语表", "*.json *.csv *.tsv *.txt"), ("所有文件", "*.*")],
        )
        if path:
            self.glossary_var.set(path)

    def _pick_output_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择输出目录", initialdir=self.output_var.get() or None)
        if folder:
            self.output_var.set(folder)

    def _open_output_dir(self) -> None:
        if self._last_outputs:
            open_in_explorer(self._last_outputs[0])
        else:
            open_in_explorer(self.output_var.get() or ".")

    # ------------------------------------------------------------------ #
    def _purge_cache(self) -> None:
        """清掉缓存里「译文 == 原文」的条目。

        模型偶尔会整段不译，这类结果一旦缓存下来，之后每次重跑都会命中它、
        那一段就永远是原文了。清掉之后下次会重新翻译这些段落。
        """
        if not messagebox.askyesno(
            APP_TITLE,
            "将清除缓存中「译文和原文完全相同」的条目。\n\n"
            "这些是模型漏翻留下的记录，会让对应段落永远显示英文。\n"
            "已经正常翻译好的内容不会受影响。\n\n确定继续吗？",
        ):
            return
        from .translator import TranslationCache

        settings = self._collect_settings()
        cache = TranslationCache(path=(settings.cache_file or "").strip() or None, enabled=True)
        try:
            removed = cache.purge_untranslated()
            remaining = cache.count()
        finally:
            cache.close()
        self._show_log_page()
        self._log(f"🧹 已清除 {removed} 条无效缓存（「译文==原文」），缓存现有 {remaining} 条记录。", "ok")
        if removed:
            self._log("   重新点「开始翻译」即可把这些漏翻的段落补上（其余内容仍走缓存）。", "info")
        else:
            self._log("   没有发现需要清理的条目。", "info")

    # ------------------------------------------------------------------ #
    # 引擎辅助
    # ------------------------------------------------------------------ #
    def _on_engine_change(self, _event: object = None) -> None:
        key = self.engine_var.get().split("|")[0].strip()
        preset = ENGINE_PRESETS.get(key)
        if not preset:
            return
        self.base_url_var.set(preset["base_url"])
        self.model_var.set(preset["model"])
        self._update_engine_note()

    def _update_engine_note(self) -> None:
        key = self.engine_var.get().split("|")[0].strip()
        preset = ENGINE_PRESETS.get(key, {})
        note = preset.get("note", "")
        env = preset.get("env_key") or ""
        key_url = preset.get("key_url") or ""
        parts = [note]
        if key_url:
            parts.append(f"申请 Key：{key_url}")
        if env:
            parts.append(f"也可用环境变量 {env}")
        self.engine_note.configure(text=" · ".join(p for p in parts if p))

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "●")

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def _log(self, message: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, message + "\n", tag or "")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.start_button, self.scan_button):
            widget.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    def _guard(self) -> bool:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "上一个任务还在运行，请先等待或点「停止」。")
            return False
        if not self.files:
            messagebox.showwarning(APP_TITLE, "请先添加至少一个 PDF 文件。")
            return False
        return True

    def _scan_only(self) -> None:
        if not self._guard():
            return
        self.cancel_flag.clear()
        self._set_busy(True)
        self._show_log_page()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", END)
        self.log_text.configure(state="disabled")
        self.worker = threading.Thread(target=self._scan_worker, args=(list(self.files),), daemon=True)
        self.worker.start()

    def _scan_worker(self, files: list[str]) -> None:
        try:
            extractor = PdfExtractor(log=lambda m: self.messages.put(("log", m)))
            for index, path in enumerate(files, start=1):
                self.messages.put(("status", f"扫描 {index}/{len(files)}"))
                layout = extractor.extract(path)
                stats = layout.stats()
                self.messages.put((
                    "log",
                    f"📄 {Path(path).name}\n"
                    f"    页数 {stats['pages']}（含文字层 {stats['pages_with_text']} 页）\n"
                    f"    文字块 {stats['text_blocks']} 个 / {stats['characters']} 字符\n"
                    f"    跳过区域 {stats['skip_regions']} 处（图片、表格）\n"
                    f"    中位字号 {stats['median_font_size']} pt",
                ))
                if stats["pages_with_text"] == 0:
                    self.messages.put(("warn", "    ⚠ 没有文字层，可能是扫描件，需要先 OCR。"))
            self.messages.put(("status", "扫描完成"))
            self.messages.put(("done_busy", None))
        except Exception as exc:
            self.messages.put(("error", f"扫描失败：{exc}"))
            self.messages.put(("done_busy", None))

    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        if not self._guard():
            return
        settings = self._collect_settings()

        if not settings.resolved_api_key() and settings.engine != "ollama":
            if not messagebox.askyesno(
                APP_TITLE,
                "还没有填写 API Key，继续的话请求会被服务端拒绝。\n要现在去填吗？",
            ):
                pass
            else:
                self.key_entry.focus_set()
                return

        modes: list[str] = []
        if self.mode_replaced_var.get():
            modes.append(MODE_REPLACED)
        if self.mode_bilingual_var.get():
            modes.append(MODE_BILINGUAL)
        if not modes:
            messagebox.showwarning(APP_TITLE, "请至少勾选一种输出模式。")
            return

        try:
            settings.save()
        except OSError:
            pass

        self.cancel_flag.clear()
        self._set_busy(True)
        self._show_log_page()
        self._last_outputs = []
        self.open_button.configure(state="disabled")
        self.progress_var.set(0)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", END)
        self.log_text.configure(state="disabled")
        self._log(f"开始处理 {len(self.files)} 个文件，引擎 {settings.model}", "info")

        self.worker = threading.Thread(
            target=self._translate_worker, args=(settings, list(self.files), modes), daemon=True
        )
        self.worker.start()

    # ------------------------------------------------------------------ #
    def _translate_worker(self, settings: Settings, files: list[str], modes: list[str]) -> None:
        total = len(files)
        all_outputs: list[str] = []
        try:
            for index, path in enumerate(files, start=1):
                if self.cancel_flag.is_set():
                    raise CancelledError("已停止")
                self.messages.put(("log", f"\n【{index}/{total}】{Path(path).name}"))
                task = settings.clone(input_pdf=path)
                setattr(task, "_modes", modes)
                pipeline = Pipeline(
                    task,
                    log=lambda m: self.messages.put(("log", m)),
                    progress=lambda p: self.messages.put(("progress", (index, total, p))),
                    cancel=self.cancel_flag.is_set,
                )
                result = pipeline.run(path)
                all_outputs.extend(result.outputs)
                self.messages.put(("log", result.summary()))
                for warning in result.warnings:
                    self.messages.put(("warn", "⚠ " + warning))
            self.messages.put(("done", all_outputs))
        except CancelledError:
            self.messages.put(("log", "⏹ 已停止。"))
            self.messages.put(("done", all_outputs))
        except Exception as exc:
            self.messages.put(("error", f"❌ {exc}"))
            self.messages.put(("error", traceback.format_exc(limit=6)))
            self.messages.put(("done", all_outputs))

    # ------------------------------------------------------------------ #
    def _test_connection(self) -> None:
        settings = self._collect_settings()
        self.test_button.configure(state="disabled")
        self._log("正在测试 API 连通性…", "info")

        def worker() -> None:
            try:
                message = Pipeline(settings, log=lambda m: self.messages.put(("log", m))).test_connection()
                self.messages.put(("ok", message))
            except Exception as exc:
                self.messages.put(("error", f"连接失败：{exc}"))
            finally:
                self.messages.put(("test_done", None))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    def _stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_flag.set()
            self.status_var.set("正在停止…")
            self._log("⏹ 已发送停止信号，当前请求结束后退出。", "warn")

    # ------------------------------------------------------------------ #
    # 主线程消息泵
    # ------------------------------------------------------------------ #
    def _drain_messages(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "warn":
                    self._log(str(payload), "warn")
                elif kind == "error":
                    self._log(str(payload), "error")
                elif kind == "ok":
                    self._log(str(payload), "ok")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    index, total, state = payload  # type: ignore[misc]
                    self._on_progress(index, total, state)
                elif kind == "done":
                    self._on_done(list(payload))  # type: ignore[arg-type]
                elif kind == "done_busy":
                    self._set_busy(False)
                elif kind == "test_done":
                    self.test_button.configure(state="normal")
        except queue.Empty:
            pass
        self.after(120, self._drain_messages)

    def _on_progress(self, index: int, total: int, state: StageProgress) -> None:
        if state.total:
            ratio = state.current / max(1, state.total)
        else:
            ratio = 0.0
        overall = ((index - 1) + ratio) / max(1, total) * 100.0
        self.progress_var.set(overall)
        label = f"{state.stage} {state.current}/{state.total}" if state.total else state.stage
        self.status_var.set(f"[{index}/{total}] {label}")

    def _on_done(self, outputs: list[str]) -> None:
        self._set_busy(False)
        self.progress_var.set(100.0 if outputs else 0.0)
        self._last_outputs = outputs
        if outputs:
            self.status_var.set("完成")
            self.open_button.configure(state="normal")
            self._log(f"\n✅ 完成，共生成 {len(outputs)} 个文件：", "ok")
            for path in outputs:
                self._log("   " + path, "ok")
        else:
            self.status_var.set("未生成文件")

    # ------------------------------------------------------------------ #
    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "任务仍在运行，确定要退出吗？"):
                return
            self.cancel_flag.set()
        try:
            self._collect_settings().save()
        except Exception:
            pass
        self.destroy()


# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        app = PdfTranslatorApp()
    except tk.TclError as exc:
        print(f"无法启动图形界面：{exc}\n请改用命令行： python run_cli.py --help", file=sys.stderr)
        return 1
    app.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
