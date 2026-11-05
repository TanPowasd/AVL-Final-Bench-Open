# 记忆检索（Memory Recall）

无外部模型、纯标准库长对话记忆与文档检索评测。你需要提交**单个 Python 文件**，对逐个输入的记忆库（`bank`）建立内存索引，并对每道查询问题返回最多 `50` 个按相关度降序排列的文档 `id`。

---

## 1. 交付与运行限制

- **最终统一提交路径（必交）**：`../submissions/mem_recall.py`（即工作区顶层 `submissions/mem_recall.py`；本地测试通过后必须将最终文件以题目名保存至此目录，正式评分仅从顶层 `submissions/` 自动分发）。
- **题内开发自测路径（可选）**：`agents/submissions/my_agent.py`（`scripts/spar.py` 同时支持直接读取 `../submissions/mem_recall.py` 或 `agents/submissions/my_agent.py`）。
- **语言与依赖**：单个 UTF-8 Python 3.10+ 源文件，仅允许使用 Python 标准库；严禁联网，严禁调用任何本地或远程神经网络、大模型或词向量库。提交运行时不得读取任何文件，包括 `data/`，索引只能从 `memory` 消息建立。
- **运行方式**：评测器为每个数据集启动一个独立子进程，通过 `stdin / stdout` 按行交互 UTF-8 JSON，`stderr` 被丢弃。子进程没有单独的握手，启动耗时计入第一条 `memory` 消息的时限。
- **开发集**：`data/dev/` 只用于本地调参。不要硬编码其中的题目或金标答案，正式分在互相隔离的隐藏集上计算，这样做拿不到分。
- **工作区边界**：读写范围和规则优先级见工作区顶层 `README.md`。
- **时限与容错规则**（权威评测器位于 `engine/runner.py`）：
  - **建库时限（`memory` 消息）**：`max(10.0 秒, 该 bank 全部文档 text 总字符数 / 10000 秒)`，同时不超过整集总时限的余量；
  - **单次查询时限（`query` 消息）**：`10.0` 秒，同时不超过整集总时限的余量；
  - **单个数据集总时限**：`120.0 秒 + 该集全部 bank 总字符数 / 10000 秒 + 问题总数 * 1.0 秒`。
  - 任一步超时、进程崩溃或输出非合法 JSON：评测器终止当前子进程，当前 `bank` 剩余未答问题全部记空排序 `[]`；进入下一个 `bank` 时重启子进程（每个数据集最多允许重启 `3` 次，超出后该集剩余问题全记空排序）。整集总时限耗尽时，剩余问题全记空排序。
  - 回复 JSON 不满足 `{"ranking": list}` 结构：记一次犯规，该题按空排序处理。
  - 返回列表中不存在于当前 `bank` 的 `id`、重复出现的 `id`（保留首次出现）以及题目 `exclude` 集合指定的 `id` 由评测器自动过滤，过滤后截取前 `50` 个计分。

---

## 2. 通信协议（`stdin / stdout` 逐行 UTF-8 JSON）

评测器对一个数据集内的每个 `bank` 依次执行以下交互（新 `bank` 到来后不会再询问旧 `bank`）：

1. **下发记忆库**：
   ```json
   {"type": "memory", "set": "locomo", "bank_id": "conv-30", "docs": [{"id": "D1:1", "text": "...", "date": "...", "speaker": "..."}]}
   ```
   - 每个元素必含字符串 `"id"` 与 `"text"`；记忆集可能额外包含 `"date"`、`"speaker"`、`"chapter"` 等元数据字段，标准检索集仅含 `"id"` 与 `"text"`。
   - 建库完成后输出任意一行合法 JSON（如 `{}`）并 `flush`。
   - 没有题目的 `bank` 不会下发。
2. **逐题查询**：
   ```json
   {"type": "query", "qid": "q_001", "bank_id": "conv-30", "question": "..."}
   ```
   - 输出一行：`{"ranking": ["doc_id_1", "doc_id_2", ...]}`（最多 `50` 个文档 `id` 字符串，按相关度从高到低排列）并 `flush`。
   - 不计分的题同样会下发，用时照算。查询消息只有上面这些字段，题目类别和金标不下发。
3. **结束通知**：
   - 当前数据集所有 `bank` 评测完毕后收到 `{"type": "end"}`，进程退出即可。

---

## 3. 评测指标与正式计分

评测集分为两类指标（仅对含有金标的计分题计算；无证据组或无 `qrels` 的拒答题不计入均值；未作答的计分题按空排序得 `0` 分）：

1. **记忆集（`group = "mem"`，主指标 `r@10`）**：
   每道计分题给定非空证据组列表 `evidence_groups = [G_1, G_2, ..., G_m]`，每组 `G_j` 是一个文档 `id` 集合（取前 `10` 名预测集合 `Top10` 与 `G_j` 交集非空即视为覆盖第 `j` 组）：
   $$\text{r@10} = \frac{1}{m} \sum_{j=1}^{m} \mathbb{I}[\text{Top10} \cap G_j \neq \emptyset]$$
