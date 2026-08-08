# `paired_atac_rna_human` regression benchmark

Ground-truth regression cases for the `paired_atac_rna_human` `CurationProfile`,
sourced from a human curator's real manual review of ~114 studies (recorded
in `04_thinking/agent.xlsx`, not part of this repo). See `golden_cases.yaml`'s
own header for the full field reference and sourcing notes.

## What exists today

- `golden_cases.yaml` -- 13 real cases (5 READY, 3 HOLD, 5 EXCLUDE) chosen to
  cover distinct `manifest_status`/`阻断类别` families, not to be exhaustive.
  Each case names a real GEO accession, the curator's actual stated reason,
  and the *direction* an agent run should reach (decision, `RelationshipKind`,
  a loose pair-unit count) -- not an exact field match.
- `scoring.py` -- direction-match scoring, decoupled from how an outcome was
  produced. Takes an `ObservedOutcome` (whatever shape a real or synthetic
  pipeline result reduces to) and a golden case, returns a `CaseScore`.
- `test_regression.py` -- structural sanity on `golden_cases.yaml` itself,
  and unit tests of `score_case` against synthetic fixtures. Both run today
  under plain `pytest`, no network or LLM required.

## What's deliberately not here yet

`test_real_pipeline_matches_the_curator` is parametrized over every golden
case but marked `@pytest.mark.skip` -- it does not run the real pipeline
against these accessions yet. That needs, for each case:

1. A real `agent-run <accession-shaped question>` (or equivalent) against
   the live GEO/Europe PMC/SRA sources, with a real LLM configured -- this
   repo's own convention (see project memory) is that PairUnit resolution
   and evidence synthesis are LLM-dependent by design and must not be
   evaluated against a `--no-llm` deterministic-only run.
2. Capturing the raw source records that run touched into an offline replay
   corpus, mirroring `eval/records.jsonl` + `eval/golden_questions.yaml`'s
   established pattern in this repo -- so CI can replay the same cases
   afterward without paying for a live LLM/network call on every run.
3. Wiring `test_real_pipeline_matches_the_curator` to load that replay
   corpus, run it through the real pipeline components (not stubs), build an
   `ObservedOutcome` from the result, and call `score_case`.

This was deliberately deferred: capturing real records is a materially
bigger, real-cost step (live LLM calls, live network, and for the READY
cases likely a real download to also validate Phase 5B's post-materialization
checks) than building the ground truth and scoring framework, and the two
were intentionally split into separate units of work.

## Running what exists

```
pytest tests/regression/paired_atac_rna_human/
```

The skipped parametrized case will show up as `SKIPPED` with the reason
above, once per golden case -- that's intentional, so the gap stays visible
in test output rather than silently absent.
