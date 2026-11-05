#include "ballmachine.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <vector>

namespace {

const int MAX_BALL = 1000;
// insert 上限：N<=1000 时为 5e5（原题）；超规模组为 500*N，即 max(500000, 500*N)。
long long MAX_INSERT = 500000;
const int MAX_RES = 1000;
int insert_count = 0;

int N, M, K, B, C;
std::vector<int> P, S, ball;
std::vector<std::vector<int>> children;

void quit(const char *message) {
  printf("%s\n", message);
  exit(0);
}

void dfs(int u) {
  std::vector<int> pending{u};
  while (!pending.empty()) {
    int cur = pending.back(); pending.pop_back();
    S.push_back(ball[cur]);
    sort(children[cur].begin(), children[cur].end(),
         [&](int x, int y) { return ball[x] < ball[y]; });
    for (auto it = children[cur].rbegin(); it != children[cur].rend(); ++it)
      if (ball[*it] != -1) pending.push_back(*it);
  }
}

} // namespace

bool insert(int U, int X) {
  ++insert_count;
  if (insert_count > MAX_INSERT) {
    quit("Too many calls");
  }

  if (U < 0 || U >= M || X < 0 || X > MAX_BALL) {
    quit("Invalid argument");
  }

  B = std::max(B, X);
  C = K + B;
  if (C > MAX_RES) {
    quit("Too many resources used");
  }

  while (P[U] != U && ball[P[U]] == -1) {
    U = P[U];
  }

  if (ball[U] == -1) {
    ball[U] = X;
    return true;
  } else {
    return false;
  }
}

std::vector<int> collect() {
  K++;
  C = K + B;
  if (C > MAX_RES) {
    quit("Too many resources used");
  }

  S.clear();
  if (ball[N - 1] != -1) {
    dfs(N - 1);
  }

  for (int i = 0; i < N; i++) {
    ball[i] = -1;
  }

  return S;
}

int main() {
  assert(2 == scanf("%d %d", &N, &M));

  MAX_INSERT = std::max(500000LL, 500LL * N);
  children.resize(N);
  P.resize(N);
  ball.resize(N, -1);
  P[N - 1] = N - 1;

  for (int i = 0; i < N - 1; i++) {
    assert(1 == scanf("%d", &P[i]));
    children[P[i]].push_back(i);
  }

  fclose(stdin);

  std::vector<int> R = find_structure(M);
  int Q = (int)R.size();

  printf("%d %d %d\n", K, B, C);
  printf("%d\n", Q);
  for (int i = 0; i < Q; i++) {
    printf("%d%c", R[i], " \n"[i + 1 == Q]);
  }
  fclose(stdout);

  return 0;
}
