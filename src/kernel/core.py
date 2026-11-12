# -*- coding: utf-8 -*-
"""内核入口：handle(user_type, text, session, meta=None) -> reply。

处理顺序（硬性规则优先于 LLM，全部确定性执行）：
1. 红线检测 -> 直接转人工（不进 LLM 循环）
1.5. 安全护栏 -> 注入/越权拒答，冒充官方转人工
2. 显式「转人工」-> 转人工 + 附对话摘要五要素
3. 澄清轮次接续（报修/加物缺房号等，最多 2 轮）
4. 超出服务边界关键词 -> 固定拒答话术
5. 服务请求缺关键信息 -> 反问澄清（会话状态记录轮数）
6. 同一问题重复 2 次未解决 -> 主动询问是否转人工
7. 其余 -> 有界 agent loop（检索 + LLM 作答）

每轮结束后：计量 latency -> turns 落库 -> SignalDetector 信号检测（badcases 落库）。
"""
import time
from dataclasses import dataclass, field

from . import config, redline
from .agent import AgentLoop
from .prompts import (
    GUEST_NO_ANSWER, GUEST_OUT_OF_SCOPE, STAFF_NO_ANSWER, STAFF_OUT_OF_SCOPE,
    GUEST_SECURITY_REFUSE, STAFF_SECURITY_REFUSE,
)
from .retriever import Retriever
from .session import SessionStore, normalize


@dataclass
class KernelReply:
    text: str
    action: str                  # answer / refuse / transfer / clarify
    sources: list = field(default_factory=list)
    iterations: int = 0          # agent loop 轮数（0 表示未进 LLM）
    trace: list = field(default_factory=list)        # loop 步骤 / tool 调用 / 检索命中
    loop_exhausted: bool = False  # 是否触顶回落


def _no_answer(user_type: str) -> str:
    return GUEST_NO_ANSWER if user_type == "guest" else STAFF_NO_ANSWER


def _out_of_scope(user_type: str) -> str:
    return GUEST_OUT_OF_SCOPE if user_type == "guest" else STAFF_OUT_OF_SCOPE


def _human_name(user_type: str, target: str = "") -> str:
    return target or ("前台" if user_type == "guest" else "值班经理")


