# -*- coding: utf-8 -*-
"""会话状态：内存存储，按 (chat_id, user_id) 隔离，无需持久化。"""
import re
from dataclasses import dataclass, field

_NORMALIZE = re.compile(r"[\s，。！？,.!?~～、；;：:]")

# 简单情绪线索，仅用于转人工摘要的「情绪状态」字段
_NEGATIVE = ["投诉", "曝光", "生气", "差劲", "太差", "垃圾", "过分", "必须", "到底", "凭什么", "赔钱"]


def normalize(text: str) -> str:
    return _NORMALIZE.sub("", text)


@dataclass
class SessionState:
    key: tuple
    user_type: str = "guest"
    history: list = field(default_factory=list)          # [{"role": "user"/"assistant", "content": str}]
    recent_questions: list = field(default_factory=list)  # 归一化问题，重复检测用
    pending_clarification: dict = None                    # {"intent": str, "rounds": int, "origin": str}
    last_action: str = ""                                 # 上一轮回复行为，用于重复计数重置
    prompt_tokens: int = 0
    completion_tokens: int = 0
    turns: int = 0

    def add_usage(self, usage: dict):
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def emotion(self) -> str:
        recent = " ".join(m["content"] for m in self.history if m["role"] == "user")
        return "负面/激动" if any(w in recent for w in _NEGATIVE) else "平稳"

    def transfer_summary(self, core_demand: str = "") -> str:
        """转人工对话摘要五要素：用户身份/核心诉求/已尝试回答/未解决点/情绪状态。"""
        role = {"guest": "住客", "staff": "门店员工"}.get(self.user_type, self.user_type)
        user_msgs = [m["content"] for m in self.history if m["role"] == "user"]
        bot_msgs = [m["content"] for m in self.history if m["role"] == "assistant"]
        demand = core_demand or (user_msgs[-1] if user_msgs else "（未识别）")
        tried = "；".join(m[:50] for m in bot_msgs[-2:]) or "（尚未作答）"
        unresolved = user_msgs[-1] if user_msgs else "（无）"
        return (
            f"用户身份：{role}\n"
            f"核心诉求：{demand[:80]}\n"
            f"已尝试回答：{tried}\n"
            f"未解决点：{unresolved[:80]}\n"
            f"情绪状态：{self.emotion()}"
        )


class SessionStore:
    def __init__(self):
        self._sessions = {}

    def get(self, chat_id: str, user_id: str, user_type: str = "guest") -> SessionState:
        key = (chat_id, user_id)
        if key not in self._sessions:
            self._sessions[key] = SessionState(key=key, user_type=user_type)
        return self._sessions[key]
