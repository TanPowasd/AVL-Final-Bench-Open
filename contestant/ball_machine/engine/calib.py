"""CPU 速度标定：让不同电脑测出的时间可以直接比较（"标准毫秒"）。

原理
----
1. 用与选手代码相同的编译器和参数（g++ -O2 -std=gnu++20）编译一段固定的 C++ 基准程序
   （随机访存 + 整数散列 + 排序，单线程，与常见算法题负载相近）。
2. 在本机运行 5 次取 CPU 时间最小值 t_local（后台负载只会让程序变慢，取最小值最抗干扰）。
3. 速度系数 factor = t_local / BASELINE_MS。BASELINE_MS 是"标准机"跑这段程序的时间。
   factor > 1 说明本机比标准机慢。
4. 评测时：实际时限 = 题面时限 × factor；报告用时 = 实测 CPU 时间 / factor。
   于是题面上的 1000 ms 在任何电脑上都表示"标准机上的 1000 ms"。

所有算法题 engine/ 下的 calib.py 内容完全相同；改 BASELINE_MS 请用
`python organizer/calibrate.py --set-baseline`，它会同步改所有副本。

环境变量
--------
- AVL_SPEED_FACTOR=1.0   直接指定系数（跳过标定），例如正式评测想固定数值时。
- AVL_NO_CALIB=1         关闭标定（系数=1，即按本机原始时间判）。
- AVL_CALIB_REFRESH=1    忽略缓存，强制重新标定。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time

# 标准机跑基准程序的 CPU 时间（毫秒）。由 organizer/calibrate.py --set-baseline 写入。
BASELINE_MS = 882.8
BASELINE_NOTE = "AMD Ryzen 9 9950X3D / g++ 15.2.0"

CALIB_VERSION = 2
RUNS = 5
FACTOR_MIN, FACTOR_MAX = 0.2, 5.0
CACHE_TTL_SEC = 3600

BENCH_SRC = r"""
#include <cstdio>
#include <cstdint>
#include <vector>
#include <algorithm>
static inline uint64_t mix(uint64_t x){x^=x>>33;x*=0xff51afd7ed558ccdULL;x^=x>>33;x*=0xc4ceb9fe1a85ec53ULL;x^=x>>33;return x;}
int main(){
    // 1) 随机访存：16M 个 uint32（64 MB），链式跳转，测缓存/内存延迟
    const uint32_t N = 1u<<24;
    std::vector<uint32_t> a(N);
    uint64_t s = 88172645463325252ULL;
    for (uint32_t i=0;i<N;i++){ s^=s<<13; s^=s>>7; s^=s<<17; a[i]=(uint32_t)(s&(N-1)); }
    uint32_t p=0; uint64_t acc=0;
    for (int it=0; it<6000000; it++){ p=a[p]^(uint32_t)it; p&=N-1; acc+=p; }
    // 2) 整数运算：散列循环，测 ALU
    uint64_t h=1;
    for (uint64_t i=0;i<60000000ULL;i++){ h=mix(h+i); }
    // 3) 排序：4M 个 64 位整数，测分支与顺序访存
    std::vector<uint64_t> b(1u<<22);
    for (size_t i=0;i<b.size();i++) b[i]=mix(i*2654435761ULL+acc);
    std::sort(b.begin(), b.end());
    // 4) 小规模 DP：O(n^2) 的 LCS，测紧循环
    const int L=3000; std::vector<int> x(L),y(L),dp(L+1),nd(L+1);
    for(int i=0;i<L;i++){x[i]=mix(i)%4;y[i]=mix(i+77777)%4;}
    for(int i=1;i<=L;i++){ nd[0]=0; for(int j=1;j<=L;j++){ nd[j]= x[i-1]==y[j-1]? dp[j-1]+1 : std::max(dp[j],nd[j-1]); } std::swap(dp,nd); }
    std::printf("%llu %llu %llu %d\n",(unsigned long long)acc,(unsigned long long)h,(unsigned long long)b[b.size()/2],dp[L]);
    return 0;
}
"""

_factor_cache: dict[str, float] | None = None


def _cache_path(key: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"avl_calib_{key}.json")


def _machine_key(cxx: str) -> str:
    try:
        ver = subprocess.run([cxx, "--version"], capture_output=True, text=True, timeout=30).stdout.splitlines()[:1]
    except Exception:
        ver = []
    raw = json.dumps([CALIB_VERSION, BASELINE_MS, socket.gethostname(), platform.machine(), platform.processor(), cxx, ver])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def measure_bench_ms(verbose: bool = False) -> dict:
    """编译并运行基准，返回 {'min_ms'(5 次最小值),'runs_ms','cxx'}。原始 CPU 毫秒，不做换算。"""
    import common  # 同目录

    with tempfile.TemporaryDirectory(prefix="avl_calib_") as d:
        src = os.path.join(d, "bench.cpp")
        with open(src, "w", encoding="utf-8") as f:
            f.write(BENCH_SRC)
        comp = common.compile_cpp([src], os.path.join(d, "bench"), stack_mb=64)
        if not comp.ok:
            raise RuntimeError("标定程序编译失败：" + comp.log[:500])
        runs = []
        common.run_sandboxed(comp.exe_path, b"", 60000, 4096, raw=True)  # 预热
        for i in range(RUNS):
            rr = common.run_sandboxed(comp.exe_path, b"", 60000, 4096, raw=True)
            if rr.returncode != 0:
                raise RuntimeError("标定程序运行失败")
            runs.append(rr.cpu_ms)
            if verbose:
                print(f"  标定第 {i + 1}/{RUNS} 次：{rr.cpu_ms:.1f} ms", file=sys.stderr)
        return {"min_ms": min(runs), "runs_ms": runs, "cxx": common.find_cxx()}


def speed_factor(verbose: bool = False) -> float:
    """本机相对标准机的速度系数（>1 表示更慢）。进程内与 1 小时文件缓存。"""
    global _factor_cache
    env = os.environ.get("AVL_SPEED_FACTOR")
    if env:
        return max(FACTOR_MIN, min(FACTOR_MAX, float(env)))
    if os.environ.get("AVL_NO_CALIB") == "1":
        return 1.0
    if _factor_cache is not None:
        return _factor_cache["factor"]

    import common

    key = _machine_key(common.find_cxx())
    path = _cache_path(key)
    if os.environ.get("AVL_CALIB_REFRESH") != "1":
        try:
            with open(path, encoding="utf-8") as f:
                c = json.load(f)
            if time.time() - c["ts"] < CACHE_TTL_SEC:
                _factor_cache = c
                return c["factor"]
        except Exception:
            pass

    if verbose:
        print("正在标定本机 CPU 速度（约 5–15 秒，1 小时内复用）...", file=sys.stderr)
    m = measure_bench_ms(verbose=verbose)
    factor = max(FACTOR_MIN, min(FACTOR_MAX, m["min_ms"] / BASELINE_MS))
    c = {"factor": factor, "min_ms": m["min_ms"], "runs_ms": m["runs_ms"], "baseline_ms": BASELINE_MS, "ts": time.time()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(c, f)
    except Exception:
        pass
    _factor_cache = c
    if verbose:
        print(f"本机基准 {m['min_ms']:.0f} ms / 标准机 {BASELINE_MS:.0f} ms → 速度系数 {factor:.3f}"
              f"（时限 ×{factor:.3f}，报告用时 ÷{factor:.3f}）", file=sys.stderr)
    return factor


def describe() -> dict:
    f = speed_factor()
    info = {"speed_factor": round(f, 4), "baseline_ms": BASELINE_MS, "baseline_note": BASELINE_NOTE}
    if _factor_cache:
        info["local_bench_ms"] = round(_factor_cache.get("min_ms", 0), 1)
    if os.environ.get("AVL_SPEED_FACTOR"):
        info["source"] = "env AVL_SPEED_FACTOR"
    elif os.environ.get("AVL_NO_CALIB") == "1":
        info["source"] = "disabled"
    else:
        info["source"] = "calibrated"
    return info
