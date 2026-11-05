# -*- coding: utf-8 -*-
"""mem_recall v0.2 评测器：按 bank 把记忆喂给 bot，逐题要排序，记忆集算 r@10、检索集算 nDCG@10，再做宏平均。

用法：
  python engine/runner.py --bot agents/baselines/bm25.py --data data/dev
  python engine/runner.py --bots agents/baselines --data data/dev --sets cpzh,hive --out results/dev.json
  python scripts/spar.py agents/submissions/my_agent.py --sets cpzh,hive,scifact
--data：一个 .json / .json.gz 文件，或一个目录（读其中第一层的全部 .json / .json.gz；同名集的多个 partK 文件自动合并）。
--sets：只跑这些集（逗号分隔）。--jobs N：N 个集并行（每集一个 bot 进程）。--per-q：结果里附每题分数。

协议（每行一个 JSON，见 README.md）：
  对每个 bank：先发 {"type":"memory","set","bank_id","docs":[{id,text,date?,speaker?,...}]}，bot 回一行任意 JSON（如 {}）；
  再发该 bank 的每道题 {"type":"query","qid","bank_id","question"}，bot 回 {"ranking":[doc id, ...]}（最多 50 个）；
  全部 bank 结束后发 {"type":"end"}。
时限：
  memory 一步 ≤ max(10s, 该 bank 每万字 1s)；每道 query ≤ 10s；
  整集总时限 = 120s + 每万字（全部 bank 字数之和）1s + 每题 1s。
  某一步超时 / 崩溃 / 回了非 JSON：杀掉 bot，本 bank 余下题记空排序；下一个 bank 重启 bot（每集最多重启 3 次，之后余下全记空）。
  总时限用完：余下题全记空。回复不是 {"ranking": list} 记一次犯规（该题空排序）。不存在 / 重复 / 被排除的 id 丢掉，只取前 50。
"""
import argparse, collections, gzip, json, math, os, queue, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

STEP_LIMIT = 10.0
TOPK = 50
MAX_RESTARTS = 3


def mem_limit(chars):
    return max(10.0, chars / 10000.0)


def set_limit(chars, nq):
    return 120.0 + chars / 10000.0 + nq * 1.0


class Bot:
    def __init__(self, path):
        self.p = subprocess.Popen([sys.executable, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.dead = False

    def _read(self):
        try:
            for line in self.p.stdout:
                self.q.put(line)
        except Exception:
            pass
        self.q.put(None)

    def ask(self, obj, limit):
        """→ (回复对象或 None, 用时)。None = 超时 / 崩溃 / 非 JSON，bot 随即作废。"""
        if self.dead:
            return None, 0.0
        t0 = time.time()
        try:
            self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n"); self.p.stdin.flush()
        except Exception:
            self.kill(); return None, time.time() - t0
        try:
            line = self.q.get(timeout=max(limit - (time.time() - t0), 0.001))
        except queue.Empty:
            line = None
        dt = time.time() - t0
        if line is None:
            self.kill(); return None, dt
        try:
            return json.loads(line), dt
        except Exception:
            self.kill(); return None, dt

    def kill(self):
        self.dead = True
        try:
            self.p.kill()
        except Exception:
            pass

    def close(self):
        if self.dead:
            return
        try:
            self.p.stdin.write('{"type": "end"}\n'); self.p.stdin.flush()
            self.p.wait(timeout=5)
        except Exception:
            self.kill()


# ------------------------------------------------------------------ 指标
def recall_groups(rank, groups, k):
    top = set(rank[:k])
    cov = [bool(top & set(g)) for g in groups]
    return sum(cov) / len(cov), float(all(cov))


def ndcg10(rank, rel):
    """与 UMB / xbench bench_ir.metrics 相同：gain = 2^rel - 1，log2(i+2) 折扣，理想排序取金标分级降序前 10。"""
    dcg = sum((2 ** rel.get(d, 0) - 1) / math.log2(i + 2) for i, d in enumerate(rank[:10]))
    ideal = sorted(rel.values(), reverse=True)[:10]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal)) or 1.0
    return dcg / idcg


