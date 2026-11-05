#include "partition.h"
#include <cassert>
#include <cstdio>
#include <algorithm>

int main() {
  int N, K, M;
  assert(3 == scanf("%d%d%d", &N, &K, &M));
  std::vector<int> A(N);
  for (int i = 0; i < N; ++i) {
    assert(1 == scanf("%d", &A[i]));
  }
  fclose(stdin);

  std::vector<int> C = add_numbers(A, K, M);

  std::vector<int> B = A;
  for (int x : C) {
    B.push_back(x);
  }
  std::sort(B.begin(), B.end());

  int S = (int)C.size();
  printf("%d\n", S);
  for (int i = 0; i < S; ++i) {
    printf("%d%c", C[i], " \n"[i + 1 == S]);
  }
  for (int i = 0; i < N + S; ++i) {
    printf("%d%c", B[i], " \n"[i + 1 == N + S]);
  }
  printf("\n");

  std::vector<int> P = find_partition(B, K);

  int L = (int)P.size();
  printf("%d\n", L);
  for (int i = 0; i < L; ++i) {
    printf("%d%c", P[i], " \n"[i + 1 == L]);
  }
  fclose(stdout);

  return 0;
}
