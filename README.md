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
| `multiome_same_cell` | 10x Multiome, SNARE-seq, SHARE-seq | accessibility + RNA matrix + per-cell linkage |
| `multiome_trimodal` | TEA-seq, DOGMA-seq | the above **+ a protein matrix** |
| `matched_scrna_scatac` | separate assays on matched samples | accessibility + RNA matrix + a cell axis |
| `atac_protein` | ASAP-seq, ISSAAC-seq | accessibility + protein matrix + a cell axis |
| `raw_reads` | any | FASTQ (legitimate but expensive input) |

`matched_scrna_scatac` is deliberately separate from `multiome_same_cell`: sharing
a sample is a much weaker claim than sharing a cell, and collapsing the two let
matched-sample studies pass as multiome.

"Accessibility signal" is satisfied by fragments **or** a peak matrix **or** a joint
AnnData/MuData object, which is why the groups are disjunctions. This is what stops a
standalone scATAC study from being marked incomplete for lacking multiome files.

### Licence handling

Public genomics archives mostly publish no per-record licence. Treating that silence
as "unknown" is literally correct but leaves nothing recommendable; treating it as an
approved licence asserts a fact nobody checked. The agent takes a third position, with
four distinct states resolved in strict precedence order:

| Status | Meaning |
|---|---|
| `restricted` | A declared licence matched the restricted list — hard blocker, always wins |
| `approved` | A declared licence was read and is on your allowlist, or you approved the candidate |
| `repository_policy` | No per-record licence exists, and every file is served by an archive whose own published terms cover redistribution |
| `unknown` | Anything else — including a declared licence that is real but not allowlisted |

`repository_policy` never overrides a licence that was actually read; it only fills a
genuine gap. Coverage must be **total** — one file from an unrecognised host voids the
basis for the whole bundle — but it may come from more than one archive, since a
deposition split across GEO (processed) and ENA (raw reads) is the norm. The policies
relied on are recorded on the preflight report, in the acquisition plan's reasons, in
the corpus snapshot, and in the package's `DATA_CARD.md`, so the basis is auditable
rather than merely trusted.

Pass `--strict-license` to require a declared, allowlisted licence and disable archive
terms entirely.

### Requiring paired data

Asking for *paired* multiome is a constraint, not a topic. Phrasings such as
"same-cell", "joint profiling", "10x Multiome", "配对", "同细胞" compile to a
`minimum_pairing_level` on the requirement, which then changes three things:

- **Search** switches from the broad modality blob to the named co-assay
  technologies (SNARE-seq, SHARE-seq, sci-CAR, Paired-seq, TEA-seq, DOGMA-seq),
  so standalone scATAC is no longer recalled alongside.
- **Verification** compares the pairing level actually claimed by the candidate
  against what was required. Levels form tiers: same-cell ≡ same-nucleus >
  same-sample ≡ same-donor > study-level ≡ unverified.
- **Decision** routes a shortfall to `manual_review` with `pairing:unverified`
  rather than excluding it, because pairing is *inferred from text*: many real
  multiome studies never write "same cell" in their abstract.

### How the LLM's understanding reaches the query

`agent-run` defaults to the planner's `deep` mode: four sequential LLM calls
(intent → study design → critical review → synthesis, ~600s budget) rather than
`fast` mode's two calls racing a 30s-per-branch budget. `fast` degrades to a
generic template on timeout; `deep` is what lets the model actually finish
analysing the question. Pass `--planner-mode fast` (or set
`SCDATA_AGENT_PLANNER_MODE=fast`) when you want a quick, approximate result
instead — `compile` and `discover` still default to `fast` for quick iteration.

