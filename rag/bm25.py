# 轻量 BM25 索引：纯 Python 自写，环境里没有 rank_bm25/jieba，也不能装依赖
# 中文分词用「字符 2-gram + ASCII 单词」：不依赖词典，对中文子串/错词容忍度好
import math
import re

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_CHARS = "".join(chr(c) for c in range(0x4E00, 0x9FFF + 1))


def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    for word in _ASCII_WORD_RE.findall(text):
        tokens.append(word)
        # 英文长词也拆 2-gram，增强子串匹配
        if len(word) > 2:
            for i in range(len(word) - 1):
                tokens.append(word[i:i + 2])
    chars = [c for c in text if c in _CJK_CHARS]
    for i in range(len(chars) - 1):
        tokens.append(chars[i] + chars[i + 1])
    return tokens


class BM25Index:

    K1 = 1.5
    B = 0.75

    def __init__(self) -> None:
        self.doc_ids: list[str] = []
        self.doc_texts: list[str] = []
        self.doc_map: dict[str, dict] = {}
        self.doc_tokens: list[list[str]] = []
        self.doc_tf: list[dict[str, int]] = []
        self.doc_lens: list[int] = []
        self.postings: dict[str, list[int]] = {}
        self.doc_freq: dict[str, int] = {}
        self.avgdl: float = 0.0

    def rebuild(self, docs: list[dict]) -> None:
        self.doc_ids = []
        self.doc_texts = []
        self.doc_map = {}
        self.doc_tokens = []
        self.doc_tf = []
        self.doc_lens = []
        self.postings = {}
        self.doc_freq = {}

        for doc in docs:
            tokens = tokenize(doc["text"])
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            doc_idx = len(self.doc_ids)
            self.doc_ids.append(doc["id"])
            self.doc_texts.append(doc["text"])
            self.doc_map[doc["id"]] = {"text": doc["text"], "metadata": doc["metadata"]}
            self.doc_tokens.append(tokens)
            self.doc_tf.append(tf)
            self.doc_lens.append(len(tokens))
            for t in set(tokens):
                self.postings.setdefault(t, []).append(doc_idx)
                self.doc_freq[t] = self.doc_freq.get(t, 0) + 1

        self.avgdl = (sum(self.doc_lens) / len(self.doc_lens)) if self.doc_lens else 0.0

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        if not self.doc_ids:
            return []
        q_tokens = tokenize(query)
        scores: dict[int, float] = {}
        n = len(self.doc_ids)
        for token in q_tokens:
            df = self.doc_freq.get(token, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc_idx in self.postings.get(token, []):
                tf = self.doc_tf[doc_idx].get(token, 0)
                dl = self.doc_lens[doc_idx]
                denom = tf + self.K1 * (1 - self.B + self.B * dl / (self.avgdl or 1.0))
                scores[doc_idx] = scores.get(doc_idx, 0.0) + idf * tf * (self.K1 + 1) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [(self.doc_ids[idx], score) for idx, score in ranked[:top_k]]
