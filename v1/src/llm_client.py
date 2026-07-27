"""
共享 DeepSeek LLM 客户端。所有模块统一从这里获取 client。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

# 确保加载项目根目录的 .env
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_env_path)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

if not DEEPSEEK_API_KEY:
    raise RuntimeError(
        f"DEEPSEEK_API_KEY 未设置！请检查 {_env_path} 文件"
    )

client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)

# CAPTCHA Agent 专用客户端（用更强的推理模型）
CAPTCHA_MODEL = os.getenv("CAPTCHA_MODEL", "deepseek-chat")
captcha_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "deepseek-chat")
