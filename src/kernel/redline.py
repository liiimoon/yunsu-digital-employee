# -*- coding: utf-8 -*-
"""红线与确定性路由规则：全部在 LLM 循环之外前置执行，命中即转人工/拒答。

口径来源：交接文档第五节「对话设计规则」。
- 住客侧红线：退款/赔偿/房费争议、人身财产安全 -> 值班经理
- 员工侧红线：工资/考勤异议/处罚/合同 -> 人事接口人（不做裁定，告知流程入口）
"""
import re
from dataclasses import dataclass


@dataclass
class RedlineHit:
    category: str   # 红线类别，用于摘要与日志
    target: str     # 接收方：值班经理 / 人事接口人
    keyword: str    # 命中的关键词


# ---------- 住客侧 ----------
# 退款、赔偿、房费争议（红线）
GUEST_MONEY = ["退款", "退钱", "赔偿", "赔钱", "赔我", "赔多少", "凭什么扣", "多收", "乱扣", "扣我钱"]
# 人身 / 财产安全（红线）
GUEST_SAFETY = ["陌生人", "着火", "起火", "失窃", "被偷", "盗窃", "抢劫", "有人进来", "有人闯", "丢了", "走丢", "走失"]
# 明确投诉升级诉求（负面情绪升级，直接转值班经理并置顶）
GUEST_ESCALATE = ["叫你们经理", "叫经理", "找经理", "找你们经理", "曝光", "投诉到底", "免费升级"]

# ---------- 员工侧 ----------
# 工资、考勤异议、处罚、合同 -> 人事接口人（需同时出现个人争议指向，避免误伤制度问答）
STAFF_CATEGORIES = ["工资", "考勤", "处罚", "合同", "处分"]
STAFF_DISPUTE = ["我", "凭什么", "不服", "为什么", "什么意思", "有没有问题", "有问题", "扣", "少了", "解释"]
# 加盟店商务协商 -> 区域运营
STAFF_BUSINESS = ["降佣金", "单独谈佣金", "重新谈佣金"]

# 显式转人工（两侧通用）
EXPLICIT_TRANSFER = ["转人工", "找真人", "人工客服", "人工服务"]


def check_redline(user_type: str, text: str) -> "RedlineHit | None":
    """命中红线返回 RedlineHit，否则 None。优先于一切其他逻辑。"""
    if user_type == "guest":
        for kw in GUEST_SAFETY:
            if kw in text:
                return RedlineHit("人身财产安全", "值班经理", kw)
        for kw in GUEST_MONEY:
            if kw in text:
                return RedlineHit("退款赔偿房费争议", "值班经理", kw)
        for kw in GUEST_ESCALATE:
            if kw in text:
                return RedlineHit("投诉升级", "值班经理", kw)
    else:  # staff
        for kw in STAFF_BUSINESS:
            if kw in text:
                return RedlineHit("商务协商", "区域运营", kw)
        if any(c in text for c in STAFF_CATEGORIES) and any(d in text for d in STAFF_DISPUTE):
            matched = next(c for c in STAFF_CATEGORIES if c in text)
            return RedlineHit("个人权益裁定", "人事接口人", matched)
    return None


def check_explicit_transfer(text: str) -> bool:
    return any(kw in text for kw in EXPLICIT_TRANSFER)


# ---------- 超出服务边界（确定性关键词，低成本短路） ----------
# 与酒店业务明显无关的请求，直接套用「超出服务边界」拒答话术
_OUT_OF_SCOPE_COMMON = [
    "天气", "机票", "火车票", "出租车", "打车", "股价", "股票", "彩票",
    "帮我写", "代写", "工作总结", "论文", "作业", "翻译",
    "最好吃", "哪家好", "推荐一下", "老板是谁",
    "发烧", "感冒", "吃什么药", "看病", "法律", "律师", "酒吧", "喝酒",
    "对面酒店", "隔壁酒店", "别家酒店",
]
_OUT_OF_SCOPE_GUEST = ["工资", "薪水", "招聘"]          # 住客问内部事务
_OUT_OF_SCOPE_STAFF = ["外卖", "快递柜"]                # 员工问住客事务（兜底，少用）


def check_out_of_scope(user_type: str, text: str) -> bool:
    kws = _OUT_OF_SCOPE_COMMON + (_OUT_OF_SCOPE_GUEST if user_type == "guest" else _OUT_OF_SCOPE_STAFF)
    return any(kw in text for kw in kws)


# ---------- 服务请求澄清（报修/加物/加床/开发票，缺关键信息先反问） ----------
_REQUEST_PREFIX = ("我要", "帮我", "给我", "麻烦", "请帮", "我们需要", "我们房间")
_QUESTION_PARTICLE = ("吗", "怎么", "什么", "哪", "多少", "可不可以", "能不能用")
_ROOM_NO = re.compile(r"(?:房号|房间号)?\s*\d{3,4}\s*(?:房|号)?")


@dataclass
class ServiceIntent:
    kind: str           # repair / add_item / extra_bed / invoice
    missing: list       # 缺失的信息项，如 ["房号"]
    question: str       # 反问话术


