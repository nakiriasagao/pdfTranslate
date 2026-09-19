"""配置管理：AI 引擎预设 + 用户设置持久化。

设置文件位置： ``%APPDATA%/pdf-translator/settings.json`` （Windows）
或 ``~/.config/pdf-translator/settings.json`` （其它平台）。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_ID = "pdf-translator"

# --------------------------------------------------------------------------- #
# AI 引擎预设（全部走 OpenAI 兼容的 /chat/completions 协议）
# --------------------------------------------------------------------------- #
ENGINE_PRESETS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek 深度求索（推荐）",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "性价比最高，中英互译质量好。模型：deepseek-chat / deepseek-reasoner",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "env_key": "OPENAI_API_KEY",
        "key_url": "https://platform.openai.com/api-keys",
        "note": "模型：gpt-4o-mini / gpt-4o / gpt-4.1-mini 等",
    },
    "moonshot": {
        "label": "Kimi 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "env_key": "MOONSHOT_API_KEY",
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长文本能力强",
    },
    "dashscope": {
        "label": "阿里通义千问 Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env_key": "DASHSCOPE_API_KEY",
        "key_url": "https://bailian.console.aliyun.com/",
        "note": "模型：qwen-plus / qwen-max / qwen-turbo",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "env_key": "ZHIPUAI_API_KEY",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "glm-4-flash 有免费额度",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "env_key": "SILICONFLOW_API_KEY",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "国内聚合平台，可切换多种开源模型",
    },
    "ollama": {
        "label": "本地 Ollama（无需联网 / 无需 Key）",
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "env_key": "",
        "key_url": "https://ollama.com/download",
        "note": "需先本地运行 ollama serve；API Key 随便填或留空",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容端点",
        "base_url": "",
        "model": "",
        "env_key": "OPENAI_API_KEY",
        "key_url": "",
        "note": "任何兼容 /v1/chat/completions 的服务都可以填在这里",
    },
}

DEFAULT_ENGINE = "deepseek"

# --------------------------------------------------------------------------- #
# 目标语言
# --------------------------------------------------------------------------- #
TARGET_LANGUAGES: dict[str, str] = {
    "zh": "简体中文",
    "zh-TW": "繁体中文",
    "en": "English 英语",
    "ja": "日本語 日语",
    "ko": "한국어 韩语",
    "fr": "Français 法语",
    "de": "Deutsch 德语",
    "es": "Español 西班牙语",
    "ru": "Русский 俄语",
    "pt": "Português 葡萄牙语",
    "it": "Italiano 意大利语",
    "ar": "العربية 阿拉伯语",
}

# 这些语言的译文需要嵌入 CJK 字体
CJK_LANGS = {"zh", "zh-TW", "ja", "ko"}

# 输出模式
MODE_REPLACED = "replaced"   # 译文替换原文字（版式最接近原文）
MODE_BILINGUAL = "bilingual"  # 左原文 / 右译文 双栏对照


def settings_dir() -> Path:
    """返回配置目录（会自动创建）。"""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return settings_dir() / "settings.json"


def cache_dir() -> Path:
    """翻译缓存目录。"""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    path = base / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    """一次翻译任务的全部参数。"""

    # --- 输入 / 输出 ---
    input_pdf: str = ""
    output_dir: str = ""
    output_suffix: str = "_译文"
    output_suffix_bilingual: str = "_双语对照"

    # --- 引擎 ---
    engine: str = DEFAULT_ENGINE
    base_url: str = ENGINE_PRESETS[DEFAULT_ENGINE]["base_url"]
    api_key: str = ""
    model: str = ENGINE_PRESETS[DEFAULT_ENGINE]["model"]
    temperature: float = 0.2
    timeout: int = 180
    max_retries: int = 4
    concurrency: int = 4

    # --- 翻译行为 ---
    target_lang: str = "zh"
    source_lang: str = "auto"
    extra_prompt: str = ""
    glossary_path: str = ""
    skip_translated: bool = True       # 源文本已是目标语言时跳过
    translate_tables: bool = False     # 默认不翻译表格
    translate_headers: bool = True     # 页眉页脚
    translate_figures: bool = False    # 图表内的标注与独立公式，默认原样保留

    # --- 版面重建 ---
    replace_mode: str = "redact"       # redact=真正删除原文字 / cover=白底遮盖
    min_font_scale: float = 0.62       # 译文最小可缩放到原字号的百分比
    max_font_scale: float = 1.15       # 译文最大可放大到原字号的百分比
    line_spacing: float = 1.0          # 行距倍数
    allow_expand_down: bool = True     # 空间不够时允许向下扩展
    bilingual_gap: float = 18.0        # 双语版左右栏之间的间隙（pt）
    bilingual_split: str = "vertical"  # vertical=左右分栏 / horizontal=上下分栏
    show_original_bilingual: bool = True  # 双语版左栏是否保留原文

    # --- 范围 ---
    page_range: str = ""               # 例如 "1-5,8,10-"；留空=全部
    max_chars_per_request: int = 2400  # 单次请求打包的最大字符数
    max_items_per_request: int = 12    # 单次请求打包的最大段落数
    use_cache: bool = True
    cache_file: str = ""               # 缓存库路径；留空=用户缓存目录

    # --- 阅读笔记 ---
    generate_notes: bool = True        # 翻译的同时生成一份速读笔记（Markdown）
    notes_suffix: str = "_阅读笔记"
    notes_detailed: bool = True        # 再按小标题逐节做精读（主题/名词/关键内容）
    notes_max_sections: int = 30       # 精读的章节数上限
    notes_section_chars: int = 3500    # 每节送给模型的字符上限

    # --- 界面 ---
    last_open_dir: str = ""

    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        valid = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in valid}
        obj = cls(**clean)
        obj.normalise()
        return obj

    def normalise(self) -> None:
        """修正越界或非法的取值。"""
        if self.engine not in ENGINE_PRESETS:
            self.engine = DEFAULT_ENGINE
        if self.target_lang not in TARGET_LANGUAGES:
            self.target_lang = "zh"
        if self.replace_mode not in ("redact", "cover"):
            self.replace_mode = "redact"
        if self.bilingual_split not in ("vertical", "horizontal"):
            self.bilingual_split = "vertical"
        self.concurrency = max(1, min(int(self.concurrency or 1), 16))
        self.max_retries = max(0, min(int(self.max_retries or 0), 10))
        self.timeout = max(10, min(int(self.timeout or 60), 900))
        self.min_font_scale = max(0.3, min(float(self.min_font_scale), 1.0))
        self.max_font_scale = max(1.0, min(float(self.max_font_scale), 2.5))
        self.line_spacing = max(0.7, min(float(self.line_spacing), 2.5))
        self.max_chars_per_request = max(200, min(int(self.max_chars_per_request), 12000))
        self.max_items_per_request = max(1, min(int(self.max_items_per_request), 60))
        self.bilingual_gap = max(0.0, min(float(self.bilingual_gap), 120.0))
        self.temperature = max(0.0, min(float(self.temperature), 2.0))

    def apply_engine_preset(self, key: str, keep_key: bool = True) -> None:
        """切换到某个引擎预设（默认保留已填写的 API Key）。"""
        preset = ENGINE_PRESETS.get(key)
        if not preset:
            return
        self.engine = key
        self.base_url = preset["base_url"]
        self.model = preset["model"]
        if not keep_key:
            self.api_key = ""

    def resolved_api_key(self) -> str:
        """返回可用的 API Key：优先用户填写，其次读环境变量。"""
        if self.api_key.strip():
            return self.api_key.strip()
        preset = ENGINE_PRESETS.get(self.engine, {})
        env_name = preset.get("env_key") or ""
        if env_name:
            return os.environ.get(env_name, "").strip()
        return ""

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = Path(path) if path else settings_path()
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                return cls.from_dict(data)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        return cls()

    # ------------------------------------------------------------------ #
    def clone(self, **overrides: Any) -> "Settings":
        data = self.to_dict()
        data.update(overrides)
        return Settings.from_dict(data)
