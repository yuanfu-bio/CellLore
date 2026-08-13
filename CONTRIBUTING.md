# Contributing to CellLore

CellLore curates scientific evidence and public data. Correct provenance and explicit
uncertainty matter more than optimistic automation.

The 1.0 repository is an archived, scoped research release. Bug fixes, reproducibility
improvements and changes inside the declared scope are welcome. New product areas
should define a new release scope rather than quietly expanding the 1.0 contract.

## Set up the repository

```bash
mamba env create -f environment.yml
mamba activate celllore
python -m pip install -e '.[dev,curation]'
pytest
```

Copy `.env.example` only when a manual model-backed run is needed:

```bash
cp .env.example .env
```

Keep `.env`, API keys, private records and unpublished data out of Git.

## Preserve the core invariants

1. **No transfer before approval.** Data downloads must remain unreachable until
   `download_approval`; Corpus Snapshots require `corpus_approval`.
2. **Claim only the observed evidence class.** Repository metadata is not file
   observation, and model inference is not physical validation.
3. **Unknown is not absent.** An unopened archive is `archive:uninspected`, not proof
   that required files are missing.
4. **Do not circumvent access controls.** Controlled-access sources and paywalled
   content remain metadata-only.
5. **Keep hard gates deterministic.** Models may plan, interpret and summarize.
   Admission rules must remain auditable code.
6. **Protect evaluation separation.** A known evaluation-family identifier, URL or
   checksum must not enter a training snapshot.
7. **Preserve source assets and provenance.** Transformations add derived artifacts;
   they do not erase the original record.

## Respect package boundaries

```text
interfaces  ──► application ──► domain
                    │
                    └─────────► infrastructure ──► domain
```

- `domain`: pure contracts, evidence rules, gates and lifecycle logic.
- `application`: agents, workflows and orchestration.
- `infrastructure`: network, model, persistence, transfer and QC implementations.
- `interfaces`: CLI, API and UI delivery.

Run `pytest tests/test_architecture.py` after changing imports.

## Test behavior, not implementation history

The suite must remain offline and deterministic. Use `httpx.MockTransport`,
monkeypatching or captured fixtures for external services.

When changing a classifier, resolver or gate:

- add a regression case built from a real public repository shape;
- assert the scientific outcome and evidence class;
- cover uncertainty and failure behavior, not only the successful path;
- run the scorecard comparison in addition to `pytest`.

```bash
ruff check src tests scripts
pytest

python scripts/eval_discovery.py --out var/scorecard.json
python scripts/compare_scorecards.py \
  eval/baseline_scorecard.json var/scorecard.json
```

If a deliberate improvement changes a baseline metric, update
`eval/baseline_scorecard.json` in the same commit and explain the scientific reason.

## Add or change a source adapter

A source is considered supported only when its behavior is executable and tested.

1. Implement the adapter under `src/celllore/infrastructure/sources/`.
2. Register it in the source or asset registry used by the workflow.
3. Reuse bounded retries, honor `Retry-After`, and send a descriptive User-Agent.
4. Preserve raw identifiers, URLs, access state, licence basis and retrieval evidence.
5. Add offline tests from representative public responses.
6. Remove the source from `QueryPlan.uncovered_sources` only after the integration
   genuinely works.

## Frontend changes

The packaged UI is generated from `frontend/`:

```bash
cd frontend
npm ci
npm run build
cd ..
git diff --exit-code -- src/celllore/interfaces/static/ui
```

Commit the updated production bundle together with its source change.

## Before opening a pull request

```bash
ruff check src tests scripts
pytest
python scripts/eval_discovery.py --out var/scorecard.json
python scripts/compare_scorecards.py \
  eval/baseline_scorecard.json var/scorecard.json
```

Also build the frontend when its source changed. Keep commits focused, explain why the
change is scientifically or operationally necessary, and update documentation only
with knowledge that will remain useful.
