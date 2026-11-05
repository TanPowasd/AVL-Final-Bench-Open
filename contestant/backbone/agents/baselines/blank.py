"""blank：白板 B。保持起始 2 个 PoP，全部走默认现货最短路，默认价格，什么都不租不建。"""
import json
import sys


class Bot:
    def __init__(self, init):
        pass

    def step(self, obs):
        return {}


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
