# Reusable commands

Activate the development environment:

```bash
mamba activate scdata-agent
```

Create or update the environment from the project declaration:

```bash
mamba env create -f environment.yml
mamba env update -n scdata-agent -f environment.yml --prune
```

Install the project in editable mode:

```bash
mamba run -n scdata-agent python -m pip install -e '.[dev]'
```

Install optional AnnData/MuData inspection support:

```bash
mamba run -n scdata-agent python -m pip install -e '.[dev,curation]'
```

Run tests:

```bash
mamba run -n scdata-agent pytest
```

Verify the layered package boundaries and entry points (included in the test suite):

```bash
mamba run -n scdata-agent pytest tests/test_architecture.py
```

Configure StepFun without placing the key in source control:

```bash
cp .env.example .env
# Edit .env locally and set STEPFUN_API_KEY to a newly rotated key.
mamba run -n scdata-agent scdata-agent llm-check
```

Send one small billable connectivity request only when desired:

```bash
mamba run -n scdata-agent scdata-agent llm-ping
```

Use StepFun semantic parsing by default, or force the deterministic offline compiler:

```bash
mamba run -n scdata-agent scdata-agent compile \
  'human lung cancer multiome data for annotation' --json
mamba run -n scdata-agent scdata-agent compile \
  'human lung cancer multiome data for annotation' --no-llm --json
```

Compile a research requirement without network access:

```bash
mamba run -n scdata-agent scdata-agent compile \
  '人类肺癌免疫微环境scATAC-seq和multiome数据，用于跨数据集泛化' \
  --json
```

Run live discovery. Set `NCBI_EMAIL` before sustained NCBI API use:

```bash
mamba run -n scdata-agent scdata-agent discover \
  'human lung cancer scATAC-seq and multiome' \
  --max-results 10 \
  --database var/scdata_agent.sqlite \
  --json-output var/latest_run.json
```

Run the standalone legacy ENA FASTQ resolver against a persisted run without downloading files:

```bash
mamba run -n scdata-agent scdata-agent resolve-assets RUN_ID \
  --database var/scdata_agent.sqlite \
  --destination data/raw \
  --json-output var/latest_assets.json
```

BioProject expansion is intentionally opt-in and should always use a bound:

```bash
mamba run -n scdata-agent scdata-agent resolve-assets RUN_ID \
  --candidate-id CANDIDATE_ID \
  --expand-bioproject \
  --max-assets 100 \
  --json-output var/bioproject_assets.json
```

Run the v2 Supervisor. It performs discovery, evidence curation, remote asset
inventory, preflight, early lineage, and acquisition planning, then stops before
any data bytes are downloaded:

```bash
mamba run -n scdata-agent scdata-agent agent-run \
  'human lung cancer scATAC and multiome data for representation learning' \
  --database var/scdata_agent.sqlite \
  --data-root data \
  --max-download-bytes 500000000000 \
  --evaluation-id GSE_EXCLUDED_FROM_TRAINING \
  --approved-license cc-by-4.0 \
  --remote-probe
```

Raise the per-repository enumeration budget for very large depositions. A study
that exceeds it is flagged `inventory:truncated` and its size is reported as a
lower bound:

```bash
mamba run -n scdata-agent scdata-agent agent-run 'research question' \
  --max-assets 5000 --database var/scdata_agent.sqlite
```

By default a bundle with no declared licence may be admitted on the published
terms of the archive serving it, with the basis recorded for audit. Require a
declared, allowlisted licence instead:

```bash
mamba run -n scdata-agent scdata-agent agent-run 'research question' \
  --strict-license --database var/scdata_agent.sqlite
```

`agent-run` defaults to the planner's `deep` mode (four sequential LLM calls,
~600s budget) so the model actually completes its analysis instead of racing
two calls against a 30s budget each and degrading to a generic template.
Trade thoroughness for speed only when you need an approximate result fast:

```bash
mamba run -n scdata-agent scdata-agent agent-run 'research question' \
  --planner-mode fast --database var/scdata_agent.sqlite
```

At the download checkpoint, stop after planning, download the recommendation, or
download every preflight-ready Bundle (including assets with unknown size):

