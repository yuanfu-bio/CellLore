<p align="center">
  <img src="docs/assets/celllore-readme-header.png" alt="CellLore — evidence-first single-cell data discovery and curation" width="100%" />
</p>

<p align="center">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-8B5CF6?style=flat-square" />
  <a href="https://github.com/yuanfu-bio/CellLore/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/yuanfu-bio/CellLore/actions/workflows/tests.yml/badge.svg" /></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img alt="LangGraph" src="https://img.shields.io/badge/orchestration-LangGraph-6657E8?style=flat-square" />
  <img alt="Workflow: 22 nodes" src="https://img.shields.io/badge/workflow-22_nodes-06B6D4?style=flat-square" />
  <img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache--2.0-22C55E?style=flat-square" />
</p>

<p align="center">
  <strong>From a biological question to an auditable, approval-gated, analysis-ready single-cell dataset.</strong>
</p>

<p align="center">
  Search public archives, verify pairing evidence, inspect every file, approve transfers,<br />
  and materialize a reproducible local corpus — without losing the scientific trail.
</p>

<p align="center">
  <a href="#-why-celllore">Why CellLore</a> ·
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-workflow">Workflow</a> ·
  <a href="#-curation-outputs">Outputs</a> ·
  <a href="docs/commands.md">CLI reference</a> ·
  <a href="docs/roadmap.md">Roadmap</a>
</p>

---

## 🧬 Why CellLore

**CellLore** is an evidence-first discovery and curation agent for public single-cell
epigenomics and multimodal data: scATAC-seq, snATAC-seq, RNA + ATAC multiome, and
matched assays.

It does more than search. CellLore turns a natural-language request into an executable
research contract, discovers candidate studies, reconstructs sample and pair-unit
relationships, reconciles remote files, checks download readiness, and materializes a
human-readable production dataset — with a persisted approval gate before transfer.

CellLore is designed for the evidence-heavy work **before** downstream analysis. It
does not replace Scanpy, Seurat, Signac or ArchR; it prepares a traceable local corpus
for those tools.

| Focus | Capability | Recorded result |
|---|---|---|
| Discovery | **Evidence-aware search** | Study accessions, source URLs, publication year, assay and organism evidence |
| Identity | **Pair-unit reconstruction** | Human-readable units such as `Patient3_GSM9196533__GSM9196532` |
| Inventory | **File-level resolution** | Exact URLs, roles, byte sizes, checksums, availability and manifest status |
| Control | **Human approval gates** | No transfer before consent; no corpus publication before governance approval |
| Validation | **Post-transfer QC** | Compression, HDF5, semantic-role, barcode and materialized-object checks |
| Materialization | **Analysis-ready layout** | Stable metadata, RNA, ATAC, pairing, QC and audit directories |
| Provenance | **Append-only audit trail** | Runs, artifacts, evidence classes, decisions and snapshots in SQLite |
| Interface | **Live control room** | React/Vite UI, SSE updates, workflow map, stage timing and downloadable XLSX catalog |

> [!NOTE]
> CellLore is an **alpha research prototype**. The workflow runs end to end against
> live public APIs, but uncertain evidence is deliberately routed to audit or human
> review rather than guessed.

## 🧭 Workflow

The control room renders the supervisor's canonical **22-node LangGraph DAG**.
This compact view matches the live `PROCESS MAP`: planning feeds a branched
discovery path, inventory fans out into parallel QC checks, and the lower two rows
form an approval-gated curation loop back to finalization.

<p align="center">
  <img src="docs/assets/diagram_final.png" alt="CellLore workflow from research planning and discovery through quality control, curation and finalization" width="100%" />
</p>

### Human checkpoints

1. **Download approval** — choose plan-only, recommended bundles, or a bounded subset.
   Transfer is unreachable before this persisted decision is resolved.
2. **Corpus approval** — approve or retain the immutable corpus snapshot as a draft
   after materialized QC and lineage checks.

## ⚡ Quick start

### 1. Clone and install

