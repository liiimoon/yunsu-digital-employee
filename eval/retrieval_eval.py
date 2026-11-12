# -*- coding: utf-8 -*-
"""检索质量离线评测：把「检索失败」与「生成失败」解耦度量。

数据源：04 评测集 / 05 安全对抗评测集中「期望行为为作答/澄清且来源列非空」的条目作为 gold；
拒答/转人工类无 gold 来源，跳过并报告跳过数。

gold 来源列格式示例：「住客FAQ Q01」「考勤制度2-3」「投诉处理SOP3」「前台SOP」；
与 retriever 返回 chunk 的 source（「文档名 · 章节标题」）做两档匹配：
- 文档档：文档名一致即算命中；
- 章节档：章节号/Q 号一致（如 Q01 ↔ Q01，§2 ↔ 2. xxx），范围号（2-3）任一命中即算。

指标：Recall@1/3/5（文档档）、章节精确命中@5、MRR；
输出整体指标 + 按文档/按分层分解 + 未命中条目清单，写入 eval/output/retrieval-*.json。
纯本地计算，不调用任何 LLM API。
"""
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.kernel import config  # noqa: E402
from src.kernel.retriever import Retriever  # noqa: E402

TOP_K = 5
BASE_CSV = config.CORPUS_DIR / "04-评测集" / "评测集-80条.csv"
SECURITY_CSV = config.CORPUS_DIR / "05-安全对抗评测集" / "安全对抗评测集.csv"
OUT_DIR = Path(__file__).resolve().parent / "output"


def parse_gold(src: str) -> tuple:
    """解析来源列 -> (gold_doc, gold_sections)。gold_sections 为 Q 号或章节号列表。"""
    src = src.strip()
    m = re.search(r"(Q\d+)", src)
    if m:
        return src[: m.start()].strip(" ·-—"), [m.group(1)]
    m = re.search(r"(\d+(?:\s*-\s*\d+)?)\s*$", src)
    if m:
        doc = src[: m.start()].strip(" ·-—")
        nums = [n.strip() for n in m.group(1).split("-")]
        if len(nums) == 2:  # 范围号如 2-3 -> [2, 3]
            secs = [str(i) for i in range(int(nums[0]), int(nums[1]) + 1)]
        else:
            secs = nums
        return doc, secs
    return src.strip(" ·-—"), []


def doc_match(gold_doc: str, chunk_doc: str) -> bool:
    return gold_doc == chunk_doc or gold_doc in chunk_doc or chunk_doc in gold_doc


def section_match(gold_secs: list, chunk_section: str) -> bool:
    if not gold_secs:
        return False
    for gs in gold_secs:
        if gs.startswith("Q"):
            if chunk_section.startswith(gs):  # Q01. xxx
                return True
        else:
            m = re.match(r"\s*(\d+)\s*[.、]", chunk_section)  # 2. xxx
            if m and m.group(1) == gs:
                return True
    return False


def collect_gold_rows() -> tuple:
    """返回 (gold_rows, skipped)。gold_rows 元素含编号/分层/问题/gold。"""
    gold_rows, skipped = [], 0
    for path in (BASE_CSV, SECURITY_CSV):
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                expected = row["期望行为"]
                src = row["来源"].strip()
                if ("作答" in expected or "澄清" in expected) and src and src != "-":
                    doc, secs = parse_gold(src)
                    gold_rows.append({
                        "编号": row["编号"], "分层": row["分层"], "问题": row["问题"],
                        "gold来源": src, "gold_doc": doc, "gold_secs": secs,
                    })
                else:
                    skipped += 1
    return gold_rows, skipped


def main():
    retriever = Retriever(config.CORPUS_DIR)
    gold_rows, skipped = collect_gold_rows()
    print(f"gold 条目：{len(gold_rows)}（跳过无 gold 来源/非作答澄清类：{skipped}）")
    print(f"语料 chunk 数：{len(retriever.chunks)}，top_k={TOP_K}\n")

    details = []
    for row in gold_rows:
        hits = retriever.search(row["问题"], top_k=TOP_K)
        doc_rank, sec_rank = None, None
        for i, (chunk, score) in enumerate(hits, 1):
            if doc_rank is None and doc_match(row["gold_doc"], chunk.doc):
                doc_rank = i
            if sec_rank is None and section_match(row["gold_secs"], chunk.section):
                sec_rank = i
        details.append({
            **{k: row[k] for k in ("编号", "分层", "问题", "gold来源")},
            "doc_rank": doc_rank, "sec_rank": sec_rank,
            "top": [{"source": c.source, "score": round(s, 2)} for c, s in hits],
        })

    n = len(details)

    def recall(k, key="doc_rank"):
        return sum(1 for d in details if d[key] and d[key] <= k) / n

    metrics = {
        "Recall@1": round(recall(1), 4),
        "Recall@3": round(recall(3), 4),
        "Recall@5": round(recall(5), 4),
        "章节精确命中@5": round(sum(1 for d in details if d["sec_rank"]) / n, 4),
        "MRR": round(sum(1 / d["doc_rank"] for d in details if d["doc_rank"]) / n, 4),
    }

    print("========== 整体指标（文档档） ==========")
    for k, v in metrics.items():
        print(f"{k}: {v:.1%}" if k != "MRR" else f"{k}: {v:.3f}")

    # 按文档分解
    print("\n========== 按 gold 文档分解（Recall@5） ==========")
    by_doc = {}
    for d in details:
        g = d["gold来源"]
        doc = g.split()[0] if " " in g else re.sub(r"(Q\d+|\d+(-\d+)?)$", "", g).strip(" ·-—")
        stat = by_doc.setdefault(doc, {"hit": 0, "total": 0})
        stat["total"] += 1
        stat["hit"] += int(bool(d["doc_rank"]))
    for doc, stat in sorted(by_doc.items()):
        print(f"{doc}: {stat['hit']}/{stat['total']}")

    # 按分层分解
    print("\n========== 按分层分解（Recall@5） ==========")
    by_layer = {}
    for d in details:
        stat = by_layer.setdefault(d["分层"], {"hit": 0, "total": 0})
        stat["total"] += 1
        stat["hit"] += int(bool(d["doc_rank"]))
    for layer, stat in sorted(by_layer.items()):
        print(f"{layer}: {stat['hit']}/{stat['total']}")

    # 未命中清单（文档档 top5 未命中）
    misses = [d for d in details if not d["doc_rank"]]
    sec_misses = [d for d in details if d["doc_rank"] and not d["sec_rank"]]
    print(f"\n========== 文档档未命中（{len(misses)} 条） ==========")
    for d in misses:
        print(f"- {d['编号']} [{d['分层']}] {d['问题'][:30]}")
        print(f"  gold: {d['gold来源']}")
        print(f"  top3: {[(t['source'][:20], t['score']) for t in d['top'][:3]]}")
    print(f"\n========== 章节档未命中（文档命中但章节不符，{len(sec_misses)} 条） ==========")
    for d in sec_misses:
        print(f"- {d['编号']} gold={d['gold来源']} | doc_rank={d['doc_rank']}"
              f" | top章节: {[t['source'][:18] for t in d['top'][:3]]}")

    OUT_DIR.mkdir(exist_ok=True)
    out_file = OUT_DIR / f"retrieval-{datetime.now():%Y%m%d-%H%M%S}.json"
    out_file.write_text(json.dumps({
        "time": datetime.now().isoformat(), "top_k": TOP_K, "gold总数": n, "跳过": skipped,
        "metrics": metrics, "details": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入：{out_file}")


if __name__ == "__main__":
    main()
