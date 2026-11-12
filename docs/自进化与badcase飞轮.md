# 云宿数字员工 · 自进化与 badcase 飞轮

> 目标：让系统上线后能够**持续发现并修复自己的问题**，而不是一次性交付后静止腐烂。
> 核心原则：**机器负责发现与建议，人负责决策与发布**（human-in-the-loop）。
> 更新日期：2026-11-11

## 1. 总体架构

```
渠道层（飞书/H5） → 内核 handle() → TurnLogger 落库（每轮全量 trace，SQLite）
                                        ↓
                              SignalDetector（5 类规则信号 + 抽样裁判）→ badcases 表
                                        ↓
                              Review CLI（人工复核 confirm/ignore）→ promote 一键晋升评测集
                                        ↓
                              回归门禁（run_eval 全量 + 安全 suite）→ 修复 → 发布
```

数据流是单向的：**采集 → 分类 → 复核 → 晋升 → 回归 → 修复 → 发布**。
任何一环都不允许机器自动改知识库/词表后直接上线（见第 5 节「为什么必须 human-in-the-loop」）。

## 2. 持久化（`src/kernel/db.py`）

SQLite（标准库，零新依赖），默认 `data/yunsu.db`，env `YUNSU_DB_PATH` 覆盖，`YUNSU_DB=off` 禁用。

- `turns`：每轮对话全量记录（渠道/chat_id/user_id/问题/回复/action/loop 轮数/sources/trace/token/latency/是否触顶）
- `badcases`：信号检测产物（turn_id 外键/类别/严重度/信号明细/status/reviewer_note）

状态机：`pending → confirmed / ignored → promoted`（晋升后回写，防重复晋升）。

## 3. badcase 分类法（taxonomy）与六类信号（`src/kernel/badcase.py`）

| 信号 | 类别 | 严重度 | 触发逻辑 |
|---|---|---|---|
| answer_followed_by_transfer | 答案不满意 | high | 上一轮作答，本轮用户显式输入「转人工」 |
| repeat_after_answer | 答案未解决 | medium | 上一轮作答，本轮归一化问题在清空前的 recent_questions 快照中 |
| loop_exhausted | loop未收敛 | medium | agent loop 达到 MAX_LOOP_ITERATIONS 触顶回落 |
| refuse_then_rephrase_hit | 疑似误拒 | medium | 上一轮拒答，本轮换说法后检索出现 relevant 命中 |
| negative_emotion_no_transfer | 情绪未安抚 | low | 会话情绪判定为「负面/激动」但本轮未转人工 |
| judge_sample | 疑似错误答案 | high | 抽样裁判校验「答案是否被检索片段支持」不通过 |

严重度语义：high = 可能影响用户权益/事实正确性，优先复核；medium = 体验受损；
low = 提示性信号，批量复核。

规则信号**零 LLM 成本**，可全量运行；只有 judge_sample 消耗 token（见第 6 节成本控制）。

## 4. Review CLI（`tools/badcase.py`）

```bash
python tools/badcase.py list [--status pending] [--category 疑似误拒]
python tools/badcase.py confirm <id> [--note "..."]     # 确认是真 badcase
python tools/badcase.py ignore <id>                     # 误报，忽略
python tools/badcase.py promote <id> --suite base --expected 拒答 \
    --points "标准答案要点" --source "文档名·章节"        # 晋升到评测集（写前自动备份）
python tools/badcase.py report                          # 类别/状态/日期分布
python tools/badcase.py mine-keywords                   # 从 confirmed 误拒/漏拦中挖护栏词表候选
```

- `promote`：编号按 suite 顺延（base 沿用 H/R/X/M 前缀，security 用 S 前缀），写入前自动备份到
  `corpus/**/backup/`；分层标记为「线上回流」，可回溯来源。
- `mine-keywords`：jieba 高频词 − 已有 redline 词表 = 候选词。**只建议，不自动改**。

## 5. 为什么必须 human-in-the-loop（防自进化漂移）

1. **信号 ≠ 事实**：规则信号是启发式（如「疑似误拒」可能只是用户换了话题），
   直接按信号改词表会把噪声固化进系统。
2. **晋升即基准**：进入评测集的条目会成为未来所有回归的判据，错误晋升会污染门禁本身。
3. **可干预是 PRD 硬要求**：转人工、拒答、词表变更都必须留人工决策点与审计痕迹
   （reviewer_note、backup、trace 落库都是为此服务）。
4. 因此本设计中机器的权限上限是「发现 + 建议」（badcase、候选词清单），
   人的动作是「确认 + 晋升 + 改词表」，发布前必须过回归门禁。

## 6. 成本控制（抽样裁判）

- judge_sample 仅对 `action=answer` 且**非 mock** 的对话按 `YUNSU_JUDGE_SAMPLE_RATE`
  （默认 0.2）抽样，每条只多一次裁判调用（约 500 token），单次对话成本仍远低于 0.10 元约束。
- 裁判调用异常**静默降级**：只打 warning，不写 badcase、不阻塞回复、不影响主链路 SLA。
- mock 模式下裁判恒不触发（`MockLLM.is_mock`），评测与压测零额外成本。

## 7. 演进路径

1. **P0（当前）**：SQLite 单机落库 + 规则信号 + 抽样裁判 + CLI 复核。
2. **存储扩展**：SQLite → PostgreSQL（多实例写入、并发复核、看板查询）；
   turns 表量级大后按日期分区或归档。
3. **检测增强**：规则检测 → 增加 LLM 兜底分类器（对规则未覆盖的 badcase 做离线批量归类），
   类别体系随业务扩展（如新增「安全漏拦」自动检测：对抗样本变体重放）。
4. **飞轮提速**：confirmed badcase 聚类（同因归并）、promote 后自动触发回归、
   回归失败自动回滚词表变更。