```bash
git clone https://github.com/yuanfu-bio/CellLore.git
cd CellLore

mamba env create -f environment.yml
mamba activate celllore
python -m pip install -e .
```

`conda` can be used in place of `mamba`; the environment name remains `celllore`.

### 2. Configure a model provider

```bash
cp .env.example .env
$EDITOR .env       # enable one provider block and add its API key
celllore llm-check # validates configuration without making a billable request
```

StepFun is enabled in the template; the same file shows exactly which lines to comment
and uncomment for DeepSeek. Keep the populated `.env` local — it is ignored by Git.

### 3. Launch the control room

```bash
celllore serve --database var/celllore.sqlite --port 8000
```

Open `http://127.0.0.1:8000`, choose a curation focus, and try:

> Find 2 public human PBMC datasets with RNA and ATAC measured in the same cell or nucleus.

The production React UI is bundled with CellLore. A fresh clone does **not** require
`npm install` or a separate frontend build before `celllore serve`.

On a remote machine, forward the same port through your editor or SSH:

```bash
ssh -L 8000:127.0.0.1:8000 user@remote-host
```

### What happens on the first run

1. CellLore turns the question into explicit scientific and evidence requirements.
2. The workflow searches public metadata and reconstructs study, sample and pair units.
3. Candidate files are reconciled and checked before any transfer is allowed.
4. The run pauses for download approval; choosing plan-only transfers no bytes.
5. Approved data are organized under `data/production/` and validated again on disk.

> [!TIP]
> Set `NCBI_EMAIL=you@example.org` before sustained GEO/SRA use. It identifies your
> requests to NCBI and is strongly recommended for responsible archive access.

<details>
<summary><strong>Prefer the CLI? Run the same approval-gated workflow</strong></summary>

Compile a question without a model call:

```bash
celllore compile \
  'public human PBMC data with same-cell scRNA-seq and scATAC-seq' \
  --no-llm
```

Run the pipeline:

```bash
export NCBI_EMAIL='you@example.org'

celllore agent-run \
  'find 2 public human PBMC datasets with RNA and ATAC from the same cell or nucleus' \
  --database var/celllore.sqlite \
  --data-root data \
  --remote-probe
```

When the run reaches `waiting_for_decision`, resume the persisted checkpoint:

```bash
celllore resume CHECKPOINT_ID \
  --option recommended \
  --database var/celllore.sqlite
```

Use `--option plan_only` to retain the plans without downloading bytes. See the
[full CLI reference](docs/commands.md) for planner modes, licence policy, remote
probes, result limits and checkpoint options.

</details>

## 📚 Curation outputs

One run produces several views of the same traceable record:

| Surface | Purpose |
|---|---|
| Control room | Live workflow, human decisions, stage timing and evidence summaries |
| Searchable catalog | Curator-facing study, pair-unit, file and audit tables |
| XLSX export | Portable review artifact with the same four catalog sheets |
| SQLite ledger | Runs, artifacts, decisions, messages and provenance |
| Production tree | Downloaded and standardized data organized for analysis |

### Candidate dataset catalog

Every run exposes four searchable UI sheets and a matching XLSX download:

| Sheet | Purpose |
|---|---|
| `01_Studies` | Study evidence, public year, catalog status and next action |
| `02_Pair units` | Traceable same-cell or same-nucleus RNA–ATAC logical units |
| `03_Files` | Exact file, URL, role, size, manifest and post-download checks |
| `04_Audit / hold` | Blocking reason, audit procedure, pass criteria and release condition |

Internal IDs remain in the ledger for stable joins, while curator-facing tables and
directories use readable identities whenever the evidence supports them.

### Production directory

Materialized bundles follow a consistent, inspection-friendly layout:

