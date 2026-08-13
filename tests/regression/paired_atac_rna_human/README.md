# Paired RNA–ATAC curator benchmark

This directory contains a compact curator-derived reference set for the
`paired_atac_rna_human` profile. It validates the decision vocabulary and scoring
contract used for same-cell/same-nucleus RNA–ATAC curation.

## Contents

- `golden_cases.yaml` — 13 public GEO cases covering READY, HOLD and EXCLUDE
  outcomes across distinct curation reasons.
- `scoring.py` — reduces an observed outcome to decision, relationship and PairUnit
  direction matches.
- `test_regression.py` — validates the reference schema and scoring behavior without
  network or model variability.

The expected values are directional, not verbatim reproductions of curator notes.
READY means that at least one resolvable unit can reach an equivalent preflight-ready
or admitted state; HOLD preserves an actionable uncertainty; EXCLUDE identifies a
scientific or access mismatch. Physical co-measurement and provenance-only matching
remain separate outcomes.

This benchmark complements `eval/`:

- this directory checks curator outcome semantics;
- `eval/` replays captured repository records through executable discovery and
  feasibility logic.

Neither suite presents synthetic scoring fixtures as live end-to-end executions.

## Run

```bash
pytest tests/regression/paired_atac_rna_human/
```
