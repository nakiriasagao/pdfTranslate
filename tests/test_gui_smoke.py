"""GUI 冒烟测试：验证界面能正常构建、控件齐备、设置能往返读写。

    python tests/test_gui_smoke.py

窗口全程保持隐藏（withdraw），不会打扰你。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认是 GBK，输出 ✓/✗ 会直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASSED: list[str] = []
FAILED: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  ✓ {label}")
    else:
        FAILED.append(f"{label} {detail}".strip())
        print(f"  ✗ {label}  {detail}")


def main() -> int:
    try:
        import tkinter as tk
    except ImportError as exc:
        print(f"跳过：本机没有 tkinter（{exc}）")
        return 0

    try:
        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except tk.TclError as exc:
        print(f"跳过：当前环境没有图形显示（{exc}）")
        return 0

    from pdf_translator.config import ENGINE_PRESETS
    from pdf_translator.gui import LANG_CODE_TO_LABEL, PdfTranslatorApp

    app = PdfTranslatorApp()
    app.withdraw()  # 不弹窗
    try:
        print("[1] 界面构建")
        check(app.title().startswith("PDF"), "窗口标题正确", app.title())
        for name, widget in (
            ("文件列表", app.file_list), ("引擎下拉", app.engine_combo),
            ("日志框", app.log_text), ("进度条", app.progress_bar),
            ("开始按钮", app.start_button), ("停止按钮", app.stop_button),
            ("测试连接按钮", app.test_button),
        ):
            check(widget is not None, f"{name} 已创建")

        print("\n[2] 引擎预设联动")
        for key, preset in ENGINE_PRESETS.items():
            app.engine_var.set(f"{key}  |  {preset['label']}")
            app._on_engine_change()
            ok = app.base_url_var.get() == preset["base_url"] and app.model_var.get() == preset["model"]
            check(ok, f"切换到 {key} 自动填充 Base URL / 模型",
                  f"{app.base_url_var.get()} / {app.model_var.get()}")

        print("\n[3] 设置收集")
        app.engine_var.set("deepseek  |  DeepSeek 深度求索（推荐）")
        app._on_engine_change()
        app.api_key_var.set("sk-test-123")
        app.lang_var.set(LANG_CODE_TO_LABEL["en"])
        app.pages_var.set("1-5,8")
        app.concurrency_var.set(6)
        app.translate_tables_var.set(True)
        app.cache_var.set(False)
        collected = app._collect_settings()
        check(collected.engine == "deepseek", "引擎被正确收集", collected.engine)
        check(collected.api_key == "sk-test-123", "API Key 被正确收集")
        check(collected.target_lang == "en", "目标语言被正确收集", collected.target_lang)
        check(collected.page_range == "1-5,8", "页码范围被正确收集", collected.page_range)
        check(collected.concurrency == 6, "并发数被正确收集", str(collected.concurrency))
        check(collected.translate_tables is True, "表格翻译开关被正确收集")
        check(collected.use_cache is False, "缓存开关被正确收集")

        print("\n[4] 设置持久化往返")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "settings.json"
            collected.base_url = "https://example.com/v1"
            collected.model = "my-model"
            collected.save(target)
            check(target.exists(), "设置文件已写出")
            from pdf_translator.config import Settings

            reloaded = Settings.load(target)
            check(reloaded.base_url == "https://example.com/v1", "Base URL 往返一致", reloaded.base_url)
            check(reloaded.model == "my-model", "模型名往返一致", reloaded.model)
            check(reloaded.target_lang == "en", "目标语言往返一致", reloaded.target_lang)
            target.write_text("{ this is not json", encoding="utf-8")
            fallback = Settings.load(target)
            check(fallback.target_lang == "zh", "设置文件损坏时回退到默认值")

        print("\n[5] 文件列表增删")
        app.files.clear()
        app.file_list.delete(0, "end")
        app._append_files([str(ROOT / "samples" / "sample_en.pdf")])
        check(len(app.files) == 1, "添加文件成功")
        check(app.file_list.size() == 1, "列表同步显示")
        app.file_list.selection_set(0)
        app._remove_selected()
        check(len(app.files) == 0, "移除选中成功")

        print("\n[6] 小窗口下「开始翻译」按钮必须可见（回归：曾因 pack 顺序被挤出可视区）")
        app.attributes("-alpha", 0.0)  # 全透明，避免打扰
        app.deiconify()
        app.update()
        tabs = app.notebook.tabs()
        check(len(tabs) == 3, "界面分为 3 个标签页（小屏放得下）", f"实际 {len(tabs)} 页")
        print(f"     内容自然尺寸：{app.winfo_reqwidth()} x {app.winfo_reqheight()}，"
              f"屏幕：{app.winfo_screenwidth()} x {app.winfo_screenheight()}")
        check(app.winfo_reqheight() <= app.winfo_screenheight() - 60,
              "内容自然高度不超过屏幕可用高度",
              f"需要 {app.winfo_reqheight()}px，屏幕仅 {app.winfo_screenheight()}px")

        for size in ("780x470", "700x400", "640x330"):
            app.geometry(size)
            app.update_idletasks()
            app.update()
            win_h = app.winfo_height()
            for name, widget in (("开始翻译按钮", app.start_button),
                                 ("进度条", app.progress_bar),
                                 ("停止按钮", app.stop_button)):
                if not widget.winfo_ismapped():
                    check(False, f"{size} 下{name}已显示", "控件未映射")
                    continue
                top = widget.winfo_rooty() - app.winfo_rooty()
                bottom = top + widget.winfo_height()
                visible = widget.winfo_height() > 1 and top >= 0 and bottom <= win_h + 1
                check(visible, f"{size} 下{name}在可视区内",
                      f"top={top} bottom={bottom} 窗口高={win_h}")

        print("\n[7] 开始翻译时自动切到日志页")
        app.geometry("820x520")
        app.notebook.select(0)
        app.update()
        check(app.notebook.index(app.notebook.select()) == 0, "初始停在文件页")
        app._show_log_page()
        app.update()
        check(app.notebook.index(app.notebook.select()) == 2, "调用后切到日志页")

        app.withdraw()
    finally:
        app.destroy()

    print("\n" + "═" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print("  - " + item)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
