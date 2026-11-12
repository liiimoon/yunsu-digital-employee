# -*- coding: utf-8 -*-
"""LLM 客户端：DeepSeek（OpenAI 兼容 SDK）+ 离线 Mock 桩。

统一返回结构：{"content": str, "tool_calls": [{"id","name","arguments"}], "usage": {...}}
"""
import json
import re

from . import config


class DeepSeekLLM:
    """真实模型客户端，仅在非 mock 模式下创建。"""

    is_mock = False

    def __init__(self, api_key: str = None, base_url: str = None, model: str = None):
        from openai import OpenAI
        key = api_key or config.DEEPSEEK_API_KEY
        if not key:
            raise RuntimeError("缺少环境变量 DEEPSEEK_API_KEY，无法调用真实模型。")
        # 本环境 httpx2 与 Brotli 版本不兼容（解压报 TypeError），要求服务端不压缩响应
        self.client = OpenAI(
            api_key=key,
            base_url=base_url or config.DEEPSEEK_BASE_URL,
            default_headers={"Accept-Encoding": "identity"},
        )
        self.model = model or config.CHAT_MODEL

    def chat_with_tools(self, messages: list, tools: list) -> dict:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools, temperature=0.2)
        msg = resp.choices[0].message
        return {
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                for tc in (msg.tool_calls or [])
            ],
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            },
        }

    def complete(self, prompt: str, model: str = None) -> dict:
        """裁判模型调用：单轮文本补全。"""
        resp = self.client.chat.completions.create(
            model=model or config.JUDGE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0)
        return {
            "content": resp.choices[0].message.content or "",
            "usage": {
                "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
                "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            },
        }


class MockLLM:
    """桩 LLM：不发起任何 API 调用，固定走「检索 -> final_answer」两拍流程。

    用于 --mock 模式验证管线结构：tool calling 循环、session、评测判定全部真实执行，
    只有模型决策被替换为确定性规则。
    - 首轮：调用 search_knowledge，query 取用户最后一条消息。
    - 收到检索结果后：若结果标记「无有效命中」（无 relevant=1 片段）-> final_answer
      拒答话术（从系统提示词中抽取约定的固定话术，与真实模型口径一致）；
      否则 final_answer 拼接命中片段并附来源。
    """

    is_mock = True

    @staticmethod
    def _last_user_text(messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                return m["content"]
        return ""

    @staticmethod
    def _last_tool_content(messages: list) -> str:
        for m in reversed(messages):
            if m.get("role") == "tool":
                return m["content"]
        return ""

    @staticmethod
    def _no_answer_text(messages: list) -> str:
        """从系统提示词中提取约定的「业务内无答案」拒答话术（第一个中文引号内的内容）。"""
        system = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        m = re.search(r"内容固定为：\s*“([^”]+)”", system)
        return m.group(1) if m else "抱歉，这个问题我暂时没有找到准确答案。"

    def chat_with_tools(self, messages: list, tools: list) -> dict:
        tool_content = self._last_tool_content(messages)
        usage = {"prompt_tokens": 0, "completion_tokens": 0}  # mock 零成本
        if not tool_content:
            return {
                "content": "",
                "tool_calls": [{
                    "id": "mock_call_1", "name": "search_knowledge",
                    "arguments": json.dumps({"query": self._last_user_text(messages)}, ensure_ascii=False),
                }],
                "usage": usage,
            }
        # 已有检索结果：任一命中片段被标记相关（relevant=1）才作答，否则拒答
        if "未检索到" in tool_content or "[relevant=1]" not in tool_content:
            return {"content": "", "usage": usage, "tool_calls": [{
                "id": "mock_call_2", "name": "final_answer",
                "arguments": json.dumps({"text": self._no_answer_text(messages)}, ensure_ascii=False),
            }]}
        answer = f"{tool_content}\n（mock 拼装答案，仅验证流程）"
        return {"content": "", "usage": usage, "tool_calls": [{
            "id": "mock_call_2", "name": "final_answer",
            "arguments": json.dumps({"text": answer}, ensure_ascii=False),
        }]}

    def complete(self, prompt: str, model: str = None) -> dict:
        return {"content": '{"pass": true, "reason": "mock 裁判不判分"}',
                "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
