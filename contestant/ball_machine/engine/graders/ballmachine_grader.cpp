#include "ballmachine.h"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fcntl.h>
#include <vector>

#ifdef _WIN32
#include <io.h>
#define JUDGE_DUP _dup
#define JUDGE_DUP2 _dup2
#define JUDGE_FILENO _fileno
#define JUDGE_CLOSE _close
#define JUDGE_OPEN _open
#define JUDGE_NULL_DEV "NUL"
#define JUDGE_O_FLAGS (_O_WRONLY)
#define JUDGE_R_FLAGS (_O_RDONLY)
#else
#include <unistd.h>
#define JUDGE_DUP dup
#define JUDGE_DUP2 dup2
#define JUDGE_FILENO fileno
#define JUDGE_CLOSE close
#define JUDGE_OPEN open
#define JUDGE_NULL_DEV "/dev/null"
#define JUDGE_O_FLAGS (O_WRONLY)
#define JUDGE_R_FLAGS (O_RDONLY)
#endif

namespace {

constexpr int MAX_BALL = 1000;
// insert 上限：原题 N<=1000 时为 5e5；超规模组按 500*N 缩放（max(500000, 500*N)）。
constexpr long long MAX_INSERT_BASE = 500000;
constexpr long long MAX_INSERT_PER_NODE = 500;
constexpr int MAX_RES = 1000;

struct JudgeState {
  uint64_t secret_hi = 0;
  uint64_t secret_lo = 0;
  int tie_mode = 0;
  uint64_t seed = 0;
  int saved_out_fd = -1;
  std::chrono::steady_clock::time_point t0;

  int N = 0;
  int M = 0;
  int K = 0;
  int B = 0;
  int C = 0;
  int insert_count = 0;
  long long max_insert = MAX_INSERT_BASE;

  std::vector<int> P;
  std::vector<int> ball;
  std::vector<std::vector<int>> children;
  std::vector<uint64_t> static_prio;
};

JudgeState *st = nullptr;

uint64_t splitmix64(uint64_t x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

[[noreturn]] void finish_fail(const char *reason) {
  auto t1 = std::chrono::steady_clock::now();
  long long ns =
      std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - st->t0).count();
  fflush(stdout);
  if (st->saved_out_fd != -1) {
    JUDGE_DUP2(st->saved_out_fd, JUDGE_FILENO(stdout));
    JUDGE_CLOSE(st->saved_out_fd);
    st->saved_out_fd = -1;
  }
  std::printf("%llu %llu\n", (unsigned long long)st->secret_hi,
              (unsigned long long)st->secret_lo);
  std::printf("FAIL %lld %s\n", ns, reason);
  std::fflush(stdout);
  std::_Exit(0);
}

void dfs_collect(int u, const std::vector<uint64_t> &prio,
                 std::vector<int> &out_s) {
  // Explicit stack: deep official trees must not overflow the grader's call stack.
  std::vector<int> pending{u};
  std::vector<int> occ;
  while (!pending.empty()) {
    int cur = pending.back(); pending.pop_back();
    out_s.push_back(st->ball[cur]);
    occ.clear();
    for (int v : st->children[cur]) if (st->ball[v] != -1) occ.push_back(v);
    std::sort(occ.begin(), occ.end(), [&](int x, int y) {
      if (st->ball[x] != st->ball[y]) return st->ball[x] < st->ball[y];
      if (prio[x] != prio[y]) return prio[x] < prio[y];
      return x < y;
    });
    pending.insert(pending.end(), occ.rbegin(), occ.rend());
  }
}

} // namespace

bool insert(int U, int X) {
  if (!st) {
    std::_Exit(1);
  }
  ++st->insert_count;
  if (st->insert_count > st->max_insert) {
    finish_fail("Too many calls");
  }
  if (U < 0 || U >= st->M || X < 0 || X > MAX_BALL) {
    finish_fail("Invalid argument");
  }
  st->B = std::max(st->B, X);
  st->C = st->K + st->B;
  if (st->C > MAX_RES) {
    finish_fail("Too many resources used");
  }
  if (st->ball[U] != -1) {
    return false;
  }
  int cur = U;
  while (st->P[cur] != cur && st->ball[st->P[cur]] == -1) {
    cur = st->P[cur];
  }
  st->ball[cur] = X;
  return true;
}

