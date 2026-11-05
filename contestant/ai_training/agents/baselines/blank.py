"""blank：白板 B。

不买卡、不招人、不研发；用起始 512 张 G1、默认学习率、默认 batch、默认存档间隔、
均匀配比（8 个数据域）训一个按 Chinchilla 比例（D = 20N）的 20B 模型，训完就以默认价格发布。
服务走默认（训完后剩余的卡自动服务旗舰模型）。
"""
import json
import sys


class Bot:
    def __init__(self, init):
        self.started = False
        self.released = False

    def step(self, obs):
        act = {}
        if not self.started:
            self.started = True
            act["run"] = [{"name": "main", "params_b": 20, "tokens_b": 400}]
        for e in obs.get("ev", []):
            if e.get("type") == "done" and e.get("run") == "main" and not self.released:
                self.released = True
                act["release"] = [{"run": "main"}]
        return act


def make_bot(init):
    return Bot(init)


if __name__ == "__main__":
    bot = make_bot(json.loads(sys.stdin.readline()))
    print("{}", flush=True)
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "end":
            print("{}", flush=True)
            break
        try:
            act = bot.step(msg)
        except Exception:
            act = {}
        sys.stdout.write(json.dumps(act, separators=(",", ":")) + "\n")
        sys.stdout.flush()