def score_q(q, rank, metric):
    if metric == "ndcg@10":
        rel = q["qrels"]
        return {"ndcg@10": ndcg10(rank, rel), "r@10": len(set(rank[:10]) & rel.keys()) / len(rel)}
    r5, a5 = recall_groups(rank, q["evidence_groups"], 5)
    r10, a10 = recall_groups(rank, q["evidence_groups"], 10)
    return {"r@10": r10, "r@5": r5, "all@10": a10}


def scored(q):
    return bool(q.get("evidence_groups") or q.get("qrels"))


# ------------------------------------------------------------------ 数据
def read_file(p):
    if p.endswith(".gz"):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def list_files(path):
    if os.path.isfile(path):
        return [path]
    return sorted(os.path.join(path, f) for f in os.listdir(path) if f.endswith(".json") or f.endswith(".json.gz"))


def load_sets(path, only=None):
    """→ {集名: 合并后的集}（partK 合并）。只读文件头判断集名，--sets 过滤后才读入其余。"""
    by = collections.defaultdict(list)
    for f in list_files(path):
        base = os.path.basename(f).split(".")[0]
        if only and base not in only:
            continue
        by[base].append(f)
    out = {}
    for name, fs in sorted(by.items()):
        merged = None
        for f in fs:
            d = read_file(f)
            if "banks" not in d:                      # 不是 v0.2 格式（如 v0.1 遗留文件）就跳过
                print(f"  跳过非 v0.2 文件 {f}", file=sys.stderr); continue
            if merged is None:
                merged = d
            else:
                merged["banks"] += d["banks"]; merged["questions"] += d["questions"]
        if merged:
            merged.pop("part", None)
            out[merged["name"]] = merged
    return out


# ------------------------------------------------------------------ 跑一集
def run_set(bot_path, ds, per_q=False):
    metric = ds["metric"]
    qby = collections.defaultdict(list)
    for q in ds["questions"]:
        qby[q["bank_id"]].append(q)
    chars_of = {b["bank_id"]: sum(len(d["text"]) for d in b["docs"]) for b in ds["banks"]}
    budget = set_limit(sum(chars_of.values()), len(ds["questions"]))
    used, restarts, fouls, timeouts = 0.0, 0, 0, 0
    pred = {}
    bot = None
    t_index = 0.0
    for b in ds["banks"]:
        qs = qby.get(b["bank_id"], [])
        if not qs:
            continue
        if bot is None or bot.dead:
            if bot is not None:
                if restarts >= MAX_RESTARTS:
                    break
                restarts += 1
            bot = Bot(bot_path)
        if used >= budget:
            break
        valid = {d["id"] for d in b["docs"]}
        r, dt = bot.ask({"type": "memory", "set": ds["name"], "bank_id": b["bank_id"], "docs": b["docs"]},
                        min(mem_limit(chars_of[b["bank_id"]]), budget - used))
        used += dt; t_index += dt
        if r is None:
            timeouts += 1; continue
        for q in qs:
            if used >= budget:
                break
            r, dt = bot.ask({"type": "query", "qid": q["qid"], "bank_id": b["bank_id"], "question": q["question"]},
                            min(STEP_LIMIT, budget - used))
            used += dt
            if r is None:
                timeouts += 1; break
            rk = r.get("ranking") if isinstance(r, dict) else None
            if not isinstance(rk, list):
                fouls += 1; rk = []
            ex = set(q.get("exclude") or [])
            seen, clean = set(), []
            for x in rk:
                if isinstance(x, str) and x in valid and x not in seen and x not in ex:
                    seen.add(x); clean.append(x)
                    if len(clean) >= TOPK:
                        break
            pred[q["qid"]] = clean
    if bot is not None:
        bot.close()
    rows = collections.defaultdict(lambda: collections.defaultdict(list))
    perq = {}
    for q in ds["questions"]:
        if not scored(q):
            continue
        s = score_q(q, pred.get(q["qid"], []), metric)
        perq[q["qid"]] = round(s[metric], 4)
        for c in {q.get("category") or "-", "ALL"}:
            for m, v in s.items():
                rows[c][m].append(v)
    res = {c: {m: round(sum(v) / len(v), 4) for m, v in d.items()} | {"n": len(d[metric])} for c, d in rows.items()}
    res["_meta"] = {"metric": metric, "group": ds["group"], "split": ds.get("split"), "official": ds.get("official", False),
                    "practice_only": ds.get("practice_only", False), "seconds": round(used, 2), "seconds_memory": round(t_index, 2),
                    "time_budget": round(budget, 1), "fouls": fouls, "timeouts_or_crashes": timeouts, "restarts": restarts,
                    "answered": len(pred), "n_questions": len(ds["questions"])}
    if per_q:
        res["_per_q"] = perq
    return res