std::vector<int> collect() {
  if (!st) {
    std::_Exit(1);
  }
  ++st->K;
  st->C = st->K + st->B;
  if (st->C > MAX_RES) {
    finish_fail("Too many resources used");
  }
  std::vector<int> S;
  if (st->ball[st->N - 1] != -1) {
    std::vector<uint64_t> prio(st->N);
    if (st->tie_mode == 0) {
      for (int i = 0; i < st->N; ++i)
        prio[i] = (uint64_t)i;
    } else if (st->tie_mode == 1) {
      for (int i = 0; i < st->N; ++i)
        prio[i] = (uint64_t)(st->N - 1 - i);
    } else if (st->tie_mode == 2) {
      prio = st->static_prio;
    } else {
      uint64_t h = splitmix64(st->seed ^ 0x243f6a8885a308d3ULL);
      for (int i = 0; i < st->N; ++i) {
        uint64_t v = (uint64_t)(st->ball[i] + 2);
        h = splitmix64(h ^ ((uint64_t)i * 0x100000001b3ULL + v));
      }
      for (int i = 0; i < st->N; ++i) {
        prio[i] = splitmix64(h ^ (uint64_t)i);
      }
    }
    S.reserve(st->N);
    dfs_collect(st->N - 1, prio, S);
  }
  std::fill(st->ball.begin(), st->ball.end(), -1);
  return S;
}

int main() {
  st = new JudgeState();
  std::printf("__JUDGE_READY__\n");
  std::fflush(stdout);

  unsigned long long s_hi = 0, s_lo = 0, sd = 0;
  if (std::scanf("%llu %llu %d %llu", &s_hi, &s_lo, &st->tie_mode, &sd) != 4) {
    return 1;
  }
  st->secret_hi = s_hi;
  st->secret_lo = s_lo;
  st->seed = sd;

  if (std::scanf("%d %d", &st->N, &st->M) != 2) {
    return 1;
  }
  st->max_insert = std::max(MAX_INSERT_BASE, MAX_INSERT_PER_NODE * (long long)st->N);
  st->P.resize(st->N);
  st->ball.assign(st->N, -1);
  st->children.resize(st->N);
  st->P[st->N - 1] = st->N - 1;
  for (int i = 0; i < st->N - 1; ++i) {
    if (std::scanf("%d", &st->P[i]) != 1) {
      return 1;
    }
    st->children[st->P[i]].push_back(i);
  }

  st->static_prio.resize(st->N);
  for (int i = 0; i < st->N; ++i) {
    st->static_prio[i] = splitmix64(st->seed ^ (uint64_t)i);
  }

  // Redirect stdin and stdout to NUL / /dev/null during user execution
  int null_in = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_R_FLAGS);
  if (null_in != -1) {
    JUDGE_DUP2(null_in, JUDGE_FILENO(stdin));
    JUDGE_CLOSE(null_in);
  }
  std::fflush(stdout);
  st->saved_out_fd = JUDGE_DUP(JUDGE_FILENO(stdout));
  int null_out = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_O_FLAGS);
  if (null_out != -1) {
    JUDGE_DUP2(null_out, JUDGE_FILENO(stdout));
    JUDGE_CLOSE(null_out);
  }

  st->t0 = std::chrono::steady_clock::now();
  std::vector<int> R = find_structure(st->M);
  auto t1 = std::chrono::steady_clock::now();
  long long ns =
      std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - st->t0).count();

  std::fflush(stdout);
  if (st->saved_out_fd != -1) {
    JUDGE_DUP2(st->saved_out_fd, JUDGE_FILENO(stdout));
    JUDGE_CLOSE(st->saved_out_fd);
    st->saved_out_fd = -1;
  }

  if ((int)R.size() != st->N - 1) {
    std::printf("%llu %llu\n", (unsigned long long)st->secret_hi,
                (unsigned long long)st->secret_lo);
    std::printf("FAIL %lld Wrong array length Q=%zu (expected %d)\n", ns,
                R.size(), st->N - 1);
    std::fflush(stdout);
    std::_Exit(0);
  }

  int empty_ok = 1;
  for (int i = 0; i < st->N; ++i) {
    if (st->ball[i] != -1) {
      empty_ok = 0;
      break;
    }
  }

  std::printf("%llu %llu\n", (unsigned long long)st->secret_hi,
              (unsigned long long)st->secret_lo);
  std::printf("OK %lld %d %d %d %d %d\n", ns, empty_ok, st->K, st->B, st->C,
              st->insert_count);
  std::printf("%d\n", (int)R.size());
  for (int i = 0; i < (int)R.size(); ++i) {
    std::printf("%d%c", R[i], (i + 1 == (int)R.size()) ? '\n' : ' ');
  }
  std::fflush(stdout);
  std::_Exit(0);
}
