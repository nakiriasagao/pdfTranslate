"""命令行入口的薄封装：``python run_cli.py <参数>``。

真正的实现在 :mod:`pdf_translator.cli`。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pdf_translator.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
