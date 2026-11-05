#include <iostream>
#include <vector>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    int n, m;
    if (!(std::cin >> n >> m)) return 0;

    // 起步模板：仅演示输入输出接口（小规模暴力，请替换为高效算法）
    std::vector<std::pair<int, int>> segs(m + 1, {0, 0});
    std::vector<char> alive(m + 1, 0);
    for (int i = 1; i <= m; ++i) {
        int op;
        std::cin >> op;
        if (op == 1) {
            int l, r;
            std::cin >> l >> r;
            segs[i] = {l, r};
            alive[i] = 1;
        } else if (op == 2) {
            int t;
            std::cin >> t;
            alive[t] = 0;
        } else {
            int l, r;
            std::cin >> l >> r;
            std::cout << "N\n";
        }
    }
    return 0;
}
