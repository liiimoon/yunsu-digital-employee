# 云宿数字员工

基于飞书的连锁酒店数字员工（求职作品集项目）。渠道层（飞书）与内核层（检索 + 有界 agent loop + 规则引擎）解耦，内核可脱离飞书离线调用与评测。

## 目录结构

```
corpus/                 语料库 V2.0（01-对内制度 / 02-对外FAQ / 03-工单Mock / 04-评测集 / 05-安全对抗评测集）
docs/高并发与稳定性设计.md  生产部署架构设计（限流/削峰/缓存/熔断/成本熔断）
src/
  channels/feishu_bot.py  飞书渠道适配器（长连接，仅协议适配）
  kernel/
    config.py             环境变量配置（模型、循环上限、价格）
    llm.py                DeepSeek 客户端（OpenAI 兼容 SDK）+ Mock 桩
    redline.py            红线 / 安全护栏 / 超边界 / 服务请求澄清等确定性规则（LLM 之外前置执行）
    retriever.py          章节切分 + jieba + BM25 检索（标题加权、通用词过滤、同义词扩展）
    prompts.py            住客/员工两套系统提示词（含全局口径）+ 裁判 prompt
    session.py            会话状态（澄清轮数、重复问题、token 用量、转人工摘要）
    agent.py              有界自主循环（tool calling，默认最多 3 轮，含 trace 采集）
    core.py               内核入口 handle(user_type, text, session)
eval/
  run_eval.py             评测 harness（离线，六维指标体系，--suite base|security|all）
  load_test.py            负载测试（asyncio 并发，吞吐量/延迟分位数/错误率）
  retrieval_eval.py       检索质量离线评测（Recall@k / MRR，解耦检索与生成）
tools/
  badcase.py              badcase 复核 CLI（list/confirm/ignore/promote/report/mine-keywords）
data/yunsu.db             SQLite 持久化（turns 全量 trace + badcases，env YUNSU_DB_PATH 覆盖）
docs/
  高并发与稳定性设计.md     生产部署架构设计
  自进化与badcase飞轮.md    badcase 采集→复核→晋升→回归的飞轮设计
```

## 运行

```bash
pip install -r requirements.txt

# 1) 评测（mock 模式，无需 API Key，验证管线）
python eval/run_eval.py --mock                  # 全部 suite（基础 80 条 + 安全对抗 60 条）
python eval/run_eval.py --mock --suite security # 只跑安全对抗评测集（含安全拦截率）

# 2) 负载测试（mock 基准，无需 API Key）
python eval/load_test.py --concurrency 50 --requests 500

# 2.5) 检索质量离线评测（Recall@1/3/5、MRR，不调 LLM）
python eval/retrieval_eval.py

# 2.6) badcase 复核（飞轮闭环：list → confirm → promote → 回归）
python tools/badcase.py list --status pending
python tools/badcase.py report
python tools/badcase.py promote <id> --suite base --expected 拒答 --points "要点" --source "文档·章节"

# 3) 评测（真实模型）
set DEEPSEEK_API_KEY=你的key        # Windows；Mac/Linux 用 export
python eval/run_eval.py
python eval/load_test.py --real --concurrency 3 --requests 20   # 真实模型小流量冒烟（限速 1 QPS）

# 4) 启动飞书机器人（长连接，无公网回调）
set FEISHU_APP_ID=你的AppID
set FEISHU_APP_SECRET=你的AppSecret
python -m src.channels.feishu_bot
```

可选环境变量：`MAX_LOOP_ITERATIONS`（默认 3）、`DEEPSEEK_CHAT_MODEL`（默认 deepseek-chat）、
`DEEPSEEK_JUDGE_MODEL`（默认 deepseek-chat）、`YUNSU_FEISHU_USER_TYPE`（飞书侧默认 staff，可改 guest）、
`YUNSU_CORPUS_DIR`（语料目录）、`YUNSU_DB_PATH`（SQLite 路径，默认 data/yunsu.db）、
`YUNSU_DB=off`（禁用落库）、`YUNSU_JUDGE_SAMPLE_RATE`（抽样裁判比例，默认 0.2）。

## 关键设计

- **处理顺序**：红线检测 → 安全护栏（注入/越狱/越权 → 固定话术拒答；冒充官方套信息 → 转人工核实）
  → 显式转人工 → 澄清轮次接续 → 寒暄/无意义输入引导 → 超边界短路 → 缺信息反问
  → 重复问题（2 次）主动询问转人工 → 有界 agent loop。
- **Agent loop**：工具 `search_knowledge` / `request_clarification` / `transfer_human` / `final_answer`；
  终止于 final_answer/transfer_human/clarify 或达到 `MAX_LOOP_ITERATIONS`，触顶回落为固定拒答话术。
- **红线**（交接文档第五节）：住客侧退款/赔偿/房费争议、人身财产安全 → 值班经理；员工侧工资/考勤异议/处罚/合同 → 人事接口人。确定性关键词，不进 LLM 循环。
- **拒答话术**：业务内无答案 / 超出服务边界 / 安全拦截三类，住客侧用文档原文（安全类为 V1.1 新增）。
- **转人工**必附对话摘要五要素（用户身份/核心诉求/已尝试回答/未解决点/情绪状态）。
- **成本审计**：每轮 token 用量累计在会话上，评测输出单次对话估算成本（约束 ≤0.10 元）。
- **评测指标体系**：行为准确率（作答≥90%/拒答≥95%/转人工 100%）+ 可审计（来源附著率 100%、
  逐条 trace 落盘）+ 可干预（接管入口可达性）+ 自主性（自主解决率/平均 loop/触顶回落率）
  + 成本 + 性能（首响 P95<5s）+ 用户体验（转人工率<30%）；安全 suite 另报安全拦截率（目标 100%）。
- **负载测试**：`eval/load_test.py` mock 基准（并发 50/请求 500）约 6600 req/s、P95 0.4ms、
  错误率 0%（仅框架开销）；生产部署设计见 `docs/高并发与稳定性设计.md`。
- **持久化与飞轮**：每轮对话全量 trace 落 SQLite（`data/yunsu.db`）；
  落库后同步运行六类 badcase 信号检测（五类规则 + 抽样裁判，规则零 LLM 成本）；
  人工用 `tools/badcase.py` 复核并一键晋升到评测集，形成「采集→分类→复核→晋升→回归→修复」
  闭环，词表变更必须人工确认（human-in-the-loop）。详见 `docs/自进化与badcase飞轮.md`。
