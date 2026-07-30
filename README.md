# scdata-agent

Evidence-first discovery and curation agent for **public single-cell epigenomics data**
(scATAC-seq / snATAC-seq and single-cell multiome).

`scdata-agent` automates the data-curation half of building a single-cell epigenomics
foundation model: it turns a natural-language research question into an executable
search contract, queries public repositories, reconciles the returned metadata against
observed remote file manifests, and produces an auditable acquisition plan — **stopping
at a human approval gate before a single byte is downloaded.**

> **Status: alpha / research prototype.** The pipeline runs end to end against live
> public APIs. Read [Known limitations](#known-limitations) before trusting its
> output — several classes of dataset are still deliberately routed to human review.

---

## Design principles

These are enforced in code, not just documented:

- **No bytes before consent.** Transfer execution is unreachable until a persisted
  human checkpoint is resolved. A second checkpoint gates publication of the immutable
  corpus snapshot.
- **Evidence has provenance classes.** Claims are labelled by how they were obtained.
  `FILE_OBSERVED` and `RECOMPUTED` evidence requires local bytes and therefore cannot
  be produced before transfer — the agent cannot "conclude" something it never saw.
- **No access circumvention.** Only open full text is fetched. Paywall or
  authentication bypass is out of scope; controlled-access sources stay metadata-only.
- **Deterministic gates, ranked advice.** Hard gates (access, modality, licence) are
  plain code. The LLM proposes and summarises; it never silently decides admission.
- **Append-only catalogue.** Runs, artifacts, claims, bundle states and snapshots are
  written to a local SQLite ledger for cross-run auditing.

## Architecture

A LangGraph state machine (`application/orchestration_v2.py`) drives ten specialist
agents. Discovery forks into a fast direct asset scout and a non-blocking metadata
review; the two rejoin at an escalation gate that decides whether open full text is
needed to resolve remaining hard fields.

```mermaid
flowchart TD
    START([start]) --> init[initialize_run]
    init --> planner[research_planner]
    planner --> pgate{planning_gate}
    pgate -->|needs clarification| planner
    pgate -->|ready| disc[resource_discovery]

    disc --> scout1[asset_scout_direct]
    disc --> ev1[evidence_metadata]
    scout1 --> egate{evidence_escalation_gate}
    ev1 --> egate

    egate -->|hard fields unresolved| ft[evidence_fulltext]
    egate -->|sufficient| scout2[asset_scout_enriched]
    ft --> scout2

    scout2 --> recon[inventory_reconcile]
    recon --> pf[preflight_assessment]
    recon --> lin1[early_lineage]
    pf --> acq[acquisition_strategy]
    lin1 --> acq

    acq --> gate1{{"download_approval<br/>HUMAN CHECKPOINT"}}
    gate1 -->|plan_only| fin[finalize]
    gate1 -->|recommended / all| xfer[transfer_execution]

    xfer --> cur[dataset_curator]
    cur --> mq[materialized_quality]
    mq --> lin2[late_lineage]
    lin2 --> gov[corpus_governance]
    gov --> gate2{{"corpus_approval<br/>HUMAN CHECKPOINT"}}
    gate2 --> fin
    fin --> END([end])
```

### Bundle schemas

A candidate is judged against the shapes it could legitimately take, not against
the union of everything the research question mentioned. Each schema
(`domain/requirements.py::BUNDLE_SCHEMAS`) lists **role groups**: a conjunction of
disjunctions, where every group must be satisfied by at least one of its roles.
Satisfying *any one* schema makes a bundle complete.

| Schema | Applies to | Requires |
|---|---|---|
| `scatac_standalone` | scATAC, snATAC | accessibility signal + a cell axis — **not** an RNA matrix |
| `multiome_rna_atac` | multiome, matched RNA/ATAC | accessibility + RNA matrix + per-cell linkage |
| `atac_protein` | ASAP-seq, ISSAAC-seq | accessibility + protein matrix + a cell axis |
| `raw_reads` | any | FASTQ (legitimate but expensive input) |

"Accessibility signal" is satisfied by fragments **or** a peak matrix **or** a joint
AnnData/MuData object, which is why the groups are disjunctions. This is what stops a
standalone scATAC study from being marked incomplete for lacking multiome files.

Package layout follows a strict dependency direction, asserted by
`tests/test_architecture.py`:

| Layer | Path | May depend on |
|---|---|---|
| `domain` | pure models, gates, role inference | nothing internal |
| `application` | agents + LangGraph orchestration | `domain` |
| `infrastructure` | HTTP sources, LLM routing, persistence | `domain` |
| `interfaces` | Typer CLI, FastAPI control room | all of the above |

## Quick start

```bash
# 1. Environment
mamba env create -f environment.yml
mamba activate scdata-agent
python -m pip install -e '.[dev]'

# 2. Credentials (never commit .env — it is gitignored)
cp .env.example .env
$EDITOR .env                       # set STEPFUN_API_KEY (or another provider)
scdata-agent llm-check             # validates config, sends no billable request

# 3. Understand a question without touching the network
scdata-agent compile 'scATAC-seq and multiome data for representation learning' --no-llm

# 4. Live discovery across Europe PMC + GEO + SRA
export NCBI_EMAIL='you@example.org'   # NCBI asks for this on sustained use
scdata-agent discover 'human lung scATAC-seq and multiome' \
  --max-results 10 \
  --database var/scdata_agent.sqlite

# 5. Full pipeline; stops at the download checkpoint
scdata-agent agent-run 'scATAC and multiome data for representation learning' \
  --database var/scdata_agent.sqlite \
  --data-root data \
  --approved-license cc-by-4.0 \
  --remote-probe
```

The run halts with `status = waiting_for_decision` and an open checkpoint. Resolve it
explicitly — `plan_only` keeps plans and downloads nothing:

```bash
scdata-agent resume CHECKPOINT_ID --option plan_only --database var/scdata_agent.sqlite
```

A local control room (FastAPI + HTMX, SSE event stream) is available via
`scdata-agent serve`. See [`docs/commands.md`](docs/commands.md) for the full command
reference.

## Data source coverage

| Source | Role | Status |
|---|---|---|
| Europe PMC | publications, preprints, open full text | supported |
| NCBI GEO | study records, supplementary file manifests | supported |
| NCBI SRA | raw sequencing studies | supported |
| ENA | FASTQ manifests, checksums, byte sizes | supported |
| Zenodo / Figshare | author-deposited processed data | supported |
| ENCODE | curated epigenomic reference data | **not implemented** |
| CELLxGENE | curated single-cell atlases | **not implemented** |
| Single Cell Portal, HCA, BioStudies/ArrayExpress | atlases and archives | **not implemented** |
| EGA / dbGaP | controlled access | metadata-only by design |

The unimplemented sources are reported honestly at runtime in
`QueryPlan.uncovered_sources` rather than silently ignored. Adding them is the
highest-value contribution right now — see [Roadmap](#roadmap).

## Known limitations

- **Archived supplementary files cannot be assessed remotely.** A large share of GEO
  depositions ship a single `GSExxxxx_RAW.tar`. Its contents are unknowable without
  downloading, so such bundles are labelled `archive:uninspected` and routed to human
  review rather than being scored as complete or incomplete. Extracting archive
  manifests post-transfer is not implemented.
- **Licence status is almost always `unknown`, and this is the binding constraint.**
  GEO/SRA expose no structured licence field and the agent refuses to guess, so in
  practice nothing reaches the `recommended` tier on its own: a measured run ended
  with a bundle that was preflight-ready, access-verified and complete, held back
  solely by `license:unknown`. Asserting the licence for a candidate via the
  `approved_license_candidate_ids` policy moves it to `recommended` with a clean risk
  list. A general, auditable licence policy is the highest-value fix in the roadmap.
- **Role inference is filename-based.** It recognises 10x, SNARE-seq and ENA
  conventions (see `tests/test_role_inference.py`), but a deposition using
  idiosyncratic names will under-report its roles. Inference never guesses: an
  unrecognised file stays unclassified rather than being assigned a plausible role.
- **Only five repositories are wired up.** ENCODE, CELLxGENE, Single Cell Portal and
  HCA — arguably the most valuable sources for this problem — are not implemented and
  are reported in `QueryPlan.uncovered_sources`.
- **The offline planner fallback is a template.** If the model's strategy branch
  exceeds `SCDATA_AGENT_PLANNER_BRANCH_TIMEOUT_SECONDS` (default 30s), a deterministic
  template takes over. The CLI prints a warning when this happens; the plan is still
  auditable but is not model-derived.

## Configuration

Model routing is provider-neutral; StepFun is the default.

| Variable | Purpose |
|---|---|
| `SCDATA_AGENT_MODEL_PROVIDER` | `stepfun` (default), `openai_compatible`, `openai`, `openrouter`, `ollama`, `anthropic` |
| `STEPFUN_API_KEY` / `STEPFUN_MODEL` | default provider credentials |
| `SCDATA_AGENT_MODEL_{PLANNER,DISCOVERY,REVIEW,CHAT}` | per-role model overrides |
| `SCDATA_AGENT_REASONING_EFFORT[_ROLE]` | per-role reasoning effort |
| `NCBI_EMAIL` / `NCBI_API_KEY` | NCBI E-utilities etiquette and rate limits |
| `SCDATA_AGENT_TRACE_MODE` | `local` (default, nothing leaves the machine) or `langsmith` |

**Tracing is local by default.** Setting `SCDATA_AGENT_TRACE_MODE=langsmith` together
with `LANGSMITH_TRACING=true` uploads run traces — including your research questions —
to LangSmith's cloud. Opt in deliberately.

See [`.env.example`](.env.example) for the complete list.

## Development

```bash
mamba run -n scdata-agent pytest              # 61 tests, fully offline
mamba run -n scdata-agent pytest tests/test_architecture.py   # layer boundaries
```

The suite uses `monkeypatch` and `httpx.MockTransport` throughout — no network access
and no API key is needed to run it, which keeps CI cheap and deterministic.

Optional AnnData/MuData inspection support: `pip install -e '.[dev,curation]'`.

## Roadmap

Ordered by value to a single-cell epigenomics foundation-model corpus:

1. Make licence resolution auditable instead of uniformly `unknown`. This currently
   gates the entire `recommended` tier, so it unblocks everything else.
2. Add ENCODE, CELLxGENE, Single Cell Portal, HCA and BioStudies/ArrayExpress adapters.
3. Read archive manifests after transfer so `archive:uninspected` bundles can be
   assessed instead of parked in manual review.
4. Preserve the semantic relation of every extracted accession
   (`primary_data_deposition` vs `reused_dataset` vs `supplementary_reference`).
5. Normalised donor/sample/cell entities with ontology IDs, so lineage can move beyond
   accession and checksum families.
6. Reference-backed TSS enrichment, doublet detection, and explicit RNA/ATAC barcode
   intersection in QC.
7. A corpus optimiser for diversity, rarity, redundancy caps and auditable sampling
   weights.

See [`docs/journal.md`](docs/journal.md) for the detailed internal roadmap.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
