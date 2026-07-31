# Contributing to scdata-agent

Thanks for helping build this. The project curates **public** scientific data,
so correctness and honesty about evidence matter more here than speed.

## Environment

```bash
mamba env create -f environment.yml
mamba activate scdata-agent
python -m pip install -e '.[dev]'          # add ,curation for AnnData/MuData QC
pytest                                     # should be green before you start
```

Credentials live in `.env`, which is gitignored. Copy `.env.example` and fill in
your own key. **Never** commit `.env`, and never `git add -f` it.

## Non-negotiables

These are the invariants that make the agent's output trustworthy. A change that
breaks one of them needs a very good reason and an explicit discussion.

1. **No bytes before consent.** Nothing may download data before the
   `download_approval` checkpoint is resolved, and nothing may publish a corpus
   snapshot before `corpus_approval`. If you add a node to the graph, check
   which side of those gates it belongs on.
2. **Never claim what was not observed.** `FILE_OBSERVED` and `RECOMPUTED`
   evidence requires local bytes. If a fact came from repository metadata, it is
   repository-reported — label it that way.
3. **Unknown is not absent.** An archive whose contents were never opened is
   `archive:uninspected`, not `bundle:missing_roles`. Prefer a warning that
   states the limit of your knowledge over a verdict you cannot support.
4. **No access circumvention.** Open full text only. Controlled-access sources
   (EGA, dbGaP) stay metadata-only.
5. **Deterministic gates stay deterministic.** The LLM may rank, summarise and
   propose. Admission decisions are plain code so they can be audited.

## Layering

`tests/test_architecture.py` enforces the dependency direction:

```
domain  <-  application  <-  interfaces
   ^             |
   +-- infrastructure
```

`domain` imports nothing from the other layers. Put pure models, gates and
inference rules there; put I/O in `infrastructure`; put agents and the LangGraph
wiring in `application`.

## Tests

The whole suite runs offline — outbound calls are stubbed with `monkeypatch` and
`httpx.MockTransport`. Please keep it that way: CI has no API key, and a test
that hits the network is a test that fails at 3am for unrelated reasons.

When you fix a classification or gating bug, add a case built from a **real**
repository filename or payload. `tests/test_role_inference.py` is the model for
this — every entry there came from an actual GEO/ENA listing, which is why it
caught cases that hand-invented names did not.

```bash
pytest                                   # everything
pytest tests/test_architecture.py        # layer boundaries only
pytest -k schema                         # bundle-schema logic
```

## Adding a data source

One of the most valuable contributions right now. See
[`docs/roadmap.md`](docs/roadmap.md) for which sources are prioritised and why —
ENCODE and CELLxGENE come first.

1. Add an adapter under `infrastructure/sources/` implementing the resolver
   protocol in `base.py`, and register it in `asset_registry.py`.
2. Be polite to the API: reuse `request_with_retry`, honour `Retry-After`, set a
   descriptive User-Agent, and support an API key/email env var if the provider
   asks for one (see `ncbi.py`).
3. Remove the source from `QueryPlan.uncovered_sources` only once it genuinely
   works — that list is a promise to the user about coverage.
4. Add tests with `httpx.MockTransport` using a real response body you captured.

## Commits and branches

Work on a branch and open a PR; `main` should stay green. Write commit messages
that explain *why* the change was needed, not just what changed — the reasoning
is the part that is expensive to reconstruct later.
