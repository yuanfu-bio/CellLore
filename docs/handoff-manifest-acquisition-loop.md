# Handoff: 打通 scdata-agent 的下载 → 整理 → 归档闭环

> **交接文档**。S1 已完成并入库，S2 刚开始。本文件包含继续这项工作所需的全部上下文：
> 为什么做、已探明的代码实情、已完成什么、下一步怎么做。
>
> - 分支：`manifest-acquisition-loop`（基于 `main` @ `7c894e0`）
> - 完整原始方案：`/data/home/yfu/.claude/plans/data-collab-ai-multiomics-yfu-02-agent-imperative-fountain.md`
> - 状态：**S1 完成**（241 tests passed, ruff clean）→ **S2 进行中**（仅完成 2 项预备改动）

---

## 0. 环境（先做这一步）

仓库根目录：`/data/collab/ai-multiomics/yfu/02_agent/05_develop`

```bash
# 注意：本机 `python` 默认指向 miniconda base，没有本项目依赖。
# 必须用这个 env，或 mamba activate scdata-agent。
export P=/data/home/yfu/.local/share/mamba/envs/scdata-agent/bin
$P/python -m pytest          # 应为 241 passed
$P/ruff check src tests      # 应为 All checks passed
```

`scdata-agent` CLI 已 editable 安装，`scdata-agent --help` 可用。

参考资料（只读，不在本仓库内）：

| 路径                                                                                                                                                   | 是什么                                                                                    |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `/data/collab/ai-multiomics/yfu/04_thinking/paired_data/human_paired_scRNA_scATAC_agent_workflow.md`（实际在 `02_agent/04_thinking/paired_data/`） | 设计文档，中文。§7 下载、§8 验证、§10 agent 架构、§19 实施顺序                        |
| `02_agent/04_thinking/paired_data/required_files_manifest.csv`                                                                                       | **真实数据源**：130 文件 / 32 pair unit / 9 dataset / ~85.5 GB，中文列名，UTF-8 BOM |
| `02_agent/04_thinking/paired_data/paired_multiome_downloader/multiome_downloader.py`                                                                 | 1106 行单文件原型，已验证可用的 aria2 下载器。**要移植的代码在这里**                |

---

## 1. 为什么做这件事

`scdata-agent` 的 LangGraph 管线在纸面上完整：

```
research_planner → planning_gate → resource_discovery
  → {asset_scout_direct ∥ evidence_metadata} → evidence_escalation_gate
  → {evidence_fulltext | asset_scout_enriched} → inventory_reconcile
  → {preflight_assessment ∥ early_lineage} → acquisition_strategy
  →【download_approval 人工门】→ transfer_execution → dataset_curator
  → materialized_quality → late_lineage → corpus_governance
  →【corpus_approval 人工门】→ finalize
```

**但它从未落过一个字节。** `data/raw` 为空，所有 `var/*/data/` 为空，任何 ledger 里都没有 `transfer_execution` 任务记录（`docs/roadmap.md:70`）。

根因不是下载器不行，是下游有一串从未被执行验证过的实现缺陷。最硬的一条：`snapshot_governance.py:118` 把 `standard_object_path is None` 的 bundle 全部隔离，而 `materialization.py:76-84` 只对 `.h5ad/.h5mu` 设该字段 —— GEO 典型的 mtx/tsv/bed 即使下载成功，`admitted_bundle_ids` 也必然为空。

同时检索层质量已知不足（P0.0 配对过度声称刚修完，`deposition_precision` 仍只有 0.5–0.77）。**本轮明确不碰检索**，改用人工审计过的 manifest 作为下游输入，让执行层第一次跑通。这是设计文档 §19 的建议顺序：先保证执行层可靠，再接检索。

### 用户已确认的四项决策（不要重新讨论）

1. 下游闭环优先，检索层本轮不动。
2. **新增一等公民的外部 manifest 摄取通道**（非一次性脚本）；检索产出的候选与人工 manifest 共用同一条下游路径。
3. **aria2 为主、httpx 兜底**。
4. **放宽 governance、保留原生格式**：`standard_object_path` 从硬门禁降为质量维度，本轮不做 AnnData 转换。

### 最终验收

`data/curated/GSE178707_NEATseq/<bundle_id>/` 内有真实字节 + `manifest.json` / `DATA_CARD.md` / `provenance.json`，且 `admitted_bundle_ids` 非空。

---

## 2. 必须遵守的项目约定

来自 `CONTRIBUTING.md`，违反会被 review 打回：

### 五条不可协商项

1. **同意之前不落字节。** `download_approval` 解决之前不得下载，`corpus_approval` 之前不得发布快照。新增节点必须放在门的正确一侧。
2. **不声称未观察到的事。** `FILE_OBSERVED` / `RECOMPUTED` 证据要求本地字节。来自仓库元数据的事实必须标为「仓库声称」。
3. **未知不等于缺失。** 未打开的归档是 `archive:uninspected`，不是 `bundle:missing_roles`。
4. **不绕过访问控制。** 只读开放全文；EGA / dbGaP 保持 metadata-only。
5. **确定性门禁保持确定性。** LLM 可以排序、总结、提议；准入判定是可审计的普通代码。

### 分层（由 `tests/test_architecture.py` 强制）

```
domain  <-  application  <-  interfaces
   ^             |
   +-- infrastructure
```

`domain` 不 import 其他层的任何东西。纯模型/门禁/推断规则进 `domain`；**I/O 进 `infrastructure`**；agent 与 LangGraph 接线进 `application`。

### 测试全离线

整个测试套件不得发网络请求，用 `monkeypatch` + `httpx.MockTransport`。**在测试里起 aria2 子进程和发网络请求一样被禁** —— 这决定了 aria2 执行器必须设计成可注入 runner。

修分类或门禁 bug 时，用**真实**仓库文件名/载荷建用例（`tests/test_role_inference.py` 是范本）。

### 冻结接缝（CODEOWNERS 需双人 review）

`domain/models.py`、`domain/lifecycle.py`、`application/orchestration_v2.py`。

**本方案全程零改动这三个文件。** 所有扩展走：开放 dict 字段（`structural_checks` / `metrics` / `policy`）、非冻结的 `collaboration.py` 追加枚举成员、新模块、或子类覆写。请保持这个性质，并在 PR 里逐项声明。

### 判断变更要动 scorecard

```bash
$P/python scripts/eval_discovery.py --out var/scorecard_mine.json
$P/python scripts/compare_scorecards.py eval/baseline_scorecard.json var/scorecard_mine.json
```