```text
data/production/YYYY-MM-DD_RUN_<run_id>/
└── GSE158013/
    └── Multiome_nuclei_W1_GSM5123949/
        ├── 01_metadata/
        ├── 02_rna_raw_counts/
        ├── 03_atac_fragments/
        ├── 04_standardized/
        │   ├── rna/
        │   │   ├── rna_raw_counts.h5ad
        │   │   └── rna_file_manifest.json
        │   ├── atac/
        │   │   ├── fragments.tsv.gz
        │   │   ├── fragments.tsv.gz.tbi
        │   │   └── atac_file_manifest.json
        │   ├── metadata/
        │   │   ├── barcode_metadata.tsv.gz
        │   │   ├── sample_metadata.json
        │   │   └── metadata_sources.json
        │   ├── pairing/
        │   │   ├── barcode_mapping.tsv.gz
        │   │   └── pairing_report.json
        │   ├── qc/
        │   │   ├── process.jsonl
        │   │   ├── validation_report.json
        │   │   ├── validation_summary.tsv.gz
        │   │   └── warnings.log
        │   └── status.json
        ├── 05_auxiliary/
        └── 99_raw_optional/
```

## 🔎 Data source coverage

| Source | Current role | Integration |
|---|---|---|
| NCBI GEO | Primary study discovery, samples and supplementary manifests | Direct |
| NCBI SRA | Linked raw-sequencing records | Direct |
| ENA | Linked FASTQ manifests, checksums and byte sizes | Direct |
| Europe PMC | Publication and open-full-text evidence enrichment | Direct |
| Zenodo / Figshare | Resolution of linked author-deposited assets | Linked assets |
| EGA / dbGaP | Controlled-access study visibility | Metadata-only by design |
| ENCODE / CELLxGENE / HCA / SCP | Curated atlases and reference corpora | Planned |

Unsupported sources are recorded in `QueryPlan.uncovered_sources`; they are never
silently treated as searched.

## 🛡️ Evidence and safety model

CellLore is built around a few enforceable invariants:

- **No bytes before consent.** Transfer execution is gated by a persisted human
  decision.
- **No access circumvention.** Paywall and authentication bypass are out of scope;
  controlled-access studies remain metadata-only.
- **Evidence has provenance classes.** `FILE_OBSERVED` and `RECOMPUTED` claims require
  local bytes and cannot be produced before transfer.
- **Deterministic gates, ranked advice.** Access, modality, pairing and licence gates
  are code; models propose and summarize but do not silently admit data.
- **Append-only audit trail.** Runs, artifacts, decisions, bundle states and corpus
  snapshots are persisted in SQLite.

<details>
<summary><strong>Bundle completeness schemas</strong></summary>

Candidates are evaluated against valid bundle shapes, not against the union of every
modality mentioned in a question.

| Schema | Applies to | Required shape |
|---|---|---|
| `scatac_standalone` | scATAC / snATAC | accessibility signal + cell axis |
| `multiome_same_cell` | 10x Multiome, SNARE-seq, SHARE-seq | accessibility + RNA + per-cell linkage |
| `multiome_trimodal` | TEA-seq / DOGMA-seq | multiome shape + protein matrix |
| `matched_scrna_scatac` | matched but separate assays | accessibility + RNA + matched sample identity |
| `atac_protein` | ASAP-seq / ISSAAC-seq | accessibility + protein + cell axis |
| `raw_reads` | any assay | FASTQ; valid but expensive input |

`matched_scrna_scatac` is intentionally weaker than `multiome_same_cell`: sharing a
sample must never be reported as sharing a cell.

</details>

<details>
<summary><strong>Licence resolution</strong></summary>

| Status | Meaning |
|---|---|
| `restricted` | A declared licence matched the restricted list; always blocks |
| `approved` | A declared licence is allowlisted or explicitly approved |
| `repository_policy` | No record licence exists and all files are covered by recognized archive terms |
| `unknown` | Licence evidence is missing, incomplete or not allowlisted |

`repository_policy` fills only a genuine missing-field gap and never overrides a
licence that was actually observed. Use `--strict-license` to require a declared,
allowlisted licence.

</details>

<details>
<summary><strong>Pairing verification</strong></summary>

Pairing phrases such as *same-cell*, *same-nucleus*, *joint profiling*, `10x Multiome`,
`配对` and `同细胞` compile into a minimum pairing tier:

