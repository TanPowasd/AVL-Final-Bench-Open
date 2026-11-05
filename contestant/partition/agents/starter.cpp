#include "partition.h"
#include <vector>

std::vector<int> add_numbers(std::vector<int> A, int K, int M) {
    // 起步模板：返回至多 K - 1 个范围在 [1, M] 内的整数
    return {};
}

std::vector<int> find_partition(std::vector<int> B, int K) {
    // 起步模板：返回长度为 B.size() 的分组编号数组（0 .. K - 1）
    int L = static_cast<int>(B.size());
    std::vector<int> P(L, 0);
    for (int i = 0; i < L && i < K; ++i) P[i] = i;
    return P;
}
