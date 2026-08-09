# 路线图与工作状态

[English](roadmap.md) · **中文**

活的计划文档。[`journal.md`](journal.md) 记录系统**已经是什么** —— 架构与已实现
行为；本文件记录它**还不是什么**，以及谁在推进哪一块。

## 如何更新本文件

由当天动过相关代码的人更新。

- 只改你负责的那一行的状态标记和 `更新日期`，其余保持不动 —— 一次改十行的 diff
  很难 review。
- 任务只有在**验收标准通过**时才标 ✅，代码写完不算。下面的验收标准都刻意写成了
  可执行、可检查输出的形式。
- 任务完成后，把实质内容迁进 `journal.md` 的 *Implemented* 段，这里只留一行。本
  文件应当保持简短。
- 发现了新问题？加到它所阻塞的那个阶段下面，并附验收标准。没有验收标准的条目是
  愿望，不是任务。
- 如果改动涉及门禁、别名表或推断规则，验收标准是一个 scorecard 数字，而不是"测试
  通过"。见 [`../CONTRIBUTING.md`](../CONTRIBUTING.md) 的
  *Measuring a judgement change* 一节。
- 对优先级有不同意见是正常且被鼓励的 —— 直接调整顺序，并在 commit message 里说明
  理由。

状态标记：✅ 已完成 · 🚧 进行中 · ⛔ 受阻 · ⬜ 未开始

### 两个语言版本的同步约定

[`roadmap.md`](roadmap.md) 是完整英文版，本文是完整翻译而非摘要。任务编号
（`P0.1`、`P1.4` …）是两份文件的共同主键 —— **不要单独在一份文件里重新编号**。

- **状态、负责人、日期的改动，必须在同一个 commit 里改两份文件。** 只有三个单元
  格，当场改比事后对账便宜得多。
- **正文改动先落英文版**，再补翻译。时间不够时，写完英文并在中文对应行留一个
  `TODO(zh)` 标记 —— 一份看起来是最新、实际已过时的翻译，比一份自己承认过时的更
  危险。
- 两份文件对同一事实说法不一致时，以英文版为准，修正中文版。

---

## 目标

服务于单细胞 ATAC 大模型语料建设：把一个自然语言研究问题，端到端地转成**带规范
Data Card 的可训练数据集**，且全程保留出处与许可依据。

三个里程碑，有先后：

1. **P0** —— 完成一次真实的端到端运行，至少有数据进入 `pretrain` 池。
2. **P1** —— 一期召回：配对 scATAC + scRNA（以及更多组学 —— 表面蛋白、其他表观
   组层），并达到规范质量。
3. **P2** —— 泛化到其他方向的数据集发现需求，并做出可当众演示的 demo。

---

## 已核实的现状 —— 2026-07-31

下面每日更新的基线。要推翻某一行，请先重新核实。

| 事实 | 证据 |
|---|---|
| 测试全绿，完全离线 | `pytest` → 194 passed；`ruff check src tests scripts` 无告警 |
| 策展判断已可度量 | `scripts/eval_discovery.py` 重放 `eval/records.jsonl`（48 条实网抓取记录）：`pairing_overclaim_rate` **0.00**、`modality_recall` **1.00**、`layer_recall` 1.00、`pairing_recall` 0.67 |
| 自然语言 → 审批检查点，实网跑通 | `var/p3_acceptance/stdout.json`：11 个 task 全部 completed，`status = waiting_for_decision`，3 个 recommended bundle，16.27 GB |
| 配对多组学问题能被正确理解 | 同一次运行，中文同细胞／三模态提问；需求携带了 `minimum_pairing_level` |
| **从未下载过任何字节** | 所有 `var/*/data/` 均为空目录；任何 sqlite 账本中都不存在 `transfer_execution` / `dataset_curator` / `corpus_governance` 任务 |

也就是说，流水线的后半程**目前只有测试验证**。这正是本路线图要改变的最重要的一
件事。

---

## P0 —— 先让整条路径真正跑通一次（最高优先级）

**本阶段的出口条件：** 一次真实的 `agent-run` → `resume` → 传输 → 物化 → QC →
语料快照，其中至少一个 bundle 进入 `pretrain` 池，且产出的数据包确实落在磁盘上。

P0 通过之前，P1 和 P2 的任何工作都不应开始。后续每一个功能，都是在拓宽一条尚未
接通的管道。