def macro(res, sets, pred):
    ks = [n for n in sets if pred(sets[n])]
    if not ks:
        return None
    main = sum(res[n]["ALL"][sets[n]["metric"]] for n in ks) / len(ks)
    grp = {}
    for g in ("mem", "en", "zh"):
        kg = [n for n in ks if sets[n]["group"] == g]
        if kg:
            grp[g] = round(sum(res[n]["ALL"][sets[n]["metric"]] for n in kg) / len(kg), 4)
    return {"score": round(main, 4), "n_sets": len(ks), "sets": ks, "groups": grp}


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bot"); g.add_argument("--bots")
    ap.add_argument("--data", required=True)
    ap.add_argument("--sets", default="")
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--per-q", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    bots = [a.bot] if a.bot else sorted(os.path.join(a.bots, f) for f in os.listdir(a.bots) if f.endswith(".py") and not f.startswith("_"))
    only = set(a.sets.split(",")) if a.sets else None
    sets = load_sets(a.data, only)
    if not sets:
        sys.exit("没有可跑的集")
    order = sorted(sets, key=lambda n: ({"mem": 0, "en": 1, "zh": 2}[sets[n]["group"]], sets[n].get("practice_only", False), n))
    out = {}
    for b in bots:
        name = os.path.splitext(os.path.basename(b))[0]
        t0 = time.time()
        with ThreadPoolExecutor(max(1, a.jobs)) as ex:
            futs = {n: ex.submit(run_set, b, sets[n], a.per_q) for n in order}
            res = {n: f.result() for n, f in futs.items()}
        is_test = lambda s: s.get("split") == "test" and not s.get("practice_only")
        is_dev = lambda s: s.get("split") == "dev" and not s.get("practice_only")
        res["_macro_official"] = macro(res, sets, is_test)      # 正式分：test 各集主指标等权宏平均
        res["_macro_dev"] = macro(res, sets, is_dev)            # dev 镜像（不含只练习的集）
        res["_wall_seconds"] = round(time.time() - t0, 1)
        out[name] = res
        print(f"\n== {name}   ({res['_wall_seconds']}s)")
        print(f"{'集':18s} {'组':3s} {'指标':8s} {'题数':>5s} {'分数':>7s}  备注")
        for n in order:
            r, m = res[n], res[n]["_meta"]
            flag = "练习" if m["practice_only"] else ""
            bad = f" 犯规{m['fouls']}" if m["fouls"] else ""
            bad += f" 超时/崩溃{m['timeouts_or_crashes']} 未答{m['n_questions'] - m['answered']}" if m["timeouts_or_crashes"] or m["answered"] < m["n_questions"] else ""
            print(f"{n:18s} {m['group']:3s} {m['metric']:8s} {r['ALL']['n']:5d} {r['ALL'][m['metric']]:7.4f}  {flag}{bad} {m['seconds']:.0f}s")
        for k in ("_macro_official", "_macro_dev"):
            if res[k]:
                print(f"{k[1:]}: {res[k]['score']:.4f}（{res[k]['n_sets']} 集）  " + "  ".join(f"{g} {v:.4f}" for g, v in res[k]["groups"].items()))
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
