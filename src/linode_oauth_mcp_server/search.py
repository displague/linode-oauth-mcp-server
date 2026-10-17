"""Tool search tuned for Linode's generated API tools.

FastMCP's BM25 transform indexes each tool's full description and every
parameter description. For Linode that buries the signal: a create operation
documents dozens of fields, so "list linode instances" ranked database tools
first. This index weights what identifies an operation (its name and title),
keeps only the first description paragraph, and folds plurals so "Linodes"
matches "linode".
"""

import math
import re
import unicodedata
from typing import Dict, List, Sequence

from fastmcp.server.transforms.search import BM25SearchTransform
from fastmcp.tools.base import Tool

_TOKEN = re.compile(r"[^\W_]{2,}")
_IDENTITY_WEIGHT = 3
NAMESPACE_PREFIX = "linode_"


def _stem(token: str) -> str:
    if len(token) <= 3:
        return token
    if token.endswith("sses"):
        return token[:-2]
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def tokenize(text: str) -> List[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return [_stem(token) for token in _TOKEN.findall(normalized)]


def searchable_text(tool: Tool) -> str:
    title = tool.title or (tool.annotations.title if tool.annotations else None) or ""
    # Every tool shares the linode_ namespace prefix, which would make
    # "linode" match everything instead of the Linode instance operations.
    name = tool.name.removeprefix(NAMESPACE_PREFIX)
    identity = f"{name} {title}"
    first_paragraph = (tool.description or "").strip().split("\n\n", 1)[0]
    parameters = " ".join((tool.parameters or {}).get("properties") or {})
    return " ".join([identity] * _IDENTITY_WEIGHT + [first_paragraph, parameters])


class _Index:
    """BM25 Okapi over pre-tokenized documents."""

    def __init__(self, documents: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self._k1, self._b = k1, b
        self._tf: List[Dict[str, int]] = []
        self._df: Dict[str, int] = {}
        self._lengths = [len(doc) for doc in documents]
        self._avg = sum(self._lengths) / len(documents) if documents else 0.0
        for doc in documents:
            tf: Dict[str, int] = {}
            for token in doc:
                tf[token] = tf.get(token, 0) + 1
            for token in tf:
                self._df[token] = self._df.get(token, 0) + 1
            self._tf.append(tf)

    def query(self, tokens: List[str], top_k: int) -> List[int]:
        n = len(self._tf)
        scores = [0.0] * n
        for token in set(tokens):
            df = self._df.get(token)
            if not df:
                continue
            idf = math.log((n - df + 0.5) / (df + 0.5) + 1.0)
            for i, tf in enumerate(self._tf):
                count = tf.get(token, 0)
                if count:
                    norm = 1 - self._b + self._b * self._lengths[i] / self._avg
                    scores[i] += (
                        idf * count * (self._k1 + 1) / (count + self._k1 * norm)
                    )
        ranked = sorted(range(n), key=lambda i: scores[i], reverse=True)
        return [i for i in ranked[:top_k] if scores[i] > 0]


class LinodeSearchTransform(BM25SearchTransform):
    """BM25 tool search over each tool's name, title and summary."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._linode_index: _Index | None = None
        self._linode_names: tuple = ()

    async def _search(self, tools: Sequence[Tool], query: str) -> Sequence[Tool]:
        names = tuple(t.name for t in tools)
        if self._linode_index is None or names != self._linode_names:
            self._linode_index = _Index([tokenize(searchable_text(t)) for t in tools])
            self._linode_names = names
        indices = self._linode_index.query(tokenize(query), self._max_results)
        return [tools[i] for i in indices]
