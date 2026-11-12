# -*- coding: utf-8 -*-
"""云宿小助手 · 飞书渠道适配器（长连接模式）。

保留 V1 的收发逻辑：长连接接收 im.message.receive_v1 事件、群聊只响应 @ 机器人、
按消息 ID 回复文本。渠道只负责协议适配，业务逻辑全部在内核。

运行方式：
  1. pip install -r requirements.txt
  2. 设置环境变量：
       FEISHU_APP_ID / FEISHU_APP_SECRET   飞书自建应用凭证
       DEEPSEEK_API_KEY                    DeepSeek 密钥
  3. python -m src.channels.feishu_bot
"""
import json
import os
import sys
import threading
from collections import deque
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.kernel.core import Kernel  # noqa: E402
from src.kernel.llm import DeepSeekLLM  # noqa: E402

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
# 飞书一期为对内渠道，默认员工侧；如需对外演示可改 guest
USER_TYPE = os.environ.get("YUNSU_FEISHU_USER_TYPE", "staff")

# 主动调用飞书 API（回复消息）的客户端
client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)

# 内核：渠道可插拔，业务逻辑与飞书协议完全解耦（惰性创建，避免 import 时要求密钥）
_kernel = None

# message_id 去重（防止平台重推导致重复处理），保留最近 500 条
_seen_ids = deque()
_seen_set = set()

# 超过该秒数未回复时，先回一条受理提示，避免用户因等待重发
ACK_DELAY_SECONDS = 2.0


def _is_duplicate(message_id: str) -> bool:
    if message_id in _seen_set:
        return True
    _seen_set.add(message_id)
    _seen_ids.append(message_id)
    if len(_seen_ids) > 500:
        _seen_set.discard(_seen_ids.popleft())
    return False


def get_kernel() -> Kernel:
    global _kernel
    if _kernel is None:
        _kernel = Kernel(llm=DeepSeekLLM())
    return _kernel


def reply_text(message_id: str, text: str) -> None:
    """按消息 ID 回复一条文本消息"""
    body = ReplyMessageRequestBody.builder() \
        .content(json.dumps({"text": text}, ensure_ascii=False)) \
        .msg_type("text") \
        .build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = client.im.v1.message.reply(req)
    if not resp.success():
        print("回复失败:", resp.code, resp.msg)


def do_message_receive(data: P2ImMessageReceiveV1) -> None:
    """收到消息时的处理入口"""
    msg = data.event.message
    chat_type = msg.chat_type  # p2p=私聊, group=群聊
    message_id = msg.message_id

    # 平台重推去重：同一 message_id 只处理一次
    if _is_duplicate(message_id):
        print(f"[去重] 忽略平台重推: {message_id}")
        return

    # 群聊中只响应 @机器人 的消息，避免打扰
    if chat_type == "group" and not msg.mentions:
        print(f"[忽略] 群聊消息未 @ 机器人: {message_id}")
        return

    if msg.message_type != "text":
        reply_text(message_id, "【云宿小助手】我目前只能处理文字消息哦。")
        return

    text = json.loads(msg.content).get("text", "").strip()
    user_id = data.event.sender.sender_id.open_id if data.event.sender else "unknown"
    print(f"收到消息[{chat_type}][{user_id}][{message_id}]: {text}")

    session = get_kernel().sessions.get(msg.chat_id, user_id, user_type=USER_TYPE)

    # 慢响应兜底：超过 ACK_DELAY_SECONDS 未处理完，先回受理提示，避免用户重发
    ack_timer = threading.Timer(
        ACK_DELAY_SECONDS, reply_text,
        args=(message_id, "【云宿小助手】收到，正在为您查询，请稍候……"),
    )
    ack_timer.start()
    try:
        reply = get_kernel().handle(
            USER_TYPE, text, session,
            meta={"channel": "feishu", "message_id": message_id})
    finally:
        ack_timer.cancel()
    print(f"  -> 行为={reply.action} loop轮数={reply.iterations} tokens={session.total_tokens}")
    reply_text(message_id, f"【云宿小助手】{reply.text}")


# 注册事件：接收消息 im.message.receive_v1
event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_message_receive)
    .build()
)

# 长连接模式：无需公网地址，本机主动连飞书
ws_client = lark.ws.Client(
    APP_ID,
    APP_SECRET,
    event_handler=event_handler,
    log_level=lark.LogLevel.INFO,
)

if __name__ == "__main__":
    if not APP_ID or not APP_SECRET:
        print("请先设置环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET。")
        sys.exit(1)
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("请先设置环境变量 DEEPSEEK_API_KEY。")
        sys.exit(1)
    print("云宿小助手启动中……（Ctrl+C 停止）")
    ws_client.start()
