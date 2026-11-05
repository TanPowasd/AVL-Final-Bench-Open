# -*- coding: utf-8 -*-
"""基线：Okapi BM25（k1=1.2, b=0.75），词 = 汉字二元组（单字段落取单字）+ 拉丁字母/数字小写整词；每个 doc 一篇文档，倒排索引。"""
import heapq, json, math, re, sys
from collections import Counter, defaultdict

CJK = re.compile(r"[\u4e00-\u9fff]+")
LAT = re.compile(r"[A-Za-z0-9]+")
K1, B = 1.2, 0.75


def grams(t):
    g = []
    for seg in CJK.findall(t):
        g += [seg[k:k + 2] for k in range(len(seg) - 1)] or [seg]
    g += [w.lower() for w in LAT.findall(t)]
    return g


class Index:
    def __init__(self, docs):
        self.ids = [d["id"] for d in docs]
        self.post = defaultdict(list)          # 词 -> [(文档序号, tf)]
        lens = []
        for i, d in enumerate(docs):
            tf = Counter(grams(d["text"]))
            lens.append(sum(tf.values()))
            for w, c in tf.items():
                self.post[w].append((i, c))
        self.N = len(docs)
        avg = sum(lens) / max(self.N, 1) or 1.0
        self.norm = [K1 * (1 - B + B * L / avg) for L in lens]

    def search(self, q, k=50):
        sc = defaultdict(float)
        for w in set(grams(q)):
            p = self.post.get(w)
            if not p:
                continue
            idf = math.log(1 + (self.N - len(p) + .5) / (len(p) + .5))
            for i, c in p:
                sc[i] += idf * c * (K1 + 1) / (c + self.norm[i])
        top = heapq.nlargest(k, sc.items(), key=lambda x: (x[1], -x[0]))
        return [self.ids[i] for i, _ in top]


def main():
    idx = None
    for line in sys.stdin:
        m = json.loads(line)
        if m["type"] == "memory":
            idx = None
            idx = Index(m["docs"])
            print("{}", flush=True)
        elif m["type"] == "query":
            print(json.dumps({"ranking": idx.search(m["question"]) if idx else []}, ensure_ascii=False), flush=True)
        else:
            break


main()