```bash
mamba run -n scdata-agent scdata-agent resume CHECKPOINT_ID \
  --option plan_only --database var/scdata_agent.sqlite
mamba run -n scdata-agent scdata-agent resume CHECKPOINT_ID \
  --option recommended --database var/scdata_agent.sqlite
mamba run -n scdata-agent scdata-agent resume CHECKPOINT_ID \
  --option all_with_unknown_size --database var/scdata_agent.sqlite
```

After transfer, materialization, local QC, late lineage, and governance, resolve
the second checkpoint to approve or reject the immutable Corpus Snapshot:

```bash
mamba run -n scdata-agent scdata-agent resume CHECKPOINT_ID \
  --option approve_snapshot --database var/scdata_agent.sqlite
mamba run -n scdata-agent scdata-agent resume CHECKPOINT_ID \
  --option reject_snapshot --database var/scdata_agent.sqlite
```

Inspect durable graph state locally:

```bash
mamba run -n scdata-agent scdata-agent graph-state RUN_ID \
  --database var/scdata_agent.sqlite
```

Cloud tracing is opt-in; local SQLite is the default:

```bash
SCDATA_AGENT_TRACE_MODE=langsmith \
LANGSMITH_TRACING=true LANGSMITH_API_KEY=... LANGSMITH_PROJECT=scdata-agent \
mamba run -n scdata-agent scdata-agent agent-run 'research question'
```

Start the local FastAPI/HTMX control room at [http://127.0.0.1:8000](http://127.0.0.1:8000):

```bash
mamba run -n scdata-agent scdata-agent serve \
  --database var/scdata_agent.sqlite --port 8000
```

When `frontend/dist` is present, the same server serves the React control room.
Its run pages use `/ui/runs/<run-id>`, so browser refreshes do not conflict with
the JSON API at `/runs/<run-id>`. For frontend development, run the API command
above alongside `cd frontend && npm run dev`; Vite proxies the API paths to port 8000.

Inspect append-only Catalog records and approved Corpus Snapshots through the API:

```bash
curl 'http://127.0.0.1:8000/catalog/records?record_type=evidence_claim'
curl 'http://127.0.0.1:8000/corpus-snapshots?approved_only=true'
```

Select an LLM provider through environment variables. StepFun remains the default:

```bash
SCDATA_AGENT_MODEL_PROVIDER=openai_compatible
SCDATA_AGENT_MODEL_API_KEY=...
SCDATA_AGENT_MODEL_BASE_URL=https://api.openai.com/v1
SCDATA_AGENT_MODEL=...
```

For Anthropic, set `SCDATA_AGENT_MODEL_PROVIDER=anthropic` plus `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`.

Run LangGraph Studio locally for graph visualization, checkpoint inspection, and interrupt debugging:

```bash
mamba run -n scdata-agent langgraph dev --no-reload --no-browser \
  --config langgraph.json --host 127.0.0.1 --port 2025
```

Then open the Studio URL printed by the command, or use:

```text
https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2025
```

Configure a higher-capability model only for Research Planner while other roles
inherit the default model:

```bash
SCDATA_AGENT_MODEL_PLANNER=YOUR_STRONGER_STEPFUN_MODEL
SCDATA_AGENT_REASONING_EFFORT_PLANNER=high
```

Test complex requirement parsing without a model call:

```bash
mamba run -n scdata-agent pytest tests/test_requirements.py tests/test_llm_requirements.py
```


Build the broad public scATAC and paired multi-omics discovery corpus. Each of the four query families is run against Europe PMC, GEO, and SRA; results are persisted to SQLite:

```bash
mamba run -n scdata-agent scdata-agent corpus-discover \
  --database var/scatac_corpus.sqlite \
  --max-results-per-query 200
```

Use a small source smoke test before a large collection run:

```bash
mamba run -n scdata-agent scdata-agent corpus-discover \
  --database var/scatac_corpus_smoke.sqlite \
  --max-results-per-query 3
```

Run the assay-pairing and corpus-planning tests:

```bash
mamba run -n scdata-agent pytest -q \
  tests/test_assay_profiles.py tests/test_requirements.py tests/test_workflow.py
```
