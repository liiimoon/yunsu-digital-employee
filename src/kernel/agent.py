# -*- coding: utf-8 -*-
"""有界自主循环（bounded agent loop）：LLM 通过 tool calling 驱动，最大轮数可配。

终止条件：
1. 模型调用 final_answer / transfer_human / request_clarification -> 立即收敛；
2. 模型未调用工具直接输出文本 -> 视为最终回答；
3. 达到 MAX_LOOP_ITERATIONS 仍未收敛 -> 回落为固定拒答话术 + 提示可转人工。
每轮 token 用量累计到会话，供成本审计（约束 ≤0.10 元/次对话）。
"""
import json
from dataclasses import dataclass, field

from . import config
from .prompts import build_system_prompt

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "检索酒店知识库（住客 FAQ / 内部制度），返回 top-k 相关片段及相关度分数。可换关键词多次调用。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "检索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_clarification",
            "description": "用户信息不足（如报修缺房号）时，向用户反问澄清。调用后本轮对话结束。",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "反问用户的问题"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_human",
            "description": "判断需要人工介入（如用户明确要求、情绪激烈）时调用，转接人工。调用后本轮对话结束。",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string", "description": "转接原因"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": "给出最终回答并结束对话轮。回答只能基于检索到的语料片段，末尾必须附来源。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "最终回复全文"}},
                "required": ["text"],
            },
        },
    },
]


@dataclass
class AgentResult:
    action: str                    # answer / clarify / transfer / refuse
    text: str                      # 回复正文（transfer 时为转接原因说明）
    sources: list = field(default_factory=list)
    iterations: int = 0
    transfer_reason: str = ""
    trace: list = field(default_factory=list)   # 逐步记录，供评测 trace 落盘
    loop_exhausted: bool = False                  # 触顶回落标记


class AgentLoop:
    def __init__(self, llm, retriever):
        self.llm = llm
        self.retriever = retriever

    @staticmethod
    def _classify_final_text(text: str) -> str:
        """终答行为归类：固定拒答话术或自由表述的拒答（含英文/拼音注入的回应）归 refuse。"""
        from .prompts import (
            GUEST_NO_ANSWER, GUEST_OUT_OF_SCOPE,
            STAFF_NO_ANSWER, STAFF_OUT_OF_SCOPE,
        )
        refusal_texts = {GUEST_NO_ANSWER, GUEST_OUT_OF_SCOPE,
                         STAFF_NO_ANSWER, STAFF_OUT_OF_SCOPE}
        hints = ("not able to", "cannot share", "can't share", "unable to share",
                 "i'm sorry, but", "无法提供", "不能提供", "不方便透露")
        t = text.strip()
        # 固定话术用包含匹配（模型可能在话术后追加来源等尾巴）
        if any(r in t for r in refusal_texts) or any(m in t.lower() for m in hints):
            return "refuse"
        return "answer"

    def _do_search(self, query: str) -> tuple:
        """执行检索，返回 (工具结果文本, 来源列表, 命中明细)。"""
        hits = self.retriever.search(query, top_k=config.SEARCH_TOP_K)
        if not hits:
            return "未检索到任何相关语料。", [], []
        parts, sources, hit_detail = [], [], []
        for i, (chunk, score) in enumerate(hits, 1):
            # relevant 标记供 MockLLM 做相关性判定；真实模型自行阅读片段判断
            relevant = self.retriever.is_relevant(query, chunk)
            parts.append(f"[片段{i}][score={score:.2f}][relevant={'1' if relevant else '0'}]"
                         f"（来源：{chunk.source}）\n{chunk.text}")
            sources.append(chunk.source)
            hit_detail.append({"source": chunk.source, "score": round(score, 2),
                               "relevant": relevant, "text": chunk.text[:200]})
        return "\n\n".join(parts), sources, hit_detail

    def run(self, user_type: str, text: str, session) -> AgentResult:
        from .prompts import GUEST_NO_ANSWER, STAFF_NO_ANSWER
        no_answer = GUEST_NO_ANSWER if user_type == "guest" else STAFF_NO_ANSWER

        messages = [{"role": "system", "content": build_system_prompt(user_type, config.MAX_LOOP_ITERATIONS)}]
        messages.extend(session.history[-config.HISTORY_TAIL:])
        messages.append({"role": "user", "content": text})

        all_sources, trace = [], []
        for iteration in range(1, config.MAX_LOOP_ITERATIONS + 1):
            resp = self.llm.chat_with_tools(messages, TOOLS)
            session.add_usage(resp["usage"])
            calls = resp["tool_calls"]

            if not calls:
                # 模型直接输出文本，视为最终回答（拒答内容同样归类）
                trace.append({"step": iteration, "type": "direct_answer"})
                return AgentResult(self._classify_final_text(resp["content"]),
                                   resp["content"], all_sources, iteration, trace=trace)

            for call in calls:
                name, args = call["name"], json.loads(call["arguments"] or "{}")

                if name == "final_answer":
                    trace.append({"step": iteration, "type": "tool_call", "tool": name,
                                  "args": {"text": args["text"][:200]}})
                    action = self._classify_final_text(args["text"])
                    return AgentResult(action, args["text"], all_sources, iteration, trace=trace)
                if name == "request_clarification":
                    trace.append({"step": iteration, "type": "tool_call", "tool": name,
                                  "args": args})
                    return AgentResult("clarify", args["question"], all_sources, iteration, trace=trace)
                if name == "transfer_human":
                    trace.append({"step": iteration, "type": "tool_call", "tool": name,
                                  "args": args})
                    return AgentResult("transfer", "", all_sources, iteration,
                                       transfer_reason=args.get("reason", ""), trace=trace)
                if name == "search_knowledge":
                    result_text, sources, hit_detail = self._do_search(args.get("query", ""))
                    all_sources.extend(s for s in sources if s not in all_sources)
                    trace.append({"step": iteration, "type": "tool_call", "tool": name,
                                  "args": args, "hits": hit_detail})
                    messages.append({
                        "role": "assistant", "content": resp["content"] or "",
                        "tool_calls": [{
                            "id": call["id"], "type": "function",
                            "function": {"name": name, "arguments": call["arguments"]},
                        }],
                    })
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": result_text})
                    break  # 检索结果入上下文，继续下一轮循环

        # 触顶未收敛：回落为固定拒答话术 + 提示可转人工
        trace.append({"step": config.MAX_LOOP_ITERATIONS, "type": "loop_exhausted"})
        return AgentResult("refuse", no_answer, all_sources, config.MAX_LOOP_ITERATIONS,
                           trace=trace, loop_exhausted=True)