class Kernel:
    """渠道无关内核：飞书 / H5 / 评测 harness 共用。

    db_path：None 时按 config（YUNSU_DB_PATH / YUNSU_DB=off）决定；
    传 ":memory:" 用内存库（测试）；显式禁用可设 env YUNSU_DB=off。
    """

    def __init__(self, llm, corpus_dir=None, db_path=None):
        self.retriever = Retriever(corpus_dir or config.CORPUS_DIR)
        self.agent = AgentLoop(llm, self.retriever)
        self.sessions = SessionStore()
        self.db = None
        self.detector = None
        path = db_path if db_path is not None else (config.DB_PATH if config.DB_ENABLED else None)
        if path:
            from .badcase import SignalDetector
            from .db import Database
            self.db = Database(path)
            self.detector = SignalDetector(self.db, retriever=self.retriever, llm=llm)

    # ---------- 转人工回复（附对话摘要五要素） ----------
    def _transfer_reply(self, session, target: str, reason: str = "") -> KernelReply:
        if session.user_type == "guest" and target == "值班经理":
            head = "非常抱歉给您带来不便。您的情况我已记录，正在为您转接值班经理，请稍候；如有紧急情况请立即拨打前台分机 0。"
        elif session.user_type == "guest":
            head = "好的，正在为您转接前台同事，请稍候；您也可以直接拨打前台分机 0。"
        elif target == "人事接口人":
            head = ("您的问题涉及个人权益事项，我无权作出裁定。按制度，请在工资发放后 5 个工作日内"
                    "向人事部门提出复核申请；我已为您转接人事接口人。")
        else:
            head = f"好的，已为您转接{target}，请稍候。"
        summary = session.transfer_summary(core_demand=reason)
        return KernelReply(f"{head}\n——转接摘要——\n{summary}", "transfer")

    def handle(self, user_type: str, text: str, session, meta: dict = None) -> KernelReply:
        """外层包装：重复计数重置 + latency 计量 + 落库 + 信号检测，实际逻辑在 _handle。

        meta：渠道侧上下文，如 {"channel": "feishu", "message_id": ...}；
        eval 不传时 channel 默认为 'eval'，保持向后兼容。
        """
        meta = meta or {}
        prev_action = session.last_action
        # 快照：answer 后清空前的 recent_questions，供 repeat_after_answer 信号判断
        prev_questions = list(session.recent_questions)
        # 上一轮已解决（answer）时重置重复计数：再次提问视为新诉求，而非"重复未解决"
        if prev_action == "answer":
            session.recent_questions.clear()

        tok_before = (session.prompt_tokens, session.completion_tokens)
        t0 = time.perf_counter()
        reply = self._handle(user_type, text, session)
        latency_ms = (time.perf_counter() - t0) * 1000
        session.last_action = reply.action

        if self.db is not None:
            chat_id, user_id = session.key
            turn_id = self.db.insert_turn({
                "channel": meta.get("channel", "eval"),
                "chat_id": chat_id, "user_id": user_id, "user_type": user_type,
                "message_id": meta.get("message_id"),
                "question": text, "reply": reply.text, "action": reply.action,
                "iterations": reply.iterations, "sources": reply.sources,
                "trace": reply.trace,
                "prompt_tokens": session.prompt_tokens - tok_before[0],
                "completion_tokens": session.completion_tokens - tok_before[1],
                "latency_ms": round(latency_ms, 2),
                "loop_exhausted": reply.loop_exhausted,
            })
            self.detector.inspect(session, prev_action, prev_questions, text, reply, turn_id)
        return reply

    def _handle(self, user_type: str, text: str, session) -> KernelReply:
        text = text.strip()
        session.turns += 1

        # 1. 红线检测（最优先，不进 LLM 循环）
        hit = redline.check_redline(user_type, text)
        if hit:
            session.history.append({"role": "user", "content": text})
            reply = self._transfer_reply(session, hit.target, reason=f"{hit.category}（命中：{hit.keyword}）")
            session.history.append({"role": "assistant", "content": reply.text})
            return reply

        # 1.5 安全护栏：注入/越狱/越权 -> 固定话术拒答；冒充官方套信息 -> 转人工核实
        sec = redline.check_security(text)
        if sec:
            session.history.append({"role": "user", "content": text})
            if sec == "transfer":
                reply = self._transfer_reply(session, _human_name(user_type, "值班经理"),
                                             reason="涉及身份核实或敏感信息请求，需人工处理")
            else:
                refuse_text = GUEST_SECURITY_REFUSE if user_type == "guest" else STAFF_SECURITY_REFUSE
                reply = KernelReply(refuse_text, "refuse")
            session.history.append({"role": "assistant", "content": reply.text})
            session.recent_questions.append(normalize(text))
            return reply

        # 2. 显式转人工
        if redline.check_explicit_transfer(text):
            session.history.append({"role": "user", "content": text})
            reply = self._transfer_reply(session, _human_name(user_type), reason="用户主动要求转人工")
            session.history.append({"role": "assistant", "content": reply.text})
            return reply

        # 3. 澄清轮次接续：用户正在补充信息
        if session.pending_clarification:
            pending = session.pending_clarification
            if redline.info_still_missing(pending["intent"], text):
                pending["rounds"] += 1
                session.history.append({"role": "user", "content": text})
                if pending["rounds"] >= config.MAX_CLARIFY_ROUNDS:
                    session.pending_clarification = None
                    reply = KernelReply(
                        "为避免耽误您的时间，建议您直接联系前台（分机 0），或输入「转人工」由同事协助处理。",
                        "refuse")
                else:
                    reply = KernelReply(
                        f"还需要补充一下：{pending['question']}", "clarify")
                session.history.append({"role": "assistant", "content": reply.text})
                return reply
            # 信息补齐：合并原始诉求与补充信息后进入正常流程（P0 不真建工单）
            origin = pending["origin"]
            kind = pending["intent"]
            session.pending_clarification = None
            merged = f"{origin}（补充信息：{text}）"
            session.history.append({"role": "user", "content": text})
            if kind == "invoice":
                confirm = (f"收到。已登记开票信息：{text}。电子发票将在当日申请后次日 12:00 前发送至您的邮箱，"
                           "如需修改抬头或税号请直接告诉我。")
            else:
                confirm = (f"收到，已为您登记：{origin}，补充信息「{text}」。"
                           "工单草稿已生成，请您确认后提交；同事接单后会尽快处理。如需修改请直接告诉我。")
            reply = KernelReply(confirm, "answer")
            session.history.append({"role": "assistant", "content": reply.text})
            session.recent_questions.append(normalize(merged))
            return reply

        # 3.5 纯寒暄/无意义输入 -> 引导澄清（不消耗 LLM）
        if redline.check_meaningless(text):
            session.history.append({"role": "user", "content": text})
            reply = KernelReply(
                "您好，我在。请问有什么可以帮您？比如退房时间、早餐、Wi-Fi、停车等问题都可以直接问我。"
                if user_type == "guest" else
                "您好，我在。请问有什么可以帮您？考勤、报销、前台 SOP、会员权益、加盟店政策等问题都可以直接问我。",
                "clarify")
            session.history.append({"role": "assistant", "content": reply.text})
            return reply

        # 4. 超出服务边界（确定性关键词短路，零 token 成本）
        if redline.check_out_of_scope(user_type, text):
            session.history.append({"role": "user", "content": text})
            reply = KernelReply(_out_of_scope(user_type), "refuse")
            session.history.append({"role": "assistant", "content": reply.text})
            session.recent_questions.append(normalize(text))
            return reply

        # 5. 服务请求缺关键信息 -> 反问澄清（记录轮数，最多 2 轮）
        intent = redline.check_service_intent(text)
        if intent:
            session.pending_clarification = {
                "intent": intent.kind, "rounds": 1, "origin": text, "question": intent.question,
            }
            session.history.append({"role": "user", "content": text})
            reply = KernelReply(intent.question, "clarify")
            session.history.append({"role": "assistant", "content": reply.text})
            session.recent_questions.append(normalize(text))
            return reply

        # 6. 同一问题重复 2 次未解决 -> 主动询问是否转人工
        norm = normalize(text)
        if session.recent_questions.count(norm) >= config.REPEAT_QUESTION_LIMIT - 1 and norm in session.recent_questions:
            human = "前台（分机 0）" if user_type == "guest" else "值班经理"
            session.history.append({"role": "user", "content": text})
            reply = KernelReply(
                f"抱歉之前没能解决您的问题。需要我为您转接{human}吗？您也可以输入「转人工」。",
                "transfer")
            session.history.append({"role": "assistant", "content": reply.text})
            return reply
        session.recent_questions.append(norm)

        # 7. 进入有界 agent loop
        session.history.append({"role": "user", "content": text})
        result = self.agent.run(user_type, text, session)
        reply = KernelReply(result.text, result.action, result.sources, result.iterations,
                            trace=result.trace, loop_exhausted=result.loop_exhausted)
        if result.action == "transfer":
            transfer = self._transfer_reply(session, _human_name(user_type), reason=result.transfer_reason)
            reply = KernelReply(transfer.text, "transfer", result.sources, result.iterations,
                                trace=result.trace, loop_exhausted=result.loop_exhausted)
        session.history.append({"role": "assistant", "content": reply.text})
        return reply