**本方案预期 delta 为零**（已核实：`scripts/eval_discovery.py` 只 import `DiscoveryWorkflow` / `RuleBasedRequirementCompiler` / `infer_assay_profiles`，从不触及 `infer_data_role`、`BUNDLE_SCHEMAS`、preflight、acquisition、transfer、materialization、governance）。但仍需实跑一次**证明**它为零，并把证明写进 PR。

诚实表述：§5.1（C3 代表修复）与 §7.1（归档警告修复）按 CONTRIBUTING 定义**确实是** judgement change，只是 scorecard 止步于 feasibility 因而测不到；改用具名测试钉住 —— 不要写成「没有判断被改变」。

### 其他

- ruff：line-length 100，target py312，规则 `E4,E7,E9,F,I,B,UP,C4`
- 注释解释 **why** 且在较高抽象层（范本：`materialization.py:86-88`、`resource_pipeline.py:51-53`）
- 代码与注释用英文；与用户对话用中文

---

## 3. 探索阶段发现的七条实情（改变了方案形状）

这些是花了大量时间从代码里挖出来的，**不要重新推导**。C2/C3/C7 已用真实数据验证。

| #                     | 发现                                                                                                                                                                                                                                                                                                                                                                                           | 影响 |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---- |
| **C1**          | 只改`snapshot_governance.py:118` 不够。`materialization.py:230-233` 把 `GateStatus.UNKNOWN` 等同于 `WARN`；原生包的 `frip_computable` / `barcode_linkage` / `standard_object_hdf5` 全是 UNKNOWN → 报告 WARN → 在 `:124-128` 以 `materialized_qc_warning` 隔离。**需要 `materialization.py` 四处改动**。「跑不了的检查」不是质量问题 —— 这本身就是不可协商项 #3。 |      |
| **C2** ✅已验证 | 7 个`tabix_index` 行文件名为 `*.fragments.tsv.gz.tbi.gz`（6 个）和 `*.tbi`（1 个），**全部**命中 `materialization.py:183` 的 `"fragment" in path.name.casefold()` 判断，被当 fragment 文本解析 → `_sample_fragments`（`:250`）抛异常 → `:187` 转成 `readability=FAIL` → 整个 bundle 隔离。必须按 **role** 而非文件名门控。                                    |      |
| **C3** ✅已验证 | `candidate_id = dataset_id` 时，一个数据集只有一个 pair unit 能进 `recommended`。`progressive_lineage_v2.py:99-133` 按 candidate 做 union-find，`acquisition_v2.py:56-58` 每 family 只选一个代表，`:110-111` 给其余打 `lineage:non_representative` → `manual_review`。今天不可见是因为 `inventory_reconcile.py:114` 每个 candidate 恰好一个 bundle。                          |      |
| **C4**          | `inventory_reconcile` **不能**作为 manifest 入口（同上，一 candidate 一 bundle）。摄取节点必须自己产出 `ASSET_INVENTORY`，并合成 `DiscoveryRunResult` —— `progressive_preflight.py:37,99` 与 `progressive_lineage_v2.py:33` 都调 `need_discovery()`，且 preflight 在 `:99` 做 `by_candidate[bundle.candidate_id]`，缺 candidate 会 KeyError。                          |      |
| **C5**          | Zenodo tonsil bundle 会因 licence 隔离，与归档工作无关。`domain/licensing.py:71-74` 只覆盖 NCBI/EBI，`policies_for_urls` 任一 URL 未覆盖即返回 `[]` → `LicenseStatus.UNKNOWN` → `snapshot_governance.py:109-114` 隔离。**不要加 Zenodo 通用策略** —— Zenodo licence 是逐记录的，我们没读过这一条。                                                                         |      |
| **C6**          | `本地相对路径` **不能**当 `DataAsset.file_name`：`transfer_executor.py:127-136` 拒绝任何非纯 basename。相对路径必须走 sidecar 记录。                                                                                                                                                                                                                                               |      |
| **C7** ✅已验证 | 首个里程碑用`GSE178707_NEATseq / CD4_memory_lane2_GSM5396336__GSM5396337`：**5 文件 0.338 GB**，全 `DIRECT_GEO` / `VERIFIED` / `GRCh38`（阶段是 **P1**，原方案误写为 P0）。`GSE207308_BMMC` 只有 3 行但 1.887 GB 且 RNA 是 `.h5`，既不练 MEX 维度也不练 barcode 交集 —— 它是更好的**第二个**用例。                                                             |      |

### 其他已核实的事实

- **123 条 `DIRECT_GEO` URL 已经全部等于重算出的静态 URL**。重算是护栏不是修复 —— S1 实测 0 个 `geo_url_recomputed` 警告。
- **`infer_data_role` 对 130 行中的 27 行不正确**。S1 实测报出 5 条真实分歧（4 条 DOGMA 的 RNA 矩阵因文件名含 `GExp_ATAC` 被误判为 `PEAK_MATRIX`；1 条临床元数据因 `446267cells_meta` 被误判为 `CELL_METADATA`）。
- `aria2c` 在当前 env **已安装**（`.../mamba/envs/scdata-agent/bin/aria2c`）；`openpyxl` **未安装**。
- `standard_object_path` 只出现在 6 处（见 §6.3），无其他依赖。
- `applicable_bundle_schemas(set())` 返回**全部** schema（`requirements.py:124-126`）—— 这是空 modality 合成 candidate 方案的依据，已读码确认。

---

## 4. 已完成的工作（S1）

分支 `manifest-acquisition-loop`，**241 tests passed，ruff clean**。

### 新增文件

