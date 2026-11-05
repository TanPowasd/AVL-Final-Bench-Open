# -*- coding: utf-8 -*-
"""基线（最朴素）：按「问句与文档共有的不同汉字 + 不同英文小写词」个数排序；同分按文档原顺序。"""
import heapq, json, re, sys
from collections import defaultdict

LAT = re.compile(r"[A-Za-z0-9]+")


def units(t):
    return {ch for ch in t if "\u4e00" <= ch <= "\u9fff"} | {w.lower() for w in LAT.findall(t)}


def main():
    ids, post = [], {}
    for line in sys.stdin:
        m = json.loads(line)
        if m["type"] == "memory":
            ids, post = [d["id"] for d in m["docs"]], defaultdict(list)
            for i, d in enumerate(m["docs"]):
                for u in units(d["text"]):
                    post[u].append(i)
            print("{}", flush=True)
        elif m["type"] == "query":
            sc = defaultdict(int)
            for u in units(m["question"]):
                for i in post.get(u, ()):
                    sc[i] += 1
            top = heapq.nlargest(50, sc.items(), key=lambda x: (x[1], -x[0]))
            print(json.dumps({"ranking": [ids[i] for i, _ in top]}, ensure_ascii=False), flush=True)
        else:
            break


main()
