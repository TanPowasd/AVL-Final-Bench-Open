"""前沿实验室协议起步模板（详细规则与字段见 README.md）。"""
import json
import sys


class Bot:
    def __init__(self, init):
        self.rules = init["rules"]
        self.seat = init["seat"]

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
