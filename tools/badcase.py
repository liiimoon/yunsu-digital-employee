# -*- coding: utf-8 -*-
"""badcase 人工复核 CLI（飞轮闭环的「可干预」入口）。

用法：
  python tools/badcase.py list [--status pending] [--category X] [--db PATH]
  python tools/badcase.py confirm <id> [--note "..."]
  python tools/badcase.py ignore <id> [--note "..."]
  python tools/badcase.py promote <id> --suite base|security --expected 作答|拒答|转人工|澄清 \
      --points "标准答案要点" --source "文档名·章节" [--question 覆盖问题文本] [--csv 目标CSV副本]
  python tools/badcase.py report
  python tools/badcase.py mine-keywords

原则：promote / mine-keywords 只提供「晋升」与「建议」，词表修改必须人工确认后手动进行
（human-in-the-loop，防自进化漂移）。
"""
import argparse
import csv
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from src.kernel import config  # noqa: E402
from src.kernel.badcase import MINE_CATEGORIES  # noqa: E402
from src.kernel.db import Database  # noqa: E402

BASE_CSV = config.CORPUS_DIR / "04-评测集" / "评测集-80条.csv"
SECURITY_CSV = config.CORPUS_DIR / "05-安全对抗评测集" / "安全对抗评测集.csv"

# promote 时期望行为 -> （base 编号前缀, 评测集原文风格）
EXPECTED_MAP = {
    "作答": ("H", "作答并附来源"),
    "拒答": ("R", "拒答+替代路径"),
    "转人工": ("X", "立即转人工"),
    "澄清": ("M", "反问澄清"),
}
CSV_FIELDS = ["编号", "分层", "场景", "问题", "标准答案要点", "来源", "期望行为"]


def open_db(args) -> Database:
    return Database(args.db or config.DB_PATH)


# ---------- list ----------
def cmd_list(args):
    db = open_db(args)
    rows = db.list_badcases(status=args.status, category=args.category)
    if not rows:
        print("（无匹配 badcase）")
        return
    print(f"{'ID':<5}{'类别':<10}{'严重度':<8}{'状态':<10}{'时间':<20}问题")
    print("-" * 90)
    for r in rows:
        q = (r["question"] or "").replace("\n", " ")[:36]
        print(f"{r['id']:<5}{r['category']:<10}{r['severity']:<8}{r['status']:<10}{r['ts']:<20}{q}")
    print(f"\n共 {len(rows)} 条")


# ---------- confirm / ignore ----------
def cmd_set_status(args, status):
    db = open_db(args)
    if db.update_status(args.id, status, note=args.note):
        print(f"badcase #{args.id} -> {status}" + (f"（备注：{args.note}）" if args.note else ""))
    else:
        print(f"badcase #{args.id} 不存在")
        sys.exit(1)


# ---------- promote ----------
def _next_id(csv_path: Path, prefix: str) -> str:
    max_n = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            no = row["编号"].strip()
            if no.startswith(prefix) and no[len(prefix):].isdigit():
                max_n = max(max_n, int(no[len(prefix):]))
    return f"{prefix}{max_n + 1:03d}"


def cmd_promote(args):
    db = open_db(args)
    bc = db.get_badcase(args.id)
    if not bc:
        print(f"badcase #{args.id} 不存在")
        sys.exit(1)
    if bc["status"] == "promoted":
        print(f"badcase #{args.id} 已是 promoted 状态，跳过")
        return

    csv_path = Path(args.csv) if args.csv else (BASE_CSV if args.suite == "base" else SECURITY_CSV)
    prefix, expected_text = EXPECTED_MAP[args.expected]
    if args.suite == "security":
        prefix = "S"
    new_id = _next_id(csv_path, prefix)
    question = args.question or bc["question"]
    scene = "对外住客" if bc["user_type"] == "guest" else "对内员工"
    new_row = {
        "编号": new_id, "分层": "线上回流", "场景": scene, "问题": question,
        "标准答案要点": args.points, "来源": args.source, "期望行为": expected_text,
    }

    # 写入前自动备份
    backup_dir = csv_path.parent / "backup"
    backup_dir.mkdir(exist_ok=True)
    backup = backup_dir / f"{csv_path.stem}.{datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(csv_path, backup)

    with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writerow(new_row)
    db.update_status(args.id, "promoted",
                     note=args.note or f"已晋升为 {csv_path.name} 的 {new_id}")
    print(f"已晋升：badcase #{args.id} -> {csv_path.name} 新增 {new_id}（备份：{backup.name}）")
    print(f"  {new_row}")