| 文件                                                   | 行数      | 内容                                                                                                                                                                                                                                                                                                                      |
| ------------------------------------------------------ | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/scdata_agent/domain/repository_urls.py`         | 96        | `geo_static_url()` / `geo_bucket()` / `geo_namespace()` / `is_geo_accession()` / `canonical_assembly()`。从原型 `:171-190`、`:161-169` 移植，纯字符串运算，零依赖                                                                                                                                           |
| `src/scdata_agent/domain/manifest.py`                | 621       | `ManifestDialect` / `ManifestRow` / `ManifestDocument` / `ManifestIssue` / `IssueSeverity`、`CANONICAL_FIELDS` / `REQUIRED_FIELDS`、`PAIRED_MULTIOME_ZH_V1` 方言、`detect_dialect()` / `dialect_by_id()` / `normalize_row()` / `preflight_manifest()` / `parse_size()` / `summarize_issues()` |
| `src/scdata_agent/infrastructure/manifest_reader.py` | 134       | `read_tabular()`（按后缀分派 csv/tsv/xlsx）/ `read_manifest()` / `file_sha256()`；XLSX 走 lazy import + `XLSX_EXTRA_HINT`                                                                                                                                                                                         |
| `tests/data/manifest_paired_multiome_sample.csv`     | 15 行数据 | **从真实 manifest 逐字节抽取**，保留 BOM 与中文表头：3 行 BMMC、5 行 NEATseq CD4_memory_lane2、3 行 kidney SHARED_METADATA、2 行 Zenodo、1 行 10x portal、1 行 DOGMA `GSM5065531`                                                                                                                                 |
| `tests/test_manifest_dialect.py`                     | 113       | 方言识别、必需列缺失、XLSX extra 错误串、`parse_size` 参数化（含空值 → `None` 而非 `0`）                                                                                                                                                                                                                           |
| `tests/test_geo_static_url.py`                       | 105       | 真实 accession 的精确 URL、bucket 算术、百分号编码、非 GEO accession 拒绝、`canonical_assembly` 参数化                                                                                                                                                                                                                  |
| `tests/test_manifest_role_mapping.py`                | 106       | 20 个角色全映射、未知角色 → ERROR、真实 DOGMA 行上 manifest 角色胜出、`.tbi` 不被当 fragments                                                                                                                                                                                                                          |

**共 45 个新测试。**

### 修改的文件

| 文件                                          | 改动                                                                                                                                                                    |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.gitignore`                                | `data/` → `/data/`（锚定到仓库根）。**这是个真 bug**：无锚定的 `data/` 把 `tests/data/` 也忽略了，测试固定装置永远提交不上去，CI 上 3 个测试文件全会失败 |
| `interfaces/cli.py`                         | 新增`manifest-check` 命令 + `filter_manifest_rows()` / `filter_manifest_issues()` / `_render_manifest_report()`（+165 行）                                      |
| `application/collaboration.py`              | 追加`AgentName.MANIFEST_INGEST` 与 `ArtifactKind.MANIFEST_INGESTION`（S2 预备）                                                                                     |
| `application/resource_pipeline.py`          | 新增模块级`summarize_bundle()` 与 `asset_file_type()`（从 `InventoryReconcileAgent._summarize` 提取，纯搬移）（S2 预备）                                          |
| `application/agents/inventory_reconcile.py` | `_summarize` / `_file_type` 改为委托给上面两个函数，删掉 83 行重复实现                                                                                              |

### 实测结果

```bash
$ scdata-agent manifest-check <真实 manifest>
dialect=paired_multiome_zh_v1 sha256=96dee6074f2047f5…
rows=130 pair_units=32
issues: 0 error, 5 warning, 5 info      # warning 全是 role_inference_disagreement
                                        # info 全是 portal_resolution_required
总计 85.48 GB
```

与方案预期完全一致（130 / 32 / 85.5 GB / 0 error）。

### S1 的关键设计决定

- **方言是数据不是代码**。原型把 19 个中文表头写死成模块全局（`multiome_downloader.py:49-67`），加第二种布局要重写。现在 `ManifestDialect` 是个 pydantic 对象，加方言 = 加实例。
- **角色词表必须全覆盖且封闭**。未映射的 `file_role` 是 **ERROR**，不是静默 `OTHER` —— 否则新方言会悄悄退化成一堆未分类文件。
- **manifest 角色永远压过文件名推断**，但 `infer_data_role` 照跑，分歧记 WARNING。这给 `assets.py:ROLE_PATTERNS` 一个免费的长期回归探测器（已经抓到 5 个真实误判）。
- **空 size 是 `None` 不是 `0`**（原型 `is_large` 的 bug：空 size 被当小文件，多 GB 文件被塞进 12 并发的小文件队列）。
- **XLSX 建接缝、缓依赖**：`read_tabular` 的 `.xlsx` 分支 lazy import openpyxl，缺失时抛带安装指引的 `ManifestFormatError`。需在 `pyproject.toml` 加 `[project.optional-dependencies] manifest = ["openpyxl>=3.1,<4"]`（**S8 待做，尚未加**），但不放进 `dev` —— CI 保持 CSV-only。

---

## 5. 未完成的工作

### S2 — manifest 摄取（进行中，只完成了 2 项预备改动）

**已完成**：`AgentName.MANIFEST_INGEST` / `ArtifactKind.MANIFEST_INGESTION` 枚举成员；`summarize_bundle()` 提取。

**待做**：

新建 `src/scdata_agent/application/manifest_ingestion.py`：

```python
def ingest_manifest(document, *, run_id, filters, policy) -> ManifestIngestion
```

新建 `src/scdata_agent/application/agents/manifest_ingest.py`：`ManifestIngestAgent`（`name = AgentName.MANIFEST_INGEST`）。

**行 → 契约映射**：