```text
same-cell ≡ same-nucleus
    > same-sample ≡ same-donor
    > study-level ≡ unverified
```

A shortfall is routed to audit with `pairing:unverified` rather than silently excluded,
because many valid multiome studies do not state “same cell” in their abstract.

</details>

## 🏗️ Architecture

The package follows a strict dependency direction enforced by
`tests/test_architecture.py`:

| Layer | Path | Responsibility |
|---|---|---|
| Domain | `src/celllore/domain` | Typed models, gates, requirements and evidence rules |
| Application | `src/celllore/application` | Agents, handoffs, curation catalog and LangGraph supervisor |
| Infrastructure | `src/celllore/infrastructure` | Public APIs, LLM routing, persistence, transfer and QC |
| Interfaces | `src/celllore/interfaces` | Typer CLI and FastAPI control surface |
| Frontend | `frontend` | React/Vite control room and catalog workspace |

Model routing is provider-neutral. StepFun is enabled in the example by default;
DeepSeek, OpenAI-compatible, OpenRouter, Ollama and Anthropic routes remain available.

| Variable | Purpose |
|---|---|
| `SCDATA_AGENT_MODEL_PROVIDER` | Default model provider |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` | DeepSeek credentials, V4 model ID and endpoint |
| `SCDATA_AGENT_MODEL_{PLANNER,DISCOVERY,REVIEW,CHAT}` | Per-role model overrides |
| `SCDATA_AGENT_REASONING_EFFORT[_ROLE]` | Per-role reasoning effort |
| `SCDATA_AGENT_PLANNER_MODE` | `deep` or `fast` planner mode |
| `SCDATA_AGENT_PLANNER_TIMEOUT_SECONDS` | Overall planner time budget |
| `SCDATA_AGENT_TRACE_MODE` | `local` by default or explicit `langsmith` opt-in |
| `NCBI_EMAIL` / `NCBI_API_KEY` | NCBI E-utilities etiquette and rate limit |

See [`.env.example`](.env.example) for the complete list.

<details>
<summary><strong>Naming and compatibility note</strong></summary>

The product, repository, Python distribution, Conda environment and CLI are named
CellLore / `celllore`. The internal Python import namespace and existing
`SCDATA_AGENT_*` configuration keys remain stable for compatibility with persisted
runs and local deployments.

</details>

## 🧪 Development

```bash
mamba run -n celllore pytest
mamba run -n celllore ruff check src tests
cd frontend && npm ci && npm run build
```

The test suite is offline by default and uses mocked HTTP transports; no API key is
required. The frontend build writes the production bundle into the Python package so
clean clones and wheels serve the same UI. Optional AnnData/MuData inspection support
is available through:

```bash
pip install -e '.[dev,curation]'
```

## ⚠️ Known limitations

- Archive contents such as `GSExxxxx_RAW.tar` cannot be classified before transfer;
  they are marked `archive:uninspected` and routed to review.
- Public archives often provide repository terms rather than a per-record licence;
  CellLore preserves that weaker evidence class instead of calling it an approved
  licence.
- `--max-assets` bounds very large manifests. Truncated inventories are flagged and
  cannot be treated as complete.
- File-role inference recognizes common 10x and archive conventions but intentionally
  leaves unfamiliar names unclassified instead of guessing.
- Several high-value atlas sources remain planned rather than implemented.

See the active [roadmap](docs/roadmap.md), its [Chinese version](docs/roadmap.zh-CN.md),
and the [architecture journal](docs/journal.md).

## 🤝 Contributing

Contributions that improve source coverage, evidence resolution, pair-unit parsing,
post-transfer QC or curator experience are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and keep hard scientific gates deterministic and
test-backed.

## 💬 Support

Open a [GitHub issue](https://github.com/yuanfu-bio/CellLore/issues) for bugs, source
requests or curation-policy discussions. For reproducible reports, include the
CellLore commit, command or UI action, run ID, failed stage and the relevant error —
never include API keys or unpublished data.

## 📄 License

Apache-2.0. See [LICENSE](LICENSE).
