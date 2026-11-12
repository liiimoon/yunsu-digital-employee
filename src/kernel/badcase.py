# -*- coding: utf-8 -*-
"""badcase 自动信号检测：每轮落库后同步运行，全部低成本规则 + 抽样裁判。

六类信号（命中即写入 badcases 表，状态 pending，等待人工复核）：
1. answer_followed_by_transfer 答案不满意（high）：上一轮作答，本轮用户显式要求转人工
2. repeat_after_answer        答案未解决（medium）：上一轮作答，本轮重复同一问题
3. loop_exhausted             loop未收敛（medium）：agent loop 触顶回落
4. refuse_then_rephrase_hit   疑似误拒（medium）：上一轮拒答，本轮换说法但检索有 relevant 命中
5. negative_emotion_no_transfer 情绪未安抚（low）：用户情绪负面但未转人工
6. judge_sample               疑似错误答案（high）：抽样裁判校验「答案是否被检索片段支持」

设计原则：规则检测零 LLM 成本；抽样裁判按 YUNSU_JUDGE_SAMPLE_RATE 控制成本，
调用异常静默降级（只记 warning，绝不阻塞回复）。
"""
import json
import random
import re

from . import config, redline
from .session import normalize

_SUPPORT_JUDGE_PROMPT = """你是质检裁判。给定用户问题、知识库检索片段和机器人答案，
判断答案是否被检索片段支持（要点一致、无编造、无矛盾）。

【用户问题】{question}
【检索片段】
{snippets}
【机器人答案】{answer}

只输出 JSON，不要输出其他内容：{{"supported": true或false, "reason": "一句话理由"}}"""

# 类别 -> 默认严重度（供 report / mine-keywords 使用）
CATEGORY_疑似误拒 = "疑似误拒"
CATEGORY_漏拦 = "漏拦"  # 预留：安全漏拦类（当前由人工复核时手工归类）
MINE_CATEGORIES = (CATEGORY_疑似误拒, CATEGORY_漏拦)


class SignalDetector:
    def __init__(self, db, retriever=None, llm=None):
        self.db = db
        self.retriever = retriever
        self.llm = llm

    # ---------- 抽样裁判（唯一消耗 token 的信号） ----------
    def _judge_supported(self, question: str, reply) -> tuple:
        """返回 (是否通过, 理由)。异常时返回 (True, '裁判调用失败，跳过')。"""
        snippets = []
        for step in reply.trace:
            for hit in step.get("hits", []):
                snippets.append(f"（{hit['source']}）{hit.get('text', '')[:200]}")
        if not snippets:
            return True, "无检索片段，跳过校验"
        prompt = _SUPPORT_JUDGE_PROMPT.format(
            question=question, snippets="\n".join(snippets[:3]), answer=reply.text[:500])
        try:
            resp = self.llm.complete(prompt)
            m = re.search(r"\{.*\}", resp["content"], re.S)
            data = json.loads(m.group(0)) if m else {}
            return bool(data.get("supported", True)), str(data.get("reason", ""))
        except Exception as e:  # noqa: BLE001 - 失败静默降级，不阻塞回复
            print(f"[badcase] 抽样裁判调用失败（已降级跳过）: {e}")
            return True, "裁判调用失败，跳过"

    def inspect(self, session, prev_action: str, prev_questions: list,
                question: str, reply, turn_id: int) -> list:
        """运行全部信号检测，命中写入 badcases。返回命中信号列表。"""
        fired = []

        def fire(signal: str, category: str, severity: str, detail: str = ""):
            fired.append({"signal": signal, "category": category,
                          "severity": severity, "detail": detail})

        # 1. 答案不满意：作答后用户显式转人工
        if prev_action == "answer" and redline.check_explicit_transfer(question):
            fire("answer_followed_by_transfer", "答案不满意", "high",
                 "作答后用户显式要求转人工")

        # 2. 答案未解决：作答后重复同一问题（用清空前的 recent_questions 快照判断）
        if prev_action == "answer" and normalize(question) in prev_questions:
            fire("repeat_after_answer", "答案未解决", "medium",
                 "作答后用户重复同一问题")

        # 3. loop 未收敛：触顶回落
        if reply.loop_exhausted:
            fire("loop_exhausted", "loop未收敛", "medium",
                 f"达到 MAX_LOOP_ITERATIONS={config.MAX_LOOP_ITERATIONS} 未收敛")

        # 4. 疑似误拒：上轮拒答，本轮检索有 relevant 命中
        if prev_action == "refuse" and self.retriever is not None:
            hits = self.retriever.search(question, top_k=1)
            if hits and self.retriever.is_relevant(question, hits[0][0]):
                fire("refuse_then_rephrase_hit", CATEGORY_疑似误拒, "medium",
                     f"换说法后检索命中：{hits[0][0].source}")

        # 5. 情绪未安抚：负面情绪但未转人工
        if session.emotion() == "负面/激动" and reply.action != "transfer":
            fire("negative_emotion_no_transfer", "情绪未安抚", "low",
                 f"情绪=负面/激动，action={reply.action}")

        # 6. 抽样裁判：答案是否被检索片段支持（非 mock 才抽样，控制成本）
        is_mock = getattr(self.llm, "is_mock", True)
        if (reply.action == "answer" and not is_mock and self.llm is not None
                and random.random() < config.JUDGE_SAMPLE_RATE):
            supported, reason = self._judge_supported(question, reply)
            if not supported:
                fire("judge_sample", "疑似错误答案", "high", reason)

        for s in fired:
            self.db.insert_badcase(turn_id, s["category"], s["severity"], [s])
        return fired