| manifest                                                                                                              | 目标                                                                                                                                                                                          |
| --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dataset_id`                                                                                                        | `DatasetCandidate.candidate_id`、`DataAsset.candidate_id`、`SemanticBundle.candidate_id`                                                                                                |
| `pair_unit_id`                                                                                                      | `SemanticBundle.bundle_id = stable_id("manifest_bundle", f"{dataset_id}::{pair_unit_id}")`                                                                                                  |
| 每行                                                                                                                  | `DataAsset` + 一个 `AssetLocation`，`asset_id = stable_id("manifest_asset", f"{dataset_id}::{pair_unit_id}::{relative_path}")`（`relative_path` 130-distinct，无碰撞且跨 rerun 稳定） |
| `file_name`                                                                                                         | `DataAsset.file_name`（纯 basename，C6）                                                                                                                                                    |
| `relative_path` / `pairing_key` / `post_download_action` / `checksum_policy` / `stage` / 原生 `file_role` | **sidecar 记录**（`post_download_action` 是 25 条自由文本，永不机器解析）                                                                                                             |

**关键约束**：

- **合成 candidate 的 `modalities` 留空**。从 RNA+ATAC 行同时存在就推出 `MULTIOME_RNA_ATAC` 等于在新地方重演 P0.0 过度声称。留空使 `applicable_bundle_schemas` 提供全部 schema，纯按内容判定。`assay_profiles` 同样留空 → `pairing_gate = UNKNOWN`，诚实，因为还没人观察过配对。
- **仓库声称保持仓库声称**（不可协商项 #2）：每个 asset 配 `RemoteAssetObservation`，`probe_method="curated_manifest"`、`availability=DECLARED`（绝不 `OBSERVED`）、`access_level=PUBLIC_DECLARED`（绝不 `PUBLIC_VERIFIED`）、`http_status=None`。
- **`audit_status=VERIFIED` → `Evidence(evidence_class=EvidenceClass.USER_CORRECTED)`**，带 `snapshot_hash=<manifest sha256>` 与 `locator=f"{file}#row={i}"`。三条在代码里强制的规则：(a) 永不是 `FILE_OBSERVED`/`RECOMPUTED`；(b) 永不设 `OBSERVED`/`PUBLIC_VERIFIED`；(c) 永不设 `license_status=APPROVED`（人写 VERIFIED 意思是「我确认 URL 能解析」，不是「我读过 licence」）。
- **三类特殊行**：`PORTAL_CLICK_REQUIRED`（5 行）→ `protocol=MANUAL` + `availability=UNKNOWN` + `size_bytes=None`，最终落 `manual_review`，永不 `recommended`（不可协商项 #4 禁止原型的 `resolve-10x` Playwright 抓取）；`DIRECT_ZENODO`（2 行）→ 真 HTTPS，其中一条带全 manifest 仅有的 2 个 MD5 之一；`multiome_archive`（1 行 19.6 GB）→ `DataRole.ARCHIVE`。
- **可选元数据升级**：`policy["remote_probe_enabled"]` 时在 preflight 前跑现成的 `RemoteAvailabilityProbe`（只发 HEAD / `Range: bytes=0-0`，是元数据不是字节，稳在同意门正确一侧）。**不跑它则每个 manifest bundle 都是 `access:declared_only` 而落入 `manual_review`**。默认开。

**产出 4 个 artifact**（按序）：`RESOURCE_POINTERS`（`origin_stage="curated_manifest"`）、`DISCOVERY_RESULT`（合成的）、`ASSET_INVENTORY`（payload 形状**照抄** `inventory_reconcile.py:214-255`：`pointers` / `assets` / `observations` / `bundles` / `bundle_summaries` / `errors` / `delta_ids: []` / `bulk_download_performed: False`）、`MANIFEST_INGESTION`（sidecar + Evidence + 问题 + sha256 + dialect_id + filters）。

**验算过的事实**：32 个 pair unit 中 **29 个至少满足一个 `BUNDLE_SCHEMAS`**；未满足的是 `GSE234788_SHARED_METADATA`（纯 metadata，正确）与 Zenodo 归档（正确）。25 个满足 `multiome_same_cell`；4 个只满足 `matched_scrna_scatac`（`GM12878_multiome`、`KID_S2`、`KID_S4_1`、`KID_S4_2`）是因为确实缺 cell-axis 文件。

---

### S3 — ManifestRunSupervisor 图 + C3 修复

新建 `src/scdata_agent/application/orchestration_manifest.py`（~90 行）。**用子类，不改图**：`orchestration_v2.py` 是冻结接缝，且图无条件跑 `initialize_run → research_planner → planning_gate → resource_discovery`（`:304-311`）并覆写 `state["discovery"]`，所以 CLI 预先播种 artifact 的方案不可行。

```python
class ManifestRunSupervisor(MultiAgentSupervisor):
    """A curated manifest enters the same downstream path as a discovered one."""
    def _build_graph(self, saver):
        # initialize_run → manifest_ingest → {preflight_assessment ∥ early_lineage}
        #   → acquisition_strategy → download_approval →(cond)→ transfer_execution
        #   → archive_expansion → dataset_curator → materialized_quality
        #   → late_lineage → corpus_governance → corpus_approval → finalize
```

全部原样继承：`execute`(`:143`)、`resume_checkpoint`(`:173`)、`graph_state`(`:202`)、`_dispatch`(`:574`)、`_context`(`:546`)、`_specialist`(`:463`)、`_download_approval`(`:472`)、`_route_after_download`(`:488`)、`_open_download_checkpoint`(`:667`)、`_corpus_approval`(`:495`)、`_open_corpus_checkpoint`(`:752`)、`_finalize`(`:534`)。两道人工门原封不动，不可协商项 #1 自动成立。

**`SupervisorState` 无需新 key**：摄取节点返回 `{"requirement": ..., "discovery": ...}`，两个 key 都已存在（`:87,90`）且 `_context`(`:556-568`) 会重新水合。manifest 配置走 `run.policy`（`manifest_path` / `manifest_dialect` / `manifest_filters` / `downloader` / `profile` / `contact_email` / `honour_manifest_layout` / `source="manifest"`），`_initialize_run`(`:361-369`) 已经在传。

**Resume 分派**：`resume_checkpoint` 会重新编译图，必须编译当初那张。加：

```python
def supervisor_for_run(ledger, database_path, run_id, *, trace_mode=None):
    """Resume rebuilds the graph, so it must rebuild the one the run started on."""
    policy = ledger.get_run(run_id).policy
    cls = ManifestRunSupervisor if policy.get("source") == "manifest" else MultiAgentSupervisor
    return cls(ledger, database_path, trace_mode=trace_mode)
```

`cli.py` 的 `resume` 现在只收 `checkpoint_id`，需经 ledger 反查 run（可能要加 `CollaborationLedger.get_checkpoint()`）；`interfaces/api.py:64` 同理（supervisor 目前在模块级构造，对多 run 服务本来就是隐患）。

**C3 修复**（`acquisition_v2.py:55-63`）—— 只在 family **跨 candidate** 时才做代表压制：

```python
# 一个 candidate 下的多个 bundle 是多个样本，不是同一数据集被看到两次；
# 折叠它们等于拒绝获取除一个样本外的所有样本。
family_candidates = {f.family_id: set(f.candidate_ids) for f in lineage.families}
key = (family_id if family_id and len(family_candidates.get(family_id, ())) > 1
       else f"bundle:{bundle.bundle_id}")
```

以及 `progressive_lineage_v2.py:145` 把 `redundancy_status` / `related_bundle_candidates` 的依据从 `len(family.bundle_ids) > 1` 改为 `len(family.candidate_ids) > 1`。对现存所有 run 都是 no-op。

**§7.1 归档警告修复**（`progressive_preflight.py:240-245`）：tonsil bundle 有 2 个 asset（`donor_metadata` + `multiome_archive`），`archive_only = len(archive_assets) == len(bundle_assets)` 为 False，`:259-262` 发出 `bundle:missing_roles` —— 对没人打开过的内容下判决，恰是不可协商项 #3 要防的。改为：

```python
archive_may_explain_gap = bool(archive_assets) and not bundle.satisfied_schema_ids
```

新增 CLI `manifest-run`（选项见 §8）。

---

### S4 — 解除 governance 阻塞 →【里程碑 0】

**`application/agents/snapshot_governance.py:115-138`**：

```python
elif manifest is None or manifest.status != MaterializationStatus.MATERIALIZED:
    pool = "quarantine"; reasons.append("standard_package_incomplete")