Whichever mode runs, the LLM's modality recognition and the deterministic
compiler's now share one alias table (`domain/requirements.py::MODALITY_ALIASES`)
instead of the LLM path re-implementing a narrower version of it — a mismatch
that once caused a literal "TEA-seq/DOGMA-seq trimodal" request to compile
without the trimodal modality. A technology name the LLM identifies that this
table doesn't cover yet — a future co-assay method, for instance — is not
discarded: it lands in `ResearchRequirement.supplementary_search_terms` and
`QueryPlanner` ORs a bounded number of them into the query, so recall for a
genuinely new assay can improve before anyone updates the alias table for it.
This is deliberately *additive* rather than letting the LLM draft the query
outright, matching the deterministic-gates principle above.

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
- **Most public data is admitted on archive terms, not a read licence.** GEO, SRA and
  ENA publish no per-record licence field, so bundles sourced entirely from them are
  marked `repository_policy` rather than `approved` — see
  [Licence handling](#licence-handling). That is a weaker basis than a licence
  somebody read, and the distinction is deliberately preserved end to end.
- **Archives are the remaining brake on the `recommended` tier.** In a measured run
  the top bundle reaches `recommended` on archive terms with an empty risk list, but
  the next one is held at `archive:uninspected` alone.
- **Very large depositions are enumerated only up to a budget.** `--max-assets`
  (default 500) caps how many files are listed *per repository*. A study that exceeds
  it is flagged `inventory:truncated`, its reported size becomes a lower bound, and it
  is routed to human review rather than being treated as fully inventoried.
- **Role inference is filename-based.** It recognises 10x, SNARE-seq and ENA
  conventions (see `tests/test_role_inference.py`), but a deposition using
  idiosyncratic names will under-report its roles. Inference never guesses: an
  unrecognised file stays unclassified rather than being assigned a plausible role.
- **Only five repositories are wired up.** ENCODE, CELLxGENE, Single Cell Portal and
  HCA — arguably the most valuable sources for this problem — are not implemented and
  are reported in `QueryPlan.uncovered_sources`.
- **The offline planner fallback is a template.** In `fast` mode, if the model's
  strategy branch exceeds `SCDATA_AGENT_PLANNER_BRANCH_TIMEOUT_SECONDS` (default
  30s), a deterministic template takes over. The CLI prints a warning when this
  happens; the plan is still auditable but is not model-derived. `agent-run`
  defaults to `deep` mode specifically to avoid this — pass `--planner-mode fast`
  only when you accept the risk of degradation in exchange for a faster result.

## Configuration

Model routing is provider-neutral; StepFun is the default.

| Variable | Purpose |
|---|---|
| `SCDATA_AGENT_MODEL_PROVIDER` | `stepfun` (default), `openai_compatible`, `openai`, `openrouter`, `ollama`, `anthropic` |
| `STEPFUN_API_KEY` / `STEPFUN_MODEL` | default provider credentials |
| `SCDATA_AGENT_MODEL_{PLANNER,DISCOVERY,REVIEW,CHAT}` | per-role model overrides |
| `SCDATA_AGENT_REASONING_EFFORT[_ROLE]` | per-role reasoning effort |
| `NCBI_EMAIL` / `NCBI_API_KEY` | NCBI E-utilities etiquette and rate limits |
| `SCDATA_AGENT_PLANNER_BRANCH_TIMEOUT_SECONDS` | `fast`-mode planner latency budget before the offline template takes over (default 30) |
| `SCDATA_AGENT_PLANNER_MODE` | `deep` or `fast`; `compile`/`discover` default here (default `fast`). `agent-run --planner-mode` overrides this per run (default `deep`) |
| `SCDATA_AGENT_PLANNER_TIMEOUT_SECONDS` | overall planner budget (default 35s for `fast`, 600s for `deep`) |
| `SCDATA_AGENT_TRACE_MODE` | `local` (default, nothing leaves the machine) or `langsmith` |

**Tracing is local by default.** Setting `SCDATA_AGENT_TRACE_MODE=langsmith` together
with `LANGSMITH_TRACING=true` uploads run traces — including your research questions —
to LangSmith's cloud. Opt in deliberately.

See [`.env.example`](.env.example) for the complete list.

## Development

```bash
mamba run -n scdata-agent pytest              # 184 tests, fully offline
mamba run -n scdata-agent ruff check src tests                # lint
mamba run -n scdata-agent pytest tests/test_architecture.py   # layer boundaries
```

The suite uses `monkeypatch` and `httpx.MockTransport` throughout — no network access
and no API key is needed to run it, which keeps CI cheap and deterministic.

Optional AnnData/MuData inspection support: `pip install -e '.[dev,curation]'`.

## Roadmap

Ordered by value to a single-cell epigenomics foundation-model corpus:

1. Read archive manifests after transfer so `archive:uninspected` bundles can be
   assessed instead of parked in manual review. This is now the main brake on the
   `recommended` tier: roughly 40% of remotely observed GEO assets are archives.
2. Add ENCODE, CELLxGENE, Single Cell Portal, HCA and BioStudies/ArrayExpress adapters,
   each with its licence policy registered in `domain/licensing.py`.
4. Preserve the semantic relation of every extracted accession
   (`primary_data_deposition` vs `reused_dataset` vs `supplementary_reference`).
5. Normalised donor/sample/cell entities with ontology IDs, so lineage can move beyond
   accession and checksum families.
6. Reference-backed TSS enrichment, doublet detection, and explicit RNA/ATAC barcode
   intersection in QC.
7. A corpus optimiser for diversity, rarity, redundancy caps and auditable sampling
   weights.

[`docs/roadmap.md`](docs/roadmap.md) tracks this with owners, acceptance criteria
and daily status ([中文](docs/roadmap.zh-CN.md)); [`docs/journal.md`](docs/journal.md)
records the architecture and what is already implemented.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
