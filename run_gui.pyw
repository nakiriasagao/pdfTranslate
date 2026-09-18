#!/usr/bin/env pythonw
"""双击启动图形界面（无控制台窗口版本）。

Windows 上 .pyw 默认用 pythonw.exe 打开，不会弹出黑窗口。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
