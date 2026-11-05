# -*- coding: utf-8 -*-
"""mem_recall v0.2 bot 模板：每收到一个记忆库（bank）就建索引，然后对该库的每道题返回按相关度排好的文档 id。
只用标准库、不联网、不调模型。这个模板用最朴素的打分（问句里的字在文档里出现几个），只为演示协议，大语料上会很慢。"""
import json, sys


def main():
    docs = []
    for line in sys.stdin:
        msg = json.loads(line)
        if msg["type"] == "memory":
            # msg = {"type":"memory","set":"locomo","bank_id":"conv-26",
            #        "docs":[{"id":"D1:1","text":"...","date":"...（可选）","speaker":"...（可选）"}, ...]}
            # 新 bank 到来时，旧 bank 的索引可以扔掉（之后的题只问这个 bank）
            docs = msg["docs"]
            print("{}", flush=True)
        elif msg["type"] == "query":
            # msg = {"type":"query","qid":"...","bank_id":"...","question":"..."}
            # 回 {"ranking": [doc id, ...]}：最多 50 个，越靠前越相关
            q = set(msg["question"])
            scored = sorted(docs, key=lambda d: -sum(ch in d["text"] for ch in q))
            print(json.dumps({"ranking": [d["id"] for d in scored[:50]]}), flush=True)
        else:   # {"type": "end"}
            break


main()