2. **检索集（`group = "en"` 或 `"zh"`，主指标 `nDCG@10`）**：
   每道计分题给定相关度字典 `qrels = {doc_id: rel}`（正整数等级）。对过滤后的预测序列前 `10` 名 `d_1, ..., d_k`（`k <= 10`）：
   $$\text{DCG@10} = \sum_{i=1}^{k} \frac{2^{\text{rel}(d_i)} - 1}{\log_2(i + 1)}, \qquad \text{nDCG@10} = \frac{\text{DCG@10}}{\text{IDCG@10}}$$
   其中 `IDCG@10` 为将 `qrels` 中所有正相关文档按等级降序取前 `10` 名计算的理想 `DCG@10`（ArguAna 按 BEIR 规范在排序评估前排除与问句同 ID 的文档 `exclude`）。

**正式总分**：在隐藏测试集（`split = "test"`，与公开 `data/dev/` 同源划分但问题隔离）的 **18 个正式计分集**上，取各集主指标（记忆集 `r@10`、检索集 `nDCG@10`）的**等权宏平均**。本题成绩范围是 `0..1`，计入全赛总分时乘以 `100`。

`data/SHA256SUMS.json` 冻结了开发集和隐藏集的文件哈希。其中 `scoring/...`、`testset/...` 是数据原始归档里的路径前缀，不对应本工作区的目录。

---

## 4. 数据集构成（`data/dev/`）

本地 `data/dev/` 提供全部 `23` 个开发集文件（`18` 个对应正式计分集 + `5` 个仅练习集，`practice_only = true` 不计入宏平均）。大体积文件为 `.json.gz`，超过 45MB 的分片文件（`.partK`）由评测器自动合并：

| 数据集 `name` | 组别 `group` | 主指标 | 计入正式分 | `dev` 计分题数 / `bank` 数 |
|---|:---:|:---:|:---:|---:|
| `lme` | `mem` | `r@10` | 是 | 236 / 236 |
| `locomo` | `mem` | `r@10` | 是 | 257 / 10 |
| `locomo_zh` | `mem` | `r@10` | 是 | 201 / 2 |
| `hive` | `mem` | `r@10` | 是 | 46 / 1 |
| `perltqa` | `mem` | `r@10` | 是 | 3956 / 16 |
| `convomem` | `mem` | `r@10` | 是 | 1936 / 1936 |
| `membench` | `mem` | `r@10` | 是 | 3353 / 3353 |
| `kmb` | `mem` | `r@10` | 是 | 1050 / 3 |
| `cpzh` | `mem` | `r@10` | 是 | 41 / 1 |
| `scifact` | `en` | `ndcg@10` | 是 | 145 / 1 |
| `nfcorpus` | `en` | `ndcg@10` | 是 | 171 / 1 |
| `arguana` | `en` | `ndcg@10` | 是 | 252 / 1 |
| `scidocs` | `en` | `ndcg@10` | 是 | 254 / 1 |
| `EcomRetrieval` | `zh` | `ndcg@10` | 是 | 257 / 1 |
| `VideoRetrieval` | `zh` | `ndcg@10` | 是 | 260 / 1 |
| `MedicalRetrieval` | `zh` | `ndcg@10` | 是 | 227 / 1 |
| `DuRetrieval` | `zh` | `ndcg@10` | 是 | 233 / 1 |
| `MMarcoRetrieval` | `zh` | `ndcg@10` | 是 | 258 / 1 |
| `tmd` | `mem` | `r@10` | 否（仅练习） | 177 / 12 |
| `realtalk` | `mem` | `r@10` | 否（仅练习） | 679 / 10 |
| `cpzh_long` | `mem` | `r@10` | 否（仅练习） | 200 / 1 |
| `cpzh_bodydate` | `mem` | `r@10` | 否（仅练习） | 210 / 1 |
| `cpzh_dialog` | `mem` | `r@10` | 否（仅练习） | 176 / 4 |

---

## 5. 本地自测

公开基线位于 `agents/baselines/`（`bm25.py`、`charoverlap.py`），协议模板位于 `agents/starter.py`。不加 `--sets` 和 `--all` 时，`spar.py` 只跑快速集 `cpzh`、`hive`、`scifact`。

```bash
# 查看开发集列表与公开基线分数
python scripts/spar.py --list

# 在快速集或指定子集上测试
python scripts/spar.py ../submissions/mem_recall.py
python scripts/spar.py agents/submissions/my_agent.py --sets cpzh,hive,scifact

# 在全部 23 个开发集上运行完整评测
python scripts/spar.py agents/submissions/my_agent.py --all --jobs 2
```