| # | 任务 | 状态 | 负责人 | 更新日期 | 验收标准 |
|---|---|---|---|---|---|
| P0.0 | 消除流水线对配对关系的高估（见下） | ✅ | | 2026-07-31 | `pairing_overclaim_rate` 0.40 → **0.00**、`modality_recall` 0.52 → **1.00**、`pairing_recall` 保持 0.67；所有问题的 `forbid_modality_pass` 均为空；基线已更新 |
| P0.1 | 选定一个小体量、已知良好的试点研究（10x Multiome、许可清晰、个位数 GB），并把 accession 记在本行 | ⬜ | | | accession 已写入本行；对窄问题执行 `agent-run` 能把它判为 `recommended` |
| P0.2 | 常见 10x 处理后布局 → AnnData/MuData 的转换器 | ⬜ | | | `barcodes.tsv.gz` + `peaks.bed` + `matrix.mtx.gz`（+ `fragments.tsv.gz`）的数据包能产出非空的 `standard_object_path`；配套离线测试须取材于真实 GEO 文件清单 |
| P0.3 | 首次真实传输执行 | ⬜ | | | 账本中出现带成功回执的 `transfer_execution` 任务；中途打断后恢复，验证断点续传与校验和均成立 |
| P0.4 | 首次真实物化 | ⬜ | | | `data/curated/<candidate>/<bundle>/` 下同时存在 `DATA_CARD.md`、`provenance.json`、`manifest.json` 和标准对象 |
| P0.5 | 首个真实语料快照 | ⬜ | | | 快照的 `admitted_bundle_ids` 非空；`corpus_approval` 检查点已解决 |

### P0.0 —— 配对关系被高估（2026-07-31 已修复）

`eval/records.jsonl` 中 30 个 standalone snATAC-seq deposition，曾有 12 个把自己
报成**同核配对多组学**。这是假阳性，所以它从不自己现身：同细胞请求会静默地收进不
具备该性质的 standalone ATAC 运行，没有任何东西进入 manual review，唯一可见的现象
是召回看起来变好了。

成因是三条，不是最初记录的两条。每一条现在都由 `tests/test_pairing_overclaim.py`
中一个用**归档真实载荷**构造的测试钉住：

1. **推断跑在序列化元数据上，而不是正文散文上。** NCBI 的 SRA ESummary 返回的是
   原始 Experiment XML。`contact_name="Gene Expression Omnibus (GEO), NCBI, NLM,
   NIH"` 出现在每一条经 GEO 中转的记录里，而 `gene expression` 是 RNA 别名 ——
   转录组层就从投稿人姓名里凭空长了出来；配对判定则来自一份名为
   `Chromium_Multiome_ATAC_GEX.pdf` 的协议文件。已由 `prose_text()` 修复：在任何
   别名匹配之前，连同属性一起剥离标记、URL 和文档文件名。
2. **别名做的是裸子串匹配。** 现已加词边界，`adt` 不会再命中更长的词内部。这也暴露
   出 `RNA_ALIASES` 从来就没收录单核拼写 —— `snRNA-seq` 一直只是靠 `rna-seq` 在其
   内部的碰撞才被命中 —— 现已显式列出，与 `ATAC_ALIASES` 对称。
3. **ENCODE 的伞形 BioProject 被当成了数据集身份。** ENCODE 把每一个实验都归在
   `PRJNA63443` 和 `SRP012412` 之下。在其上做合并，把 **13 个不同的 GEO Series 压
   成了一个候选**，候选的模态列表变成整组的并集 —— 于是组里一条真正的多组学记录，
   就把所有 standalone snATAC 都放进了同细胞问题。仅靠文本推断抓不到这一条：标签是
   对的，错的是合并。`domain/resolution.py` 现在先应用研究级标识符，并拒绝任何会把
   "已经对自己属于哪个研究有分歧"的两组合并的后续操作。不需要维护伞形 accession
   名单 —— 伞形标识符就是那些成员彼此矛盾的标识符。

第 3 条正是这件事需要 scorecard 而非代码评审的原因：前两条修完后
`pairing_overclaim_rate` 已经是 0.00，而 standalone ATAC 仍在通过另一条路径进入同
细胞问题。

**防复发。** `forbid_pairing_above` 约束单条记录能声称到什么程度；本次新增的
`forbid_modality_pass` 则点名"某个问题绝不允许放行的记录"，并且是**绝对检查** ——
与基线持平也不能豁免。因为把一条本就不该通过的记录排除掉，会**拉低**
`deposition_precision`，使得修复和缺陷在增量上长得一模一样。确实有两个问题的
precision 因此下降，原因已写进 `eval/golden_questions.yaml` 对应的 note 里。