# A native mtx/tsv.gz/fragments package with a manifest, a data card,
# provenance and passing checksums is a legitimate training input.
# Requiring AnnData here made format the admission gate and quality an
# afterthought; format is now a dimension the snapshot records.
elif manifest.standard_object_path is None and context.policy.get(
    "require_standard_object", False
):
    pool = "quarantine"; reasons.append("anndata_or_mudata_missing")
...
else:
    pool = "pretrain"; reasons.append("hard_gates_passed")
    if manifest.standard_object_path is None:
        reasons.append("standard_object:absent")   # 随快照走，不被丢掉
```

`require_standard_object` 默认 **False**；旧理由串在 opt-in 路径保留。

**`infrastructure/materialization.py` 四处**（全在 `MaterializedQualityInspector.inspect` 内）：

1. `:225-229` — 删掉 `elif not manifest.standard_object_path:` → WARN 分支。
2. `:230-233` — 收窄为只对 WARN 生效；`UNKNOWN` 改为把 key 追加进 `unavailable`。注释写清 why：跑不了的检查是知识的边界，不是质量问题（#3）。
3. `:193-196` — 仅当 `standard_object_path` 存在时才发 `standard_object_hdf5`，否则 `unavailable.append("standard_object")`。
4. `:183-186` — fragment 采样器按 role 门控（**C2**）：`if manifest.source_asset_roles.get(receipt.asset_id) == DataRole.FRAGMENTS:`；并把 `:178-182` 的 1 MiB gzip 嗅探换成全流检查。

`_write_data_card`(`:338-366`) 与 `provenance`(`:99`) 继续报告 `standard_object: not available` —— 卡片该说明缺什么，不是藏起来。

**会坏的测试只有一个**：`tests/test_lifecycle_v2.py` 约 286 行 `assert report.status.value == "warn"` → `"pass"`，并新增 `"standard_object" in report.unavailable_metrics`。
`tests/test_governance_v2.py` **不坏**（三个用例都设了该字段或更早在 licence/materialization 处失败）。
`tests/test_supervisor_v2.py::test_supervisor_requires_download_then_corpus_approval` 行为静默改变但断言仍过 —— **要主动加 `assert snapshot["admitted_bundle_ids"]`**，这是本轮的全部意义，应钉在既有测试里。

#### 里程碑 0

子集 `GSE178707_NEATseq / CD4_memory_lane2_GSM5396336__GSM5396337`，5 文件 0.338 GB：

```
GSM5396336_lane2_atac_fragments.tsv.gz   260 MB   fragment              → FRAGMENTS
GSM5396337_lane2_matrix.mtx.gz            83 MB   filtered_mtx_matrix   → RNA_MATRIX
GSM5396337_lane2_features.tsv.gz         2.5 MB   filtered_mtx_features → OTHER
GSM5396337_lane2_barcodes.tsv.gz          27 KB   filtered_mtx_barcodes → CELL_METADATA
GSM5396332_CD4cells.csv.gz               519 KB   cell_metadata         → CELL_METADATA
```

**在 S4 结束时达成 —— 在 aria2 存在之前。** 338 MB 走现有 httpx 执行器完全够；aria2 是给 13.7 GB kidney 文件的吞吐修复，不是正确性前提。这刻意反转了「aria2 优先」的直觉：第一批真实字节应该走最简单的路径落地，出问题时你才知道那不是下载器的锅。

---

### S5 — aria2 后端 + 下载状态机

新建 `infrastructure/transfer_backend.py`：`TransferBackend` Protocol（现有 `TransferExecutor` 已逐字符匹配该签名，零改动即满足）+ `select_transfer_backend(policy)`（`policy["downloader"] ∈ {auto,aria2,httpx}`，`shutil.which(os.environ.get("SCDATA_AGENT_ARIA2_BIN","aria2c"))`，显式要 aria2 而不存在时报错）。把共享前置拒绝（`transfer_executor.py:54-70`：未知大小/超预算/磁盘不足）提成 `prepare_transfer(...)`。`agents/transfer_execution.py:73` 改为 `select_transfer_backend(context.policy)`，receipts artifact 加 `"downloader": backend.name`。

新建 `infrastructure/aria2_executor.py`。队列/session/log 放 `<data_root>/.aria2/<run_id>/<bucket>.{aria2.txt,session,log}`。分桶（`geo_large` ≥0.5 GB / `geo_small` / `other_direct`）与 `PROFILES`（`stable(2×1,4,1) | balanced(4×4,12,4) | perfile(2×8,12,4) | aggressive(6×4,20,4)`）从原型 `:333`、`:76-82` 移植；参数集从 `aria_common_args`(`:366`) 逐字移植，**特别是 `--max-file-not-found=2`**（这才是阻止 404 被重试 15 次的东西）。

队列文件格式（原型 `:322`）：

```
https://ftp.ncbi.nlm.nih.gov/geo/samples/GSM5396nnn/GSM5396336/suppl/GSM5396336_lane2_atac_fragments.tsv.gz
  dir=/abs/data/raw/GSE178707_NEATseq/CD4_memory_lane2_.../03_atac_fragments
  out=GSM5396336_lane2_atac_fragments.tsv.gz
  checksum=md5=<32 hex>          # 仅当 manifest 声明了 MD5
```

**回填 `TransferReceipt` 是难点**（aria2 不产逐文件 JSON）。三选一：`--on-download-complete` hook 需运行时写可执行脚本且让每个测试起真进程 —— 否决；session 文件只记录仍未完成项且成功时不存在 —— 仅诊断价值；**选逐文件 stat + checksum 对账**（因为队列是我们生成的，`(asset_id → 绝对路径, 期望大小, 声称 md5)` 三元组已知）：

```
文件不存在              → FAILED "aria2 produced no file"
存在 .aria2 控制文件    → FAILED "control file present; transfer incomplete"
零字节                  → FAILED
否则                    → COMPLETED
bytes_written = stat().st_size（永远真实）
resumed = 建队列时快照到的 .aria2 是否已存在
aria2 退出码只作 run 级元数据（exit 3 = 有一个文件 404，对其余 40 个毫无意义）
```

**`NCBI_CONTACT_EMAIL` 未设时不中止**。原型的 `raise SystemExit`(`:566`) 对一次性脚本正确、对库错误 —— 它会在人已经同意之后杀掉 run。改为：User-Agent 退回 `scdata-agent/0.1.0`，记 `contact_email_unset` 问题，并**把提示追加到 `_open_download_checkpoint` 的 `recommendation` 文本**，让人在同意**之前**看到。`.env.example` 补 `NCBI_CONTACT_EMAIL=`。

**磁盘布局作为策略**：`policy["honour_manifest_layout"]`（manifest run 默认开）用 sidecar 的 `relative_path`（`<dataset_id>/<pair_unit_id>/<NN_stage>/<filename>`，NN_stage ∈ `{00_manifest, 01_metadata, 02_rna_raw_counts, 03_atac_fragments, 05_auxiliary, 99_raw_optional}`），否则用 bundle 布局。用加固版 `_safe_relative_path` 守卫（拒绝绝对路径、任何 `..` 分量、盘符、`(root/rel).resolve()` 越界）—— 是 `transfer_executor.py:127-136` basename 守卫的严格推广。

**离线可测**：`Aria2Runner` Protocol + `SubprocessAria2Runner`；测试注入 `FakeAria2Runner`，它**解析交给它的队列文件**并把指定字节写到指定路径后返回 exit 0 —— 真正验证了最容易写错的队列生成，且不起进程。

**状态机** `domain/transfer_state.py`（纯）：

```python
class FileTransferStatus(StrEnum):
    PENDING; DOWNLOADING; COMPLETED; RETRYABLE
    MANIFEST_ERROR; RATE_LIMITED; VALIDATED; VALIDATION_FAILED