# ---------- report ----------
def cmd_report(args):
    db = open_db(args)
    rep = db.report()
    print("========== badcase 分布 ==========")
    print("\n按类别：")
    for cat, n in rep["by_category"]:
        print(f"  {cat}: {n}")
    print("\n按状态：")
    for st, n in rep["by_status"]:
        print(f"  {st}: {n}")
    print("\n按日期：")
    for d, n in rep["by_date"][:10]:
        print(f"  {d}: {n}")
    print("\n类别 x 状态：")
    for cat, st, n in rep["by_category_status"]:
        print(f"  {cat} / {st}: {n}")


# ---------- mine-keywords ----------
def _redline_words() -> set:
    """收集现有护栏/红线词表（用于过滤，避免重复建议）。"""
    from src.kernel import redline
    words = set()
    for name in ("GUEST_MONEY", "GUEST_SAFETY", "GUEST_ESCALATE", "STAFF_CATEGORIES",
                 "STAFF_DISPUTE", "STAFF_BUSINESS", "EXPLICIT_TRANSFER",
                 "_SEC_INJECTION_CN", "_SEC_PRIVACY_CN", "_SEC_SOCIAL_CN",
                 "_OUT_OF_SCOPE_COMMON", "_OUT_OF_SCOPE_GUEST", "_OUT_OF_SCOPE_STAFF"):
        words.update(getattr(redline, name, []))
    return words


def cmd_mine_keywords(args):
    import jieba
    db = open_db(args)
    questions = db.confirmed_questions(MINE_CATEGORIES)
    if not questions:
        print(f"暂无 confirmed 状态的 {MINE_CATEGORIES} 类 badcase，先复核一些样例再挖词。")
        return
    existing = _redline_words()
    stop = {"可以", "怎么", "什么", "为什么", "是不是", "你们", "我们", "一下", "这个", "那个"}
    counter = Counter()
    for q in questions:
        for t in jieba.lcut(q or ""):
            t = t.strip("，。、；：:（）()/?？！! ")
            if len(t) >= 2 and t not in stop and not t.isdigit():
                counter[t] += 1
    print(f"从 {len(questions)} 条 confirmed badcase 中提取高频词（已过滤现有护栏词表）：\n")
    print(f"{'候选词':<12}{'频次':<6}建议")
    print("-" * 50)
    suggested = 0
    for word, cnt in counter.most_common(30):
        if word in existing:
            continue
        print(f"{word:<12}{cnt:<6}建议人工评估是否加入 redline.py 相应词表")
        suggested += 1
    if not suggested:
        print("（无新候选词）")
    print("\n注意：以上仅为建议，词表修改须人工确认后手动编辑 src/kernel/redline.py，"
          "并全量跑 eval/run_eval.py 回归（human-in-the-loop）。")


def main():
    parser = argparse.ArgumentParser(description="badcase 复核 CLI")
    parser.add_argument("--db", default="", help="数据库路径（默认取 YUNSU_DB_PATH / data/yunsu.db）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出 badcase")
    p.add_argument("--status", default="", help="按状态过滤 pending/confirmed/ignored/promoted")
    p.add_argument("--category", default="", help="按类别过滤")

    for name in ("confirm", "ignore"):
        p = sub.add_parser(name, help=f"标记为 {name}")
        p.add_argument("id", type=int)
        p.add_argument("--note", default="")

    p = sub.add_parser("promote", help="晋升 badcase 到评测集 CSV")
    p.add_argument("id", type=int)
    p.add_argument("--suite", choices=["base", "security"], required=True)
    p.add_argument("--expected", choices=list(EXPECTED_MAP), required=True)
    p.add_argument("--points", required=True, help="标准答案要点")
    p.add_argument("--source", required=True, help="来源（文档名·章节）")
    p.add_argument("--question", default="", help="覆盖问题文本（默认用原 turn 问题）")
    p.add_argument("--note", default="")
    p.add_argument("--csv", default="", help="目标 CSV 路径覆盖（测试用副本，不污染真实评测集）")

    sub.add_parser("report", help="按类别/状态/日期统计分布")
    sub.add_parser("mine-keywords", help="从 confirmed 误拒/漏拦 badcase 中挖掘护栏词表候选")

    args = parser.parse_args()
    if args.cmd == "list":
        cmd_list(args)
    elif args.cmd in ("confirm", "ignore"):
        cmd_set_status(args, "confirmed" if args.cmd == "confirm" else "ignored")
    elif args.cmd == "promote":
        cmd_promote(args)
    elif args.cmd == "report":
        cmd_report(args)
    elif args.cmd == "mine-keywords":
        cmd_mine_keywords(args)


if __name__ == "__main__":
    main()
