# -*- coding: utf-8 -*-
"""全局配置：全部从环境变量读取，密钥绝不硬编码。"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 支持从项目根目录 .env 读取密钥（不覆盖已存在的系统环境变量）
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

# 语料库目录
CORPUS_DIR = Path(os.environ.get("YUNSU_CORPUS_DIR", str(PROJECT_ROOT / "corpus")))

# DeepSeek（OpenAI 兼容接口）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
CHAT_MODEL = os.environ.get("DEEPSEEK_CHAT_MODEL", "deepseek-chat")      # 作答模型
JUDGE_MODEL = os.environ.get("DEEPSEEK_JUDGE_MODEL", "deepseek-chat")    # 裁判模型

# Agent loop 参数
MAX_LOOP_ITERATIONS = int(os.environ.get("MAX_LOOP_ITERATIONS", "3"))
SEARCH_TOP_K = int(os.environ.get("SEARCH_TOP_K", "3"))

# 会话规则
MAX_CLARIFY_ROUNDS = 2        # 反问澄清最多 2 轮
REPEAT_QUESTION_LIMIT = 2     # 同一问题重复 2 次未解决 -> 主动询问转人工
HISTORY_TAIL = 6              # 带入上下文的最近消息条数

# 成本审计（DeepSeek 参考价，元 / 百万 tokens，可按实际调整）
PRICE_INPUT_PER_M = float(os.environ.get("PRICE_INPUT_PER_M", "2"))
PRICE_OUTPUT_PER_M = float(os.environ.get("PRICE_OUTPUT_PER_M", "8"))

# 持久化（SQLite）：YUNSU_DB=off 禁用；YUNSU_DB_PATH 覆盖默认路径
DB_ENABLED = os.environ.get("YUNSU_DB", "on").lower() != "off"
DB_PATH = os.environ.get("YUNSU_DB_PATH", str(PROJECT_ROOT / "data" / "yunsu.db"))

# 抽样裁判：answer 行为按比例抽样校验「答案是否被检索片段支持」（控制成本）
JUDGE_SAMPLE_RATE = float(os.environ.get("YUNSU_JUDGE_SAMPLE_RATE", "0.2"))
