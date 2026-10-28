"""中英文混合分词。

seekdb 的 FULLTEXT 空间分词器按空白切词，纯中文文本会被切成一个长词，导致召回很差。
这里在入库前把中文拆成"单字 + 相邻二元组"，英文按单词切分并小写化，
从而可以用普通 FULLTEXT 索引 + ``MATCH ... AGAINST`` 拿到可用的中文召回。
"""

from __future__ import annotations

import re
from typing import Iterable

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: 停用词：出现频率高但对检索无区分度。
STOPWORDS = frozenset(
    """
    the a an of to in for on and or is are be with by at from as that this it its
    must shall should not no any all
    """.split()
)


def _split_cjk(run: str) -> list[str]:
    """把一段连续中文切成单字与二元组。"""
    tokens: list[str] = list(run)
    tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def tokenize(text: str) -> list[str]:
    """把文本切成检索用 token 列表（保持出现顺序，可重复）。"""
    if not text:
        return []
    tokens: list[str] = []
    for run in _CJK.findall(text):
        tokens.extend(_split_cjk(run))
    for word in _WORD.findall(text):
        tokens.append(word.lower())
    for number in _NUMBER.findall(text):
        tokens.append(number)
    return tokens


def index_text(text: str) -> str:
    """生成用于 FULLTEXT 索引的 token 串（空格分隔）。"""
    return " ".join(tokenize(text))


def query_tokens(text: str, *, drop_stopwords: bool = True) -> list[str]:
    """生成查询用 token 列表（去重、去停用词）。"""
    seen: set[str] = set()
    result: list[str] = []
    for token in tokenize(text):
        if drop_stopwords and token.lower() in STOPWORDS:
            continue
        if len(token) < 1:
            continue
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result


def overlap_score(query: Iterable[str], document: Iterable[str]) -> float:
    """计算查询 token 在文档中的覆盖率（0~1）。"""
    query_set = {t.lower() for t in query}
    if not query_set:
        return 0.0
    doc_set = {t.lower() for t in document}
    return len(query_set & doc_set) / len(query_set)
