#!/usr/bin/env python
"""图形界面启动入口 —— 双击本文件（或 run_gui.pyw）即可打开窗口。

命令行运行也可以：  python run_gui.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许直接双击运行时正确找到包目录
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _check_dependencies() -> bool:
    missing: list[str] = []
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401
        except ImportError:
            missing.append("PyMuPDF")
    try:
        import requests  # noqa: F401
    except ImportError:
        missing.append("requests")
    if missing:
        message = (
            "缺少依赖：" + "、".join(missing) + "\n\n"
            "请在命令行执行：\n"
            f"    \"{sys.executable}\" -m pip install -r requirements.txt\n"
        )
        try:
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror("PDF 双语翻译器", message)
            root.destroy()
        except Exception:
            pass
        print(message, file=sys.stderr)
        return False
    return True


def main() -> int:
    if not _check_dependencies():
        return 1
    from pdf_translator.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
