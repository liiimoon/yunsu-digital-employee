# -*- coding: utf-8 -*-
"""
云宿小助手 · 飞书机器人 V1（最小闭环版）
功能：长连接接收消息，回显文本，验证收发通路。
运行方式：
  1. pip install lark-oapi
  2. 设置环境变量（推荐，避免密钥写进代码）：
       Windows: set FEISHU_APP_ID=你的AppID && set FEISHU_APP_SECRET=你的AppSecret
       Mac/Linux: export FEISHU_APP_ID=你的AppID && export FEISHU_APP_SECRET=你的AppSecret
  3. python 云宿小助手-bot-v1.py
  4. 在飞书里找到「云宿小助手」机器人，私聊发送任意文字，应收到回显。
"""
import json
import os

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

APP_ID = os.environ.get("FEISHU_APP_ID", "请填入你的AppID")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "请填入你的AppSecret")

# 用于主动调用飞书 API（回复消息）的客户端
client = (
    lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.INFO)
    .build()
)


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

    # 群聊中只响应 @机器人 的消息，避免打扰
    if chat_type == "group" and not msg.mentions:
        return

    if msg.message_type != "text":
        reply_text(message_id, "【云宿小助手】我目前只能处理文字消息哦。")
        return

    text = json.loads(msg.content).get("text", "").strip()
    print(f"收到消息[{chat_type}]: {text}")

    # V1 阶段：只做回显，证明通路；后续版本在这里接知识库
    reply_text(
        message_id,
        f"【云宿小助手】已收到：{text}\n（知识库问答接入中，当前为通路验证版本）",
    )


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
    print("云宿小助手 V1 启动中……（Ctrl+C 停止）")
    ws_client.start()
