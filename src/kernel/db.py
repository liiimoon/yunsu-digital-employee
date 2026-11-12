# -*- coding: utf-8 -*-
"""持久化层：SQLite（标准库，零新依赖），默认存 data/yunsu.db。

两张表：
- turns：每轮对话全量 trace 落库（渠道/用户/问题/回复/行为/loop 信息/token/延迟）
- badcases：信号检测命中的疑似 badcase，人工复核后流转状态
  （pending -> confirmed/ignored -> promoted）
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    channel TEXT DEFAULT 'eval',
    chat_id TEXT,
    user_id TEXT,
    user_type TEXT,
    message_id TEXT,
    question TEXT,
    reply TEXT,
    action TEXT,
    iterations INTEGER DEFAULT 0,
    sources TEXT DEFAULT '[]',
    trace TEXT DEFAULT '[]',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    loop_exhausted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS badcases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'low',   -- high / medium / low
    signals TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', -- pending / confirmed / ignored / promoted
    reviewer_note TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_badcases_status ON badcases(status);
CREATE INDEX IF NOT EXISTS idx_badcases_category ON badcases(category);
"""

# badcases 状态流转
STATUSES = ("pending", "confirmed", "ignored", "promoted")


class Database:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # ---------- turns ----------
    def insert_turn(self, record: dict) -> int:
        cols = ("ts", "channel", "chat_id", "user_id", "user_type", "message_id",
                "question", "reply", "action", "iterations", "sources", "trace",
                "prompt_tokens", "completion_tokens", "latency_ms", "loop_exhausted")
        row = {c: record.get(c) for c in cols}
        row["ts"] = row["ts"] or datetime.now().isoformat(timespec="seconds")
        row["sources"] = json.dumps(row["sources"] or [], ensure_ascii=False)
        row["trace"] = json.dumps(row["trace"] or [], ensure_ascii=False)
        row["loop_exhausted"] = int(bool(row["loop_exhausted"]))
        cur = self.conn.execute(
            f"INSERT INTO turns ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [row[c] for c in cols])
        self.conn.commit()
        return cur.lastrowid

    # ---------- badcases ----------
    def insert_badcase(self, turn_id: int, category: str, severity: str, signals: list) -> int:
        cur = self.conn.execute(
            "INSERT INTO badcases (ts, turn_id, category, severity, signals, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (datetime.now().isoformat(timespec="seconds"), turn_id, category, severity,
             json.dumps(signals, ensure_ascii=False)))
        self.conn.commit()
        return cur.lastrowid

    def list_badcases(self, status: str = None, category: str = None) -> list:
        sql = ("SELECT b.*, t.question, t.reply, t.action, t.user_type, t.channel "
               "FROM badcases b JOIN turns t ON t.id = b.turn_id")
        cond, args = [], []
        if status:
            cond.append("b.status = ?")
            args.append(status)
        if category:
            cond.append("b.category = ?")
            args.append(category)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY b.id DESC"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def get_badcase(self, badcase_id: int) -> dict:
        r = self.conn.execute(
            "SELECT b.*, t.question, t.reply, t.action, t.user_type, t.channel "
            "FROM badcases b JOIN turns t ON t.id = b.turn_id WHERE b.id = ?",
            (badcase_id,)).fetchone()
        return dict(r) if r else None

    def update_status(self, badcase_id: int, status: str, note: str = None) -> bool:
        assert status in STATUSES, f"非法状态: {status}"
        cur = self.conn.execute(
            "UPDATE badcases SET status = ?, reviewer_note = COALESCE(?, reviewer_note), "
            "updated_at = ? WHERE id = ?",
            (status, note, datetime.now().isoformat(timespec="seconds"), badcase_id))
        self.conn.commit()
        return cur.rowcount > 0

    def report(self) -> dict:
        """按类别/状态/日期统计分布。"""
        def rows(sql):
            return [tuple(r) for r in self.conn.execute(sql)]
        return {
            "by_category": rows("SELECT category, COUNT(*) FROM badcases GROUP BY category ORDER BY 2 DESC"),
            "by_status": rows("SELECT status, COUNT(*) FROM badcases GROUP BY status"),
            "by_date": rows("SELECT substr(ts, 1, 10) d, COUNT(*) FROM badcases GROUP BY d ORDER BY d DESC"),
            "by_category_status": rows(
                "SELECT category, status, COUNT(*) FROM badcases GROUP BY category, status"),
        }

    def confirmed_questions(self, categories: tuple) -> list:
        """取指定类别、confirmed 状态的 badcase 关联问题文本（mine-keywords 用）。"""
        marks = ",".join("?" * len(categories))
        return [r[0] for r in self.conn.execute(
            f"SELECT t.question FROM badcases b JOIN turns t ON t.id = b.turn_id "
            f"WHERE b.status = 'confirmed' AND b.category IN ({marks})", categories)]

    def close(self):
        self.conn.close()
