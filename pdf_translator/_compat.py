"""PyMuPDF 导入兼容层与全局行为设置。

PyMuPDF 1.24 之后官方包名从 ``fitz`` 改为 ``pymupdf``，并计划移除 ``fitz`` 别名。
这里统一入口，保证新旧版本都能用，同时避免旧别名打印弃用警告。
"""

from __future__ import annotations

try:  # PyMuPDF >= 1.24
    import pymupdf as fitz
except ImportError:  # pragma: no cover - 老版本
    import fitz  # type: ignore[no-redef]

# 关掉「Consider using the pymupdf_layout package…」这类广告式提示
try:
    fitz.no_recommend_layout()  # type: ignore[attr-defined]
except AttributeError:
    pass

__all__ = ["fitz"]