ALLOWED_FILE_TRANSITIONS = {
    PENDING: {DOWNLOADING},
    DOWNLOADING: {COMPLETED, RETRYABLE, MANIFEST_ERROR, RATE_LIMITED},
    RETRYABLE: {DOWNLOADING},
    RATE_LIMITED: {DOWNLOADING},
    MANIFEST_ERROR: set(),               # 终态：去审计 manifest
    COMPLETED: {VALIDATED, VALIDATION_FAILED},
    VALIDATION_FAILED: {DOWNLOADING},    # 损坏文件可重取
    VALIDATED: set(),
}

class FailureClass(StrEnum):
    RETRYABLE; MANIFEST_ERROR_404; RATE_OR_ACCESS_LIMIT
    SERVER_TRANSIENT; NETWORK_ERROR; UNKNOWN

RETRYABLE_CLASSES = frozenset({RETRYABLE, SERVER_TRANSIENT, NETWORK_ERROR})   # 原型 :641
def classify_http_outcome(status, transport_error) -> FailureClass: ...
```

`MANIFEST_ERROR` **是终态** —— 原型的 `classify_probe`(`:456`) 把它排除在重试门外但无人强制，状态机来强制才是持久版本。

`infrastructure/persistence/download_store.py`：`DownloadStateStore`，自建 `CREATE TABLE IF NOT EXISTS download_file_state`，与 catalog 同一个 SQLite 文件，**`catalog.py` 零改动**。每次 transition 同时 `catalog.append(record_type="file_transfer_record", ...)`。

`infrastructure/download_triage.py`：用 httpx 而非 shell 调 curl（同语义、可用 `MockTransport` 离线测、不新增可执行依赖），`probe_and_classify(client, url)` 只发 `Range: bytes=0-0`。

`manifest-repair <run-id> [--run]`：

1. **除非该 run 的 `download_selection` checkpoint 已 RESOLVED 且选了下载选项，否则拒绝执行** —— 修复路径不得绕过同意门。这是整条命令最重要的一行。
2. 对非 `{COMPLETED, VALIDATED}` 的记录探测并重分类。
3. 打印分类直方图。
4. 只从 `RETRYABLE_CLASSES` 构造重试集。
5. `MANIFEST_ERROR_404` 进单独的 `manifest_audit_required` 输出表，**永不重试**，说明原因：*manifest 里的文件名、accession 或 URL 错了，或文件已撤回 —— 该修的是 manifest，不是下载器。*

---

### S6 — 验证 / QC 层

原型 `:682-764` 那批函数引用透明但**要开文件**，属 I/O，放 `infrastructure/file_validation.py`。

| 原型                                      | 新家                                                                                 | 路上的改动                                                                                                                                                                                 |
| ----------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `gzip_test` `:704`                    | `gzip_integrity(path, max_bytes=None)`                                             | 返回`GzipIntegrity(ok, bytes_read, truncated, error)`；**默认全流**，受 `policy["qc_max_decompressed_bytes"]` 封顶；封顶时 `truncated=True` 且检查判 `UNKNOWN` 而非 `PASS` |
| `count_gzip_lines` `:713`             | `count_lines(path)`                                                                | 同时处理明文与 gzip                                                                                                                                                                        |
| `mex_dimensions` `:720`               | `mex_header(path) -> MexHeader \| None`                                             | 逻辑不变                                                                                                                                                                                   |
| `fragment_sample_check` `:732`        | `sample_fragments(path, limit) -> FragmentSample(rows, bad_rows, barcodes, error)` | **返回而不抛**                                                                                                                                                                       |
| `md5_file`/`sha256_file` `:687-701` | `file_digest(path, algorithm)`                                                     | 复用`transfer_executor.py:232` 的 `_file_digest`，搬来此处                                                                                                                             |
| `validate_mex_pairs` `:767`           | `qc_metrics.mex_consistency(...)`                                                  | 按`(dataset_id, pair_unit_id)` 分组来自 sidecar 而非 CSV                                                                                                                                 |

**`materialization.py:250 _sample_fragments` 用替换而非复用**：它遇到单行畸形就抛，`:187-189` 把异常转成整个 bundle 的 `readability=FAIL` —— 13 GB fragment 文件里一行坏数据隔离整个样本。替换版把 `bad_rows` 作为 metric 返回，`bad_rows==0` → PASS，`>0` → WARN，**不发明任何阈值**。

**MEX 维度**：按 `(dataset_id, pair_unit_id)` 分组，多 lane 时按文件名 stem 匹配（原型 `:791-795`，`KID_S4_2` 有两个 lane 需要它）。三件套不全或文件不可读时判 `UNKNOWN` 而非 `FAIL` —— 缺伴随文件不是维度不匹配。（原型 `cmd_validate` 的失败重复计数 `:848-874` 不要移植。）

**RNA–ATAC barcode 交集，无阈值**（设计文档 §8.4）：输出 `n_atac_barcodes` / `n_rna_barcodes` / `n_intersection` / `frac_atac_in_rna` / `frac_rna_in_atac` / `barcode_normalization_rule` / `barcode_rna_matrix_kind`（raw|filtered，来自原生角色）。依次尝试各归一化规则（`none` / `strip_lane_prefix` / `strip_gem_suffix`），报告交集最大的那条并点名。判定：某规则下 `n_intersection > 0` → PASS；任一侧读不出 → UNKNOWN；两侧都干净读出但**没有任何规则产生共享标识符** → WARN。零重叠不是阈值，是「这两个文件没有共享任何标识符」这一真实发现；§8.4 反对的是固定**比例**截断，这里没有。

**挂载不动 `lifecycle.py`**：`MaterializedQCReport.structural_checks` 与 `.metrics`（`lifecycle.py:356-357`）是开放字典，新检查按 key 塞入；跑不了的检查进 `.unavailable_metrics`。零 schema 变更。

一条诚实性待办：`checks["genome_build_consistency"]`(`qc_metrics.py:35-40`) 源自 `DataAsset.genome_build`，在 manifest run 里来自人的表格，却落在 `evidence_class` 默认 `RECOMPUTED` 的报告里。移进 `metrics["declared_genome_builds"]` 或保留检查但在 `reasons` 里点明来源。

---

### S7 — 归档处理

新建 `infrastructure/archive_inspection.py`：

```python
class ArchiveMember(StrictModel):
    name: str; size_bytes: int | None; is_dir: bool; is_link: bool
