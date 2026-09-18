"""PDF 双语翻译器 —— 保留原排版的 PDF 翻译工具。

核心能力：
  * 提取 PDF 文字及其版面信息（坐标 / 字号 / 颜色 / 对齐）
  * 自动识别并跳过图片与表格区域，只翻译正文文字
  * 接入 DeepSeek 及任意 OpenAI 兼容大模型接口进行翻译
  * 输出「译文替换版」与「双语对照版」两份 PDF

对外主要入口：
  - :class:`pdf_translator.config.Settings`   运行配置
  - :class:`pdf_translator.pipeline.Pipeline` 端到端流程
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]

APP_TITLE = "PDF 双语翻译器"