### 为什么 P0.2 是硬阻塞

`snapshot_governance.py:118` 会把任何 `manifest.standard_object_path is None` 的
bundle 判入 quarantine，理由为 `anndata_or_mudata_missing`。而
`standard_object` 目前只在下载到的文件**恰好**是 `.h5ad`/`.h5mu` 时才有值
（`materialization.py:76-84`）。典型的 GEO 处理后数据交付的是 mtx/tsv/bed，永远
不会自带 h5ad。所以在今天，即使下载成功，最终的 admitted 名单也会是空的 —— 转换器
才是接通这条电路的那一环。

---

## P1 —— 输出规范化与一期召回

一期目标是配对 scATAC + scRNA；更多组学（ASAP-seq/TEA-seq 的表面蛋白、其他表观组
层）复用同一套 schema。

| # | 任务 | 状态 | 负责人 | 更新日期 | 验收标准 |
|---|---|---|---|---|---|
| P1.1 | 把 `DATA_CARD.md` 扩展到规范字段集（见下） | ⬜ | | | P0 试点的 card 携带全部必需字段；每个值都可追溯到某条 evidence 记录，或显式标记为 unknown |
| P1.2 | 传输后读取归档文件清单 | ⬜ | | | 一个 `GSExxxxx_RAW.tar` 的 bundle 能产出真实的 role 推断，而不是 `archive:uninspected`；不允许对未打开的内容下判定 |
| P1.3 | ENCODE 适配器 + 许可政策 | ⬜ | | | resolver 能返回资产；政策已在 `domain/licensing.py` 注册；`encode` 已从 `QueryPlan.uncovered_sources` 移除 |
| P1.4 | CELLxGENE 适配器 + 许可政策 | ⬜ | | | 同上，针对 `cellxgene`。ENCODE 之后价值最高的源：策展过的图谱且许可明确声明，同时它也是唯一能真正触发 `approved` 许可状态的源 —— GEO/SRA 永远只能到 `repository_policy` |
| P1.5 | 在真实研究上跑通三模态路径 | ⬜ | | | 一个 TEA-seq 或 DOGMA-seq 的 deposition 端到端满足 `multiome_trimodal` |
| P1.6 | 超越文本推断的配对证据 | 🚧 | | 2026-08-09 | 实测的逐细胞条码交集，能把 bundle 从 `pairing:unverified` 升级为有 `FILE_OBSERVED` 证据支撑的同细胞判定。这是 P0.0 的根治方案：高估来自文本推断，而实测条码不会说谎 |

### Data Card 规范化（P1.1）

当前的 card 只有九行：bundle id、candidate、物化状态、data roles、标准对象、许可
依据。这足够审计"为什么被收录"，但不足以让任何人判断"要不要拿它训练"。

对齐既有标准而不是自创：文档结构对齐 HuggingFace Dataset Card，机器可读元数据对齐
Croissant。必须补充的字段：

- 物种、组织、疾病状态、细胞类型
- 细胞数、样本数、donor 数
- 基因组版本（已经算进 `provenance.json` 的 `genome_builds`，只是没有呈现在 card 上）
- 配对层级，**以及它所依据的 evidence 类别**
- QC 指标与门禁结论
- 源 accession 与引用
- 语料池归属（pretrain / finetune / evaluation vault）

按 `CONTRIBUTING.md` §2，这一条不可让步：每个字段要么可追溯到 evidence 记录，要么
写成 unknown。一个看起来合理、但没人观察过的值，比留空更糟。

### P1.6 —— 超越文本推断的配对证据（2026-08-09 已构建，尚未经实网验证）

`PairUnitResolver`（提议哪些文件构成一个可独立策展的物理单元；未能真正解析的分组
会显式标为 `provisional`，而不是硬猜一个）和 `PairingVerifier`（唯一被允许发出
`has_pair_relationship` 声明的组件）取代了原来一次性的文本推断，改为分级证据：命
名的联合检测技术、或该单元自身样本文本里的显式同细胞声明（`REPOSITORY_REPORTED`）
优先；两者都拿不到时才走 LLM 推断（`MODEL_INFERRED`，且单独存在时永远不足以满足
硬性的物理配对要求）。任何一级都会拒绝对 `provisional` 单元下判断 —— 一个 assay
级别的先验说的是"这个检测方法支持共测量"，而不是"这一坨还没被真正分组的文件就是
一个正确的物理单元"。

