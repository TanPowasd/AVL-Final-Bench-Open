#include "partition.h"

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

int main() {
  std::printf("__JUDGE_READY__\n");
  std::fflush(stdout);

  unsigned long long s_hi = 0, s_lo = 0;
  int phase = 0;
  if (std::scanf("%llu %llu %d", &s_hi, &s_lo, &phase) != 3) {
    return 1;
  }

  if (phase == 1) {
    int N = 0, K = 0, M = 0;
    if (std::scanf("%d %d %d", &N, &K, &M) != 3) {
      return 1;
    }
    std::vector<int> A(N);
    for (int i = 0; i < N; ++i) {
      if (std::scanf("%d", &A[i]) != 1) {
        return 1;
      }
    }

    int null_in = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_R_FLAGS);
    if (null_in != -1) {
      JUDGE_DUP2(null_in, JUDGE_FILENO(stdin));
      JUDGE_CLOSE(null_in);
    }
    std::fflush(stdout);
    int saved_out = JUDGE_DUP(JUDGE_FILENO(stdout));
    int null_out = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_O_FLAGS);
    if (null_out != -1) {
      JUDGE_DUP2(null_out, JUDGE_FILENO(stdout));
      JUDGE_CLOSE(null_out);
    }

    auto t0 = std::chrono::steady_clock::now();
    std::vector<int> C = add_numbers(A, K, M);
    auto t1 = std::chrono::steady_clock::now();
    long long ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();

    std::fflush(stdout);
    if (saved_out != -1) {
      JUDGE_DUP2(saved_out, JUDGE_FILENO(stdout));
      JUDGE_CLOSE(saved_out);
    }

    std::printf("%llu %llu\n", s_hi, s_lo);
    if ((int)C.size() > K - 1) {
      std::printf("FAIL %lld Too many added numbers: S=%zu > K-1=%d\n", ns,
                  C.size(), K - 1);
      std::fflush(stdout);
      std::_Exit(0);
    }
    for (size_t i = 0; i < C.size(); ++i) {
      if (C[i] < 1 || C[i] > M) {
        std::printf("FAIL %lld Added number C[%zu]=%d out of range [1, %d]\n",
                    ns, i, C[i], M);
        std::fflush(stdout);
        std::_Exit(0);
      }
    }

    std::printf("OK %lld\n", ns);
    std::printf("%d\n", (int)C.size());
    for (int i = 0; i < (int)C.size(); ++i) {
      std::printf("%d%c", C[i], (i + 1 == (int)C.size()) ? '\n' : ' ');
    }
    std::fflush(stdout);
    std::_Exit(0);

  } else if (phase == 2) {
    int L = 0, K = 0;
    if (std::scanf("%d %d", &L, &K) != 2) {
      return 1;
    }
    std::vector<int> B(L);
    for (int i = 0; i < L; ++i) {
      if (std::scanf("%d", &B[i]) != 1) {
        return 1;
      }
    }

    int null_in = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_R_FLAGS);
    if (null_in != -1) {
      JUDGE_DUP2(null_in, JUDGE_FILENO(stdin));
      JUDGE_CLOSE(null_in);
    }
    std::fflush(stdout);
    int saved_out = JUDGE_DUP(JUDGE_FILENO(stdout));
    int null_out = JUDGE_OPEN(JUDGE_NULL_DEV, JUDGE_O_FLAGS);
    if (null_out != -1) {
      JUDGE_DUP2(null_out, JUDGE_FILENO(stdout));
      JUDGE_CLOSE(null_out);
    }

    auto t0 = std::chrono::steady_clock::now();
    std::vector<int> P = find_partition(B, K);
    auto t1 = std::chrono::steady_clock::now();
    long long ns =
        std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();

    std::fflush(stdout);
    if (saved_out != -1) {
      JUDGE_DUP2(saved_out, JUDGE_FILENO(stdout));
      JUDGE_CLOSE(saved_out);
    }

    std::printf("%llu %llu\n", s_hi, s_lo);
    if ((int)P.size() != L) {
      std::printf("FAIL %lld Partition length %zu != B.size() %d\n", ns,
                  P.size(), L);
      std::fflush(stdout);
      std::_Exit(0);
    }
    for (int i = 0; i < L; ++i) {
      if (P[i] < 0 || P[i] >= K) {
        std::printf("FAIL %lld Group index P[%d]=%d out of range [0, %d)\n", ns,
                    i, P[i], K);
        std::fflush(stdout);
        std::_Exit(0);
      }
    }

    std::printf("OK %lld\n", ns);
    std::printf("%d\n", L);
    for (int i = 0; i < L; ++i) {
      std::printf("%d%c", P[i], (i + 1 == L) ? '\n' : ' ');
    }
    std::fflush(stdout);
    std::_Exit(0);
  }

  return 1;
}