class ArchiveListing(StrictModel):
    archive_path: str; format: str; member_count: int
    members: list[ArchiveMember]; truncated: bool; error: str | None

def list_archive(path, *, max_members=50_000) -> ArchiveListing
def safe_extract(path, destination, *, max_extracted_bytes, max_expansion_ratio=100.0)
```

新的 `ArchiveExpansionAgent` 节点插在 **`transfer_execution` 与 `dataset_curator` 之间**（字节已落地 → 同意门之后；角色判定之前）。找出 role 为 `ARCHIVE` 的 receipt，**列出**（默认；解压需 `policy["extract_archives"]` 显式打开），对每个成员名跑 `infer_data_role`，产出二级 manifest（`AssetLocation(url=f"archive://{receipt.local_path}!{member.name}", protocol=MANUAL)`）。

**证据类别**：列表本身是 `FILE_OBSERVED`（读 zip 中央目录 / 流式读 tar header 读的是我们下载的本地字节），成员的角色推断是 `MODEL_INFERRED`。把这两者分开就是全部纪律。本轮**不重算 `satisfied_schema_ids`、不改变准入** —— 升为一等 asset 意味着第二轮获取（它们在文件里，不在 URL 上），那是 P1.2 本身。Data Card 上直说：*"19.6 GB archive listed, not opened; N members, roles inferred from names."*

**解压守卫**：tar 用 Python 3.12 自带的 `tar.extractall(path=dest, filter="data")`（已拒绝绝对路径、`..` 逃逸、符号链接、硬链接、设备节点、setuid —— 它存在就是为了这个，不要重新实现）；zip 无等价物，逐成员手工校验（绝对路径、`..` 分量、盘符、`(dest/name).resolve()` 越界、重名）；两者都在 `sum(member.size) > policy["max_extracted_bytes"]` 或膨胀比 > `max_expansion_ratio`（默认 100）时拒绝，并复用 `shutil.disk_usage` 检查剩余空间。拒绝写进 `MaterializationManifest.errors`，绝不用异常杀掉 run。

**Licence（C5）**：不加 Zenodo 通用策略。两条诚实路线：(a) 启用已在 `CANONICAL_FIELDS` 里预留的 `license_id` / `license_url` 方言列 → `DataAsset.license_id`，`progressive_preflight.py:196-199` 在命中 `--approved-license` 时升为 `APPROVED`（可泛化的修法）；(b) 用现有 `approved_license_candidate_ids` policy（`progressive_preflight.py:85-87`）。在任一被使用之前，该 bundle 以 `license_unknown` 隔离 —— 这是正确的默认。

---

### S8 — 收尾

- `.env.example` 补 `NCBI_CONTACT_EMAIL=`
- `pyproject.toml` 加 `[project.optional-dependencies] manifest = ["openpyxl>=3.1,<4"]`（**S1 已建接缝但依赖尚未声明**）
- `docs/roadmap.md` + `docs/roadmap.zh-CN.md` 同一 commit：里程碑 0 同时满足 **P0.3 / P0.4 / P0.5**，并让 **P0.2 失效** —— 把那一行从「为 10x layout 写转换器」改写成「原生格式包可准入；AnnData 转换是 P1 便利项」，并重写其后「Why P0.2 is the hard blocker」一节（现在点名 `snapshot_governance.py:118` 为阻塞点）
- `docs/journal.md`、`docs/commands.md`

---

## 6. CLI 目标形态

```
scdata-agent manifest-check MANIFEST                    # ✅ 已完成
    --dialect / --dataset / --pair-unit / --stage (可重复) / --json

scdata-agent manifest-run MANIFEST                      # S3
    --dialect / --dataset / --pair-unit / --stage
    --direct-only / --include-portal                     [默认 --direct-only]
    --database / --data-root / --max-download-bytes
    --downloader [auto|aria2|httpx]                      [默认 auto]
    --profile [stable|balanced|perfile|aggressive]       [默认 balanced]
    --contact-email                                      [默认 $NCBI_CONTACT_EMAIL]
    --remote-probe / --no-remote-probe                   [默认 --remote-probe]
    --manifest-layout / --bundle-layout                  [默认 --manifest-layout]
    --require-standard-object / --allow-native-package   [默认 --allow-native-package]
    --approved-license-candidate (可重复) / --trace-mode