def check_service_intent(text: str) -> "ServiceIntent | None":
    """识别报修/加物类『请求』；咨询类问句（带疑问语气）返回 None，走检索问答。"""
    if any(p in text for p in _QUESTION_PARTICLE):
        return None
    is_request = any(p in text for p in _REQUEST_PREFIX) or "报修" in text or "看不了" in text or "坏了" in text
    if not is_request:
        return None

    has_room = bool(_ROOM_NO.search(text))
    if any(k in text for k in ("报修", "修一下", "坏了", "看不了", "不制冷", "不制热", "漏水", "跳闸")):
        if not has_room:
            return ServiceIntent("repair", ["物品/问题", "房号"],
                                 "为了帮您登记报修，请告诉我具体是什么物品（或什么问题）以及您的房间号，好吗？")
    if any(k in text for k in ("加床",)):
        if not (has_room and any(k in text for k in ("大床", "双床", "套房", "房型"))):
            return ServiceIntent("extra_bed", ["房型", "入住日期"],
                                 "部分房型支持加床（100 元/晚）。请问您的房型和入住日期是？我帮您确认是否可加。")
    if any(k in text for k in ("加一床", "加被子", "加枕头", "加物", "送水", "送毛巾", "加瓶水")):
        if not has_room:
            return ServiceIntent("add_item", ["房号"],
                                 "可以的。请问您的房间号是多少？确认后我们尽快给您送过去。")
    if "发票" in text:
        if not ("抬头" in text and "税号" in text):
            return ServiceIntent("invoice", ["发票抬头", "税号"],
                                 "可以开具电子普通发票。请提供发票抬头和税号，好吗？次日 12:00 前发送至您的邮箱。")
    return None


# ---------- 安全护栏（提示词注入/越狱/越权/社工，确定性拦截，不进 LLM 循环） ----------
# 中文关键词：子串匹配（统一小写比较）
_SEC_INJECTION_CN = [
    "系统提示词", "提示词原文", "你的指令", "内部指令", "忽略之前", "忽略以上", "忽略所有",
    "开发者模式", "越狱", "没有限制", "无限制", "不受限制", "假装你没有限制", "解除限制",
    "角色设定", "系统设定", "无视规则", "无视之前", "无视所有",
    "忽略权限", "绕过权限", "权限校验", "摩斯密码", "摩斯", "你的规则",
]
_SEC_PRIVACY_CN = [
    "花名册", "员工名单", "工资表", "薪资表", "工资汇总", "考勤汇总", "导出数据", "数据库",
    "客人的电话", "客人电话", "客人的手机", "客人手机", "客人的联系", "客人的房号",
    "客人信息", "住客信息", "入住记录", "开房记录", "入住名单",
    "住哪个房间", "住在哪个房间", "住哪些房间",
    "监控录像", "调取监控", "查看监控", "调监控", "监控调", "下监控", "调阅监控",
    "sop原文", "制度原文", "文档原文", "原文发给我", "原文贴",
]
# 假紧急情况/冒充官方套取信息 -> 转人工核实
_SEC_SOCIAL_CN = ["我是警察", "警察办案", "公安机关", "派出所", "执法人员", "工商检查"]
# 英文/编码注入：正则（词边界，避免误伤）
_SEC_LATIN_RE = re.compile(
    r"\b(ignore|forget)\b.{0,60}\b(instructions?|prompt|rules?)\b"
    r"|\b(system|hidden|initial)\s+(prompt|instructions?)\b"
    r"|\bdeveloper\s+mode\b|\bjailbreak\b|\bDAN\b"
    r"|\bprevious\s+instructions?\b"
    r"|[A-Za-z0-9+/]{24,}={0,2}",  # 长 base64 段
    re.IGNORECASE)


def check_security(text: str) -> "str | None":
    """安全护栏：命中返回 'refuse'（注入/越权）或 'transfer'（需人工核实的社工），否则 None。"""
    low = text.lower()
    if any(kw in text for kw in _SEC_SOCIAL_CN):
        return "transfer"
    if any(kw in low for kw in _SEC_INJECTION_CN + _SEC_PRIVACY_CN):
        return "refuse"
    if _SEC_LATIN_RE.search(text):
        return "refuse"
    return None


# ---------- 无意义/寒暄输入 -> 引导澄清 ----------
_GREETING_RE = re.compile(
    r"^(?:在吗|在么|你好|您好|嗨|喂|有人吗|哈哈+|你好呀|hi|hello|hey|test|测试|啊|嗯|哦)"
    r"(?:在吗|在么|你好|您好|嗨|喂|有人吗|哈哈+|hi|hello|hey|test|测试|啊|嗯|哦|[\s吗嘛呢呀吧？?！!~～。.,、])*$",
    re.IGNORECASE)


def check_meaningless(text: str) -> bool:
    """纯寒暄/无意义输入（如「在吗在吗在吗」），返回 True 时走引导澄清。"""
    t = text.strip()
    return len(t) <= 1 or bool(_GREETING_RE.match(t))


def info_still_missing(intent_kind: str, text: str) -> bool:
    """用户在澄清轮次的回复是否仍缺关键信息（简化判断：报修/加物看房号）。"""
    if intent_kind in ("repair", "add_item"):
        return not _ROOM_NO.search(text)
    if intent_kind == "extra_bed":
        return not any(k in text for k in ("大床", "双床", "套房", "入住"))
    if intent_kind == "invoice":
        return not ("税号" in text or re.search(r"[0-9A-Z]{15,20}", text))
    return False