物化之后，`MaterializedQualityAssessmentAgent` 会用真实的 RNA/ATAC 条码交集
（`resource_pipeline.barcode_overlap_pairing_claim`）以 `FILE_OBSERVED` 强度重新
计算该声明 —— 对应上面的验收标准 —— 且 `CurationProfile.required_evidence`（见
`paired_atac_rna_human`）把 `FILE_OBSERVED` 定为 CORPUS_READY 门唯一能满足
`has_pair_relationship` 的证据类别，而进入 acquisition 阶段只需要更弱的
`REPOSITORY_REPORTED`/`MODEL_INFERRED` 下限。`tests/test_qc_metrics_v2.py::
TestRnaAtacBarcodeOverlap` 用离线合成数据验证了条码交集的计算本身。

尚未打 ✅：还没有在一次真实下载的数据集上跑过。`tests/regression/
paired_atac_rna_human/`（Phase 7，2026-08-08 落地）已经有 13 个源自策展人真实人工
审核的 golden case，但 `test_real_pipeline_matches_the_curator` 仍然是
`@pytest.mark.skip` —— 要接上它，需要对真实来源跑一次带真实 LLM 的 `agent-run`，
并把结果录成离线 replay corpus（见该目录的 README）。这次实网抓取，就是 P1.6 剩下
的验收工作。

同一批改动顺带把原来的 `Modality` 枚举泛化成了 `OmicsLayer` + 一个注册制的
`CurationProfile`（`domain/curation/`，目前只注册了一个实例：
`paired_atac_rna_human`）—— 内核只读取这份声明数据（每个谓词所需的证据、bundle
schema、分组信号、检索提示），不再对它分支判断。这已经是朝 P2.1 迈出的实质一步，
但还没有靠注册第二个 profile 来证明。

---

## P2 —— 泛化与 Demo

| # | 任务 | 状态 | 负责人 | 更新日期 | 验收标准 |
|---|---|---|---|---|---|
| P2.1 | 把 `BUNDLE_SCHEMAS` 从固定枚举改造成注册表 | 🚧 | | 2026-08-09 | 新增一个模态方向只需添加数据与 role 规则，无需改动完备性判定引擎 |
| P2.2 | role 推断突破以 ATAC 为中心的文件名约定 | ⬜ | | | 取自第二个方向（纯 scRNA、空间或甲基化）真实文件清单的测试用例能正确分类 |
| P2.3 | 控制室中的演示路径 | ⬜ | | | 单页串联 问题 → 计划 → 候选 → 审批 → data card；旁观者无需讲解即可跟上 |
| P2.4 | 可复现的演示脚本 + 预置数据库 | ⬜ | | | 从干净 checkout 离线运行；无需 API key、无需网络，每次输出一致 |

**"泛化"的实际成本是什么：** `BUNDLE_SCHEMAS`（`domain/requirements.py`）和基于
文件名的 role 推断，在构造上就是以 ATAC 为中心的。模态**识别**已经泛化了 ——
`MODALITY_ALIASES` 被两条编译路径共享，未识别的技术名也会作为
`supplementary_search_terms` 保留下来。真正不泛化的是**完备性判定**：一个新方向
需要它自己的 schema 与 role 规则。P2.1 的意义就在于让这件事变成"添加"，而不是
"手术"。

`domain/curation/CurationProfile`（见上面 P1.6 的说明）正是这个注册表——
`bundle_schemas`、`required_evidence`、`curation_unit_grouping`、`search_hints`
按 profile 声明，内核读取实例上的字段，而不是对拿到了哪个 profile 分支判断。仍标
🚧 而非 ✅：目前只注册了一个 profile（`paired_atac_rna_human`），"新增方向无需碰
引擎"这句话还没有靠真的注册第二个 profile 来验证过。

---

## 明确推迟 —— 现在不做

都是真实的工作，但都不推动 P0–P2。等 P1 落地后再重新评估。

- 规范化的 donor/sample/cell 实体与本体 ID
- 语料优化器：多样性、稀有度、冗余上限、可审计的采样权重
- 参考基因组支撑的 TSS enrichment、doublet 检测、完整矩阵稀疏度
- FASTQ → matrix 处理（明确的高成本项；保持为需显式开启的工作流）
- 抽取 accession 的语义关系（`primary_data_deposition` / `reused_dataset` /
  `supplementary_reference`）
- Single Cell Portal、HCA、BioStudies/ArrayExpress 适配器
- EGA / dbGaP 永久保持 metadata-only —— 这是设计决策，不是待补的缺口