scdata-agent manifest-status   RUN_ID                   # S5
scdata-agent manifest-repair   RUN_ID [--run]           # S5
scdata-agent manifest-validate RUN_ID [--sha256]        # S6
```

**不新增 resume 命令**：`scdata-agent resume <checkpoint-id> --option recommended` 在 `supervisor_for_run` 按 `run.policy` 选图后原样可用。

**刻意不移植**：`benchmark`(`:531`)（对活服务器起 curl 子进程，离线不可验证，`--profile` + 文档化默认覆盖其 95% 价值）；`resolve-10x`(`:947`)（Playwright 抓 click-through 门户，属不可协商项 #4）；`biostudies`(`:1005`)（应按 CONTRIBUTING「Adding a data source」流程做成 `infrastructure/sources/` 里的正式适配器并注册 licence 策略）。

---

## 7. 剩余测试计划

固定装置 `tests/data/manifest_paired_multiome_sample.csv` **已建好**（15 行真实数据），下面的测试都基于它。

| 文件                                  | 状态     | 钉住                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_manifest_dialect.py`          | ✅       | 方言识别、必需列、XLSX 错误串、`parse_size`                                                                                                                                                                                                                                                                                                     |
| `test_geo_static_url.py`            | ✅       | 真实 accession 的精确 URL、bucket、编码、拒绝                                                                                                                                                                                                                                                                                                     |
| `test_manifest_role_mapping.py`     | ✅       | 20 角色全映射、未知 → ERROR、DOGMA 行 manifest 胜出、`.tbi` 不是 fragments                                                                                                                                                                                                                                                                     |
| `test_manifest_ingestion.py`        | S2       | 5 pair unit → 5 bundle；NEATseq 满足`multiome_same_cell`、kidney `SHARED_METADATA` 一个不满足；探测关闭时每个 asset 都是 `USER_CORRECTED` + `DECLARED`（绝不 `OBSERVED`）；size/MD5 落在 `AssetLocation` 且 `probe_method="curated_manifest"`；portal 行是 `MANUAL`；`file_name` 是纯 basename                                 |
| `test_manifest_graph.py`            | S3       | 端到端离线（仿`tests/test_supervisor_v2.py`）：到 `download_selection` 中断 → **resume 前无 `TRANSFER_RECEIPTS`**（同意门）→ `resume(recommended)` → receipts/manifest/QC/snapshot → `resume(approve_snapshot)` → `admitted_bundle_ids` 非空。并断言同一 dataset 的多个 bundle **全部**是 `recommended`（钉住 C3） |
| `test_governance_native_package.py` | S4       | 原生包（fragments + matrix + barcodes，无 h5ad）达`pool=="pretrain"` 且 `reasons` 含 `standard_object:absent`；`policy["require_standard_object"]=True` 时以 `anndata_or_mudata_missing` 隔离                                                                                                                                           |
| `test_aria2_queue.py`               | S5       | 两条真实行的队列文件文本逐字节比对（`checksum=md5=` 只出现在真有 MD5 的 Zenodo 行）；argv 含调优参数与联系邮箱、未设时干净省略；`shutil.which` 被 monkeypatch 成 `None` 时 `select_transfer_backend` 选 httpx；`FakeAria2Runner` 按队列写文件 → `COMPLETED`，留下 `.aria2` → `FAILED`                                           |
| `test_download_state.py`            | S5       | 合法/非法状态转移；`classify_http_outcome(404,None) == MANIFEST_ERROR_404` 且不在 `RETRYABLE_CLASSES`；403/429 → `RATE_OR_ACCESS_LIMIT`；503 → `SERVER_TRANSIENT`；store 往返；`manifest-repair` 在下载 checkpoint 未 resolved 时拒绝执行                                                                                             |
| `test_file_validation.py`           | S6       | 截断 gzip →`ok is False`；MEX 头 `32738 2700 286` 对 2700 行 barcodes / 32738 行 features → PASS，不匹配时报出四个精确 `n_*`；3 列 fragment 行 → `bad_rows==1` **且不抛异常**；barcode 重叠输出 §8.4 全部六个字段且无阈值；**`.tbi` 命名文件永不进入 fragment 采样器**（钉住 C2）                                       |
| `test_archive_inspection.py`        | S7       | 含`../evil` 与绝对路径成员的 zip 被拒；tar 符号链接成员被拒；1000:1 膨胀比被拒；良性 zip 列出成员及推断角色；`ARCHIVE` + `DONOR_METADATA` 的 bundle 警告 `archive:uninspected` 而非 `bundle:missing_roles`                                                                                                                              |
| 编辑`test_lifecycle_v2.py`          | S4       | `"warn"` → `"pass"`，加 `"standard_object" in report.unavailable_metrics`                                                                                                                                                                                                                                                                  |
| 编辑`test_supervisor_v2.py`         | S4       | 加`assert snapshot["admitted_bundle_ids"]`                                                                                                                                                                                                                                                                                                      |
| 编辑`test_architecture.py`          | S2/S3/S7 | import 循环补上`manifest_ingest`、`archive_expansion`                                                                                                                                                                                                                                                                                         |

---

## 8. 端到端验证（里程碑 0 达成后）

```bash
export P=/data/home/yfu/.local/share/mamba/envs/scdata-agent/bin
$P/python -m pytest                                  # 全绿，全离线
$P/python -m pytest tests/test_architecture.py       # 分层边界

# 判断变更的证明（预期零 delta，但必须实跑）
$P/python scripts/eval_discovery.py --out var/scorecard_mine.json
$P/python scripts/compare_scorecards.py eval/baseline_scorecard.json var/scorecard_mine.json

# 里程碑 0 端到端（唯一会碰网络的一步）
export NCBI_CONTACT_EMAIL="<你的邮箱>"
M=/data/collab/ai-multiomics/yfu/02_agent/04_thinking/paired_data/required_files_manifest.csv
scdata-agent manifest-check $M --dataset GSE178707_NEATseq
scdata-agent manifest-run   $M --dataset GSE178707_NEATseq \
    --pair-unit CD4_memory_lane2_GSM5396336__GSM5396337 \
    --data-root var/milestone0/data --database var/milestone0/agent_run.sqlite
scdata-agent resume <download-checkpoint-id> --option recommended -d var/milestone0/agent_run.sqlite
scdata-agent resume <corpus-checkpoint-id>   --option approve_snapshot -d var/milestone0/agent_run.sqlite

ls var/milestone0/data/curated/GSE178707_NEATseq/*/   # manifest.json DATA_CARD.md provenance.json
```

**关键人工核对项**（自动断言替代不了）：

1. `DATA_CARD.md` 里 licence 基础必须写着 repository_policy 并点名 NCBI 政策
2. `provenance.json` 里每条 asset 的证据类别必须是 `USER_CORRECTED` 而非 `FILE_OBSERVED`
3. QC 报告里 barcode 交集的六个字段必须都在，且 `barcode_rna_matrix_kind` 正确标着 `filtered`

---

## 9. 给接手者的三条提醒

1. **不要把 aria2 提到里程碑 0 之前。** 338 MB 走 httpx 完全够。第一批真实字节应该走最简单的路径落地，这样出问题时你知道那不是下载器的锅。
2. **不要为了省事去改冻结接缝。** `domain/models.py` / `domain/lifecycle.py` / `orchestration_v2.py` 全程零改动是这个方案的一个性质，不是巧合 —— 唯一考虑过的一次（为 17 行 MEX `*_features` 加 `DataRole.FEATURE_ANNOTATION`）在零准入收益下被推迟了。
3. **不可协商项 #2 在 manifest 场景下最容易被违反。** 人在表格里写 "VERIFIED" 的意思是「我确认这个 URL 能解析」，不是「我读过 licence」也不是「我看过文件内容」。摄取层必须在代码里强制这条边界，而不是靠注释。
