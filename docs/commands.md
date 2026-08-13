# Reusable commands

Commands assume the repository root unless noted otherwise.

## Environment

Create and activate the declared environment:

```bash
mamba env create -f environment.yml
mamba activate celllore
```

Update an existing environment:

```bash
mamba env update -n celllore -f environment.yml --prune
```

Install runtime, development and optional curation dependencies:

```bash
python -m pip install -e .
python -m pip install -e '.[dev]'
python -m pip install -e '.[dev,curation]'
```

## Configuration

Create a local model configuration and validate it without a billable request:

```bash
cp .env.example .env
$EDITOR .env
celllore llm-check
```

Send one small provider request when an end-to-end connectivity check is required:

```bash
celllore llm-ping
```

Identify discovery and transfer requests to NCBI:

```bash
export NCBI_EMAIL='you@example.org'
export NCBI_CONTACT_EMAIL='you@example.org'
```

## Control room

Start the packaged FastAPI/React interface:

```bash
celllore serve \
  --database var/celllore.sqlite \
  --port 8000
```

Open <http://127.0.0.1:8000>.

## Research planning

Compile a model-backed research contract:

```bash
celllore compile \
  'public human PBMC same-cell RNA and ATAC data' \
  --json
```

Inspect deterministic compilation during tests or offline debugging:

```bash
celllore compile \
  'public human PBMC same-cell RNA and ATAC data' \
  --no-llm --json
```

## Curation workflow

Start the approval-gated supervisor:

```bash
celllore agent-run \
  'find public human PBMC datasets with same-cell RNA and ATAC measurements' \
  --database var/celllore.sqlite \
  --data-root data \
  --max-download-bytes 500000000000 \
  --remote-probe
```

Use a bounded quick planning mode:

```bash
celllore agent-run 'research question' \
  --planner-mode fast \
  --database var/celllore.sqlite
```

Raise the per-repository file enumeration limit:

```bash
celllore agent-run 'research question' \
  --max-assets 5000 \
  --database var/celllore.sqlite
```

Exclude Evaluation Vault families and allow only named licence IDs:

```bash
celllore agent-run 'research question' \
  --evaluation-id GSE_EXCLUDED_FROM_TRAINING \
  --approved-license cc-by-4.0 \
  --database var/celllore.sqlite
```

Require a declared, allowlisted licence:

```bash
celllore agent-run 'research question' \
  --strict-license \
  --database var/celllore.sqlite
```

Permit unresolved per-record licence evidence only for publicly accessible Bundles:

```bash
celllore agent-run 'research question' \
  --allow-unknown-license \
  --database var/celllore.sqlite
```

Inspect persisted workflow state:

```bash
celllore graph-state RUN_ID \
  --database var/celllore.sqlite
```

## Checkpoints

Keep the acquisition plan without downloading:

```bash
celllore resume CHECKPOINT_ID \
  --option plan_only \
  --database var/celllore.sqlite
```

Download all recommended Bundles:

```bash
celllore resume CHECKPOINT_ID \
  --option recommended \
  --database var/celllore.sqlite
```

Approve only selected recommended Bundles:

```bash
celllore resume CHECKPOINT_ID \
  --bundle-id BUNDLE_A \
  --bundle-id BUNDLE_B \
  --database var/celllore.sqlite
```

Include eligible manual-review Bundles whose sizes are unknown:

```bash
celllore resume CHECKPOINT_ID \
  --option all_with_unknown_size \
  --database var/celllore.sqlite
```

Resolve the final Corpus Snapshot checkpoint:

```bash
celllore resume CHECKPOINT_ID \
  --option approve_snapshot \
  --database var/celllore.sqlite

celllore resume CHECKPOINT_ID \
  --option reject_snapshot \
  --database var/celllore.sqlite
```

## Curated manifests

Audit a curated manifest without creating a run or using the network:

```bash
celllore manifest-check path/to/manifest.xlsx
```

Run a curated manifest through the downstream approval-gated workflow:

```bash
celllore manifest-run path/to/manifest.xlsx \
  --database var/celllore.sqlite \
  --data-root data
```

Inspect transfer state and classify safe repairs:

```bash
celllore manifest-status RUN_ID \
  --database var/celllore.sqlite

celllore manifest-repair RUN_ID \
  --database var/celllore.sqlite
```

Use command-specific `--help` before executing a repair action.

## Catalog API

Read evidence claims and approved snapshots from a running server:

```bash
curl 'http://127.0.0.1:8000/catalog/records?record_type=evidence_claim'
curl 'http://127.0.0.1:8000/corpus-snapshots?approved_only=true'
```

Download a run catalog:

```bash
curl -OJ \
  'http://127.0.0.1:8000/runs/RUN_ID/curation-catalog.xlsx'
```

## Broad discovery utility

Build the broad scATAC and paired multi-omics discovery catalog:

```bash
celllore corpus-discover \
  --database var/scatac_corpus.sqlite \
  --max-results-per-query 200
```

Run a small source smoke test first:

```bash
celllore corpus-discover \
  --database var/scatac_corpus_smoke.sqlite \
  --max-results-per-query 3
```

## Tests and evaluation

Run static checks and the complete offline test suite:

```bash
ruff check src tests scripts
pytest
```

Run the release scorecard:

```bash
python scripts/eval_discovery.py --out var/scorecard.json
python scripts/compare_scorecards.py \
  eval/baseline_scorecard.json var/scorecard.json
```

Run focused architectural and research-contract tests:

```bash
pytest tests/test_architecture.py
pytest tests/test_requirements.py tests/test_llm_requirements.py
```

## Frontend

Build the packaged production interface:

```bash
cd frontend
npm ci
npm run build
cd ..
git diff --exit-code -- src/celllore/interfaces/static/ui
```

Run the frontend development server alongside `celllore serve`:

```bash
cd frontend
npm run dev
```

## LangGraph Studio and tracing

Run Studio against the local graph:

```bash
langgraph dev --no-reload --no-browser \
  --config langgraph.json \
  --host 127.0.0.1 \
  --port 2025
```

Opt in to LangSmith tracing:

```bash
SCDATA_AGENT_TRACE_MODE=langsmith \
LANGSMITH_TRACING=true \
LANGSMITH_API_KEY=... \
LANGSMITH_PROJECT=celllore \
celllore agent-run 'research question'
```

## Release verification

Build and validate Python artifacts:

```bash
python -m pip install build twine
python -m build
python -m twine check dist/*
```

Run the complete archive verification:

```bash
ruff check src tests scripts
pytest
python scripts/eval_discovery.py --out var/scorecard.json
python scripts/compare_scorecards.py \
  eval/baseline_scorecard.json var/scorecard.json
cd frontend && npm ci && npm run build && cd ..
git diff --exit-code -- src/celllore/interfaces/static/ui
python -m build
python -m twine check dist/*
git diff --check
```
