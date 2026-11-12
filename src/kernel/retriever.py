# -*- coding: utf-8 -*-
"""检索：按 Markdown 章节 / FAQ 条目切分 chunk，jieba 分词 + BM25 排序。"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import jieba
from rank_bm25 import BM25Okapi

# 语料元数据页脚（来源/生效日期/适用人群）不参与检索
_META_LINE = re.compile(r"^(来源|生效日期|适用人群)[:：]")


# 匹配信号判定时的通用词（命中这些词不代表真的相关）
_GENERIC_TOKENS = {
    "你", "我", "我们", "你们", "他", "她", "它", "能", "不能", "可以", "是不是",
    "怎么", "怎样", "如何", "什么", "哪", "哪里", "哪家", "多少", "有没有",
    "一定", "保证", "承诺", "吧", "呢", "的", "了", "吗", "啊", "请", "帮", "帮忙",
    "看看", "一下", "现在", "今天", "明天", "觉得", "事情", "东西", "问题", "晚上",
    "一个", "这个", "那个",
}


@dataclass
class Chunk:
    doc: str       # 文档名，如 住客FAQ-30条
    section: str   # 章节标题，如 Q03. 酒店有早餐吗？多少钱？
    text: str      # 章节正文
    tokens: list = field(default_factory=list, repr=False)

    @property
    def source(self) -> str:
        return f"{self.doc} · {self.section}"


def _tokenize(text: str) -> list:
    # 搜索引擎模式切分更细（如“住宿标准”-> 住宿/标准/住宿标准），提升召回
    return [t for t in jieba.lcut_for_search(text) if t.strip()]


# 同义词扩展：口语表达对齐语料用词，提升召回
_SYNONYMS = {
    "抽烟": ["吸烟"], "香烟": ["吸烟"],
    "退钱": ["退款"], "返钱": ["退款"],
    "赔钱": ["赔偿"], "价钱": ["价格"],
    "无线网": ["Wi-Fi"], "上网": ["Wi-Fi"],
    "小孩": ["儿童"], "小朋友": ["儿童"],
}


def _expand(tokens: list) -> list:
    expanded = list(tokens)
    for t in tokens:
        expanded.extend(_SYNONYMS.get(t, []))
    return expanded


def _split_sections(doc_name: str, content: str) -> list:
    """按 '## ' 二级标题切分；FAQ 每条 Q 为一个 chunk。"""
    chunks = []
    current_title, buf = "", []

    def flush():
        body = "\n".join(
            ln for ln in buf
            if ln.strip() and ln.strip() != "---" and not _META_LINE.match(ln.strip())
        ).strip()
        if current_title and body:
            chunks.append(Chunk(doc=doc_name, section=current_title.strip("# ").strip(), text=body))

    for line in content.splitlines():
        if line.startswith("## "):
            flush()
            current_title, buf = line, []
        else:
            buf.append(line)
    flush()
    return chunks


class Retriever:
    """BM25 关键词检索，语料量小无需向量库。"""

    def __init__(self, corpus_dir):
        corpus_dir = Path(corpus_dir)
        self.chunks = []
        for md in sorted(corpus_dir.rglob("*.md")):
            if md.name == "README.md" or "04-评测集" in md.parts:
                continue
            self.chunks.extend(_split_sections(md.stem, md.read_text(encoding="utf-8")))
        for c in self.chunks:
            # 标题权重 x3：FAQ 的问题标题是最强相关信号
            c.tokens = _tokenize(c.section) * 3 + _tokenize(c.text)
        self.bm25 = BM25Okapi([c.tokens for c in self.chunks]) if self.chunks else None

    def search(self, query: str, top_k: int = 3) -> list:
        """返回 [(Chunk, score)]，按相关度降序，过滤零分结果。

        打分前先剔除通用词与单字：长句/噪声输入中，通用词（我/怎么/可以…）
        会在短文档上累计出虚高分数，淹没真正的高 IDF 命中（如「早餐」）。
        """
        if not self.bm25:
            return []
        q_tokens = _expand(_tokenize(query))
        scoring = [t for t in q_tokens if len(t) >= 2 and t not in _GENERIC_TOKENS] or q_tokens
        scores = self.bm25.get_scores(scoring)
        ranked = sorted(zip(self.chunks, scores), key=lambda x: x[1], reverse=True)
        return [(c, float(s)) for c, s in ranked[:top_k] if s > 0]

    def is_relevant(self, query: str, chunk: Chunk) -> bool:
        """粗粒度相关性判定：查询与 chunk 是否存在非通用词重叠。

        供 MockLLM / 兜底逻辑使用；真实模型自行判断相关性，不依赖此函数。
        """
        matched = {
            t for t in _expand(_tokenize(query))
            if len(t) >= 2 and t not in _GENERIC_TOKENS
        } & set(chunk.tokens)
        return bool(matched)
