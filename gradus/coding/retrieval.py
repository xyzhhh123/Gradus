"""检索的基础件：分词与 BM25 打分。

`tool_search`（找工具）与 `recall`（找被压缩掉的原文）共用同一套，
免得两处各写一份、其中一处永远忘了中文。

形态照 Pi 的 `extensions/tool-search/tool.ts`（BM25 + 拆驼峰/下划线 + 停用词 +
简单词干化），另外加两处它没有的东西：**中文 2-gram** 与**小型双语同义词表**——
Pi 的分词器只认 `[a-z0-9]`，中文查询在那里必然零命中。理由见 `docs/design.md` §5.9。
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

#: 英文停用词：这些词几乎出现在每条描述里，留着只会把分数拉平。
STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is",
        "it", "of", "on", "or", "that", "the", "this", "to", "with", "will", "when",
        "your", "you", "use", "using", "used", "into", "its", "if",
        # schema 描述里到处都有的词
        "tool", "tools", "call", "calls", "type", "string", "object", "boolean",
        "integer", "array", "number", "default", "optional", "required", "value",
        "values", "otherwise", "via",
    }
)

#: 中文 2-gram → 英文规范词。工具与文档里大多是英文，中文查询要能落到那些词上。
SYNONYMS: dict[str, str] = {
    "任务": "task",
    "子任务": "task",
    "列表": "list",
    "列出": "list",
    "清单": "list",
    "完成": "complete",
    "收尾": "complete",
    "认领": "claim",
    "领取": "claim",
    "更新": "update",
    "修改": "update",
    "创建": "create",
    "新建": "create",
    "获取": "get",
    "查看": "get",
    "详情": "get",
    "细节": "get",
    "代理": "agent",
    "子代理": "agent",
    "智能体": "agent",
    "派生": "spawn",
    "孵化": "spawn",
    "启动": "spawn",
    "发送": "send",
    "消息": "message",
    "通知": "message",
    "关闭": "close",
    "终止": "close",
    "中断": "interrupt",
    "打断": "interrupt",
    "等待": "wait",
    "同步": "wait",
    "定时": "schedule",
    "计划": "schedule",
    "日程": "schedule",
    "管理": "manage",
    "后台": "background",
    "检索": "search",
    "搜索": "search",
    "查找": "search",
    "回忆": "recall",
    "读取": "read",
    "读": "read",
    "写入": "write",
    "写": "write",
    "删除": "delete",
    "移除": "delete",
    "文件": "file",
    "目录": "directory",
    "命令": "command",
    "重试": "retry",
    "测试": "test",
    "报错": "error",
    "错误": "error",
    "登录": "login",
    "认证": "auth",
    "令牌": "token",
    "配置": "config",
    "接口": "api",
    "数据库": "database",
    "缓存": "cache",
}

#: 中文 2-gram 里的纯噪声：语气词、量词，不指向任何东西。
NOISE: frozenset[str] = frozenset(
    {"所有", "某个", "一下", "帮我", "这个", "那个", "然后", "现在", "可以", "需要", "我们", "你们"}
)

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z0-9]+")

#: BM25 的常用参数，与 Pi 一致。
K1 = 1.2
B = 0.75


def _stem(term: str) -> str:
    """简单复数词干化：`issues` → `issue`、`searches` → `search`。"""
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 4 and re.search(r"(ches|shes|sses|xes|zes)$", term):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """切成规范词：拆驼峰与下划线、去停用词、复数归并、中文 2-gram、同义词映射。

    中文没有空格，整句会变成一个 token；按 2-gram 切开后，「任务」「完成」这样的
    片段才可能通过 `SYNONYMS` 落到英文词上。
    """
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)

    terms: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", spaced.lower()):
        if _CJK.fullmatch(chunk):
            if len(chunk) == 1:
                terms.append(chunk)
            else:
                terms.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
            continue
        for word in _WORD.findall(chunk):
            if word in STOP_WORDS:
                continue
            terms.append(_stem(word))

    mapped = [SYNONYMS.get(term, term) for term in terms]
    return [term for term in mapped if term not in NOISE]


def surface_terms(text: str) -> list[str]:
    """原文里可能**逐字**出现的片段：中文 2-gram + 小写英文词，不做同义词映射。

    截取命中的片段时要用它，而不是 `tokenize` 的结果——「重试」经同义词映射变成
    `retry`，拿去中文原文里找是找不到的，片段就会退回开头。
    """
    out: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", text.lower()):
        if _CJK.fullmatch(chunk):
            if len(chunk) == 1:
                out.append(chunk)
            else:
                out.extend(chunk[index : index + 2] for index in range(len(chunk) - 1))
            continue
        out.append(chunk)
    return out


def bm25(query_terms: Sequence[str], corpus: Sequence[Sequence[str]]) -> list[float]:
    """Okapi BM25 打分；`corpus` 是每篇文档的分词结果。"""
    if not corpus:
        return []

    k1, b = K1, B
    counts = [len(terms) for terms in corpus]
    average = sum(counts) / len(counts) or 1.0
    document_terms = [set(terms) for terms in corpus]

    scores = [0.0] * len(corpus)
    for term in set(query_terms):
        frequency = sum(1 for terms in document_terms if term in terms)
        if frequency == 0:
            continue
        idf = math.log(1 + (len(corpus) - frequency + 0.5) / (frequency + 0.5))
        for index, terms in enumerate(corpus):
            count = terms.count(term)
            if count == 0:
                continue
            norm = k1 * (1 - b + b * (counts[index] / average))
            scores[index] += idf * (count * (k1 + 1)) / (count + norm)
    return scores
