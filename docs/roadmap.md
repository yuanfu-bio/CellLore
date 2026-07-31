# Roadmap and working status

**English** · [中文](roadmap.zh-CN.md)

The living plan. [`journal.md`](journal.md) records what the system **is** —
architecture and implemented behaviour. This file records what it is **not yet**,
and who is moving which piece.

## How to update this file

Updated daily by whoever touched the code that day.

- Change the status marker and the `Updated` date on the row you worked on. Leave
  the rest alone — a diff that touches ten rows is hard to review.
- A task moves to ✅ only when its **acceptance criterion passes**, not when the
  code is written. The criteria below are deliberately written as commands whose
  output can be checked.
- When a task completes, move the substance of it into `journal.md` under
  *Implemented* and leave a one-line entry here. This file should stay short.
- Found something new? Add it under the phase it blocks, with an acceptance
  criterion. A task without one is a wish, not a task.
- If the work changed a gate, an alias table or an inference rule, the criterion
  is a scorecard number, not a passing test. See *Measuring a judgement change*
  in [`../CONTRIBUTING.md`](../CONTRIBUTING.md).
- Disagreeing with a priority is fine and expected — edit the ordering and say
  why in the commit message.

Status markers: ✅ done · 🚧 in progress · ⛔ blocked · ⬜ not started

### Keeping the two languages in sync

[`roadmap.zh-CN.md`](roadmap.zh-CN.md) is a full translation, not a summary. Task
IDs (`P0.1`, `P1.4`, …) are the shared key — never renumber in one file alone.

- **Status, owner and date changes go into both files in the same commit.** They
  are three cells; doing it now is cheaper than reconciling later.
- **Prose changes land in English first**, then the translation. If you are short
  on time, write the English and leave a `TODO(zh)` marker on the Chinese row —
  a stale translation that looks current is worse than one that admits it.
- If the two disagree on a fact, English wins and the Chinese gets fixed.

---

## Objective

Serve the single-cell ATAC foundation-model corpus effort: turn a natural-language
research question into a **trainable dataset with a conformant Data Card**, end to
end, with the provenance and licence basis intact.

Three milestones, in order:

1. **P0** — one real end-to-end run reaches the `pretrain` pool.
2. **P1** — phase-one recall: paired scATAC + scRNA (and further modalities —
   surface protein, other epigenome layers) at conformant quality.
3. **P2** — generalise to other dataset-discovery directions, and a demo that
   runs the whole path in front of an audience.

---

## Verified state — 2026-07-31

Baseline for the daily updates below. Re-verify before disputing a row.

| Fact | Evidence |
|---|---|
| Test suite green, fully offline | `pytest` → 184 passed; `ruff check src tests scripts` clean |
| Curation judgement is measured | `scripts/eval_discovery.py` over `eval/records.jsonl` (48 live-captured records): `pairing_overclaim_rate` 0.40, `modality_recall` 0.52, `deposition_precision` 0.57 |
| NL → approval checkpoint works live | `var/p3_acceptance/stdout.json`: 11 tasks completed, `status = waiting_for_decision`, 3 recommended bundles, 16.27 GB |
| Paired-multiome question is understood | Same run, Chinese same-cell/trimodal question; requirement carried `minimum_pairing_level` |
| **Nothing has ever been downloaded** | every `var/*/data/` is empty; no `transfer_execution` / `dataset_curator` / `corpus_governance` task exists in any sqlite ledger |

The second half of the pipeline is therefore **test-verified only**. That is the
single most important thing this roadmap is trying to change.

---

## P0 — Make the whole path run once (highest priority)

**Exit criterion for the phase:** a real `agent-run` → `resume` → transfer →
materialization → QC → corpus snapshot in which at least one bundle lands in the
`pretrain` pool, with the resulting package present on disk.

Nothing in P1 or P2 should start before this passes. Every later feature widens a
pipe that is not yet connected.

| # | Task | Status | Owner | Updated | Acceptance criterion |
|---|---|---|---|---|---|
| P0.0 | Stop the pipeline overclaiming pairing (see below) | ⬜ | | | `pairing_overclaim_rate` drops from 0.40 toward 0; `pairing_recall` does not regress; baseline scorecard updated in the same commit |
| P0.1 | Pick a small, known-good pilot study (10x Multiome, clear terms, single-digit GB) and record its accession here | ⬜ | | | Accession written into this row; `agent-run` on a narrow question returns it as `recommended` |
| P0.2 | Converters for common 10x processed layouts → AnnData/MuData | ⬜ | | | `barcodes.tsv.gz` + `peaks.bed` + `matrix.mtx.gz` (+ `fragments.tsv.gz`) package yields a non-null `standard_object_path`; offline test built from a real GEO listing |
| P0.3 | First real transfer execution | ⬜ | | | A `transfer_execution` task exists in the ledger with successful receipts; resume interrupted mid-transfer and confirm range continuation + checksum both hold |
| P0.4 | First real materialization | ⬜ | | | `data/curated/<candidate>/<bundle>/` contains `DATA_CARD.md`, `provenance.json`, `manifest.json` and a standard object |
| P0.5 | First real corpus snapshot | ⬜ | | | Snapshot with a non-empty `admitted_bundle_ids`; `corpus_approval` checkpoint resolved |

### P0.0 — the pairing overclaim

Twelve of the thirty standalone snATAC-seq depositions in `eval/records.jsonl`
report themselves as **same-nucleus paired multiome**. Two causes, both in
`infrastructure/sources/common.py`, both from matching aliases against a joined
metadata blob rather than prose:

- `"gene expression"` is in `RNA_ALIASES` (`common.py:22`), and every GEO-brokered
  SRA record carries `contact_name="Gene Expression Omnibus (GEO), NCBI, NLM,
  NIH"` — a transcriptome layer appears out of the submitter's name.
- `_infer_pairing_level` ends with a bare `if "multiome" in content` →
  `SAME_NUCLEUS` (`common.py:137`), which promotes ENCODE records that merely cite
  a protocol PDF named `chromium_multiome_atac_gex.pdf`.

This is a **false positive**, so it will not announce itself: a same-cell request
silently admits standalone ATAC runs whose data cannot support the claim, nothing
enters manual review, and the only visible effect is recall looking better. It is
the error the `multiome_same_cell` / `matched_scrna_scatac` split exists to
prevent, occurring one layer below that split. Phase one depends on exactly this
judgement, which is why it precedes P0.1: picking a pilot study means trusting
these labels.

`forbid_pairing_above` in `eval/golden_questions.yaml` pins it — here the
expensive error is the false positive, not the miss.

### Why P0.2 is the hard blocker

`snapshot_governance.py:118` quarantines any bundle whose
`manifest.standard_object_path is None`, with reason `anndata_or_mudata_missing`.
`standard_object` is currently only populated when a downloaded file *happens* to
be `.h5ad`/`.h5mu` (`materialization.py:76-84`). Typical GEO processed depositions
ship mtx/tsv/bed and never do. So today, even a successful download ends with an
empty admitted list — the converter is what closes the circuit.

---

## P1 — Conformant output and phase-one recall

Paired scATAC + scRNA is the phase-one target; further modalities (surface
protein via ASAP-seq/TEA-seq, other epigenome layers) ride on the same schemas.

| # | Task | Status | Owner | Updated | Acceptance criterion |
|---|---|---|---|---|---|
| P1.1 | Expand `DATA_CARD.md` to a conformant field set (see below) | ⬜ | | | Card for the P0 pilot carries every required field; each value traceable to an evidence record or explicitly marked unknown |
| P1.2 | Read archive manifests after transfer | ⬜ | | | A `GSExxxxx_RAW.tar` bundle produces real role inference instead of `archive:uninspected`; no bundle is judged on unopened contents |
| P1.3 | ENCODE adapter + licence policy | ⬜ | | | Resolver returns assets; policy registered in `domain/licensing.py`; `encode` removed from `QueryPlan.uncovered_sources` |
| P1.4 | CELLxGENE adapter + licence policy | ⬜ | | | Same, for `cellxgene`. Highest-value source after ENCODE: curated atlases with declared licences, which also exercises the `approved` licence state that GEO/SRA never reach |
| P1.5 | Trimodal path on a real study | ⬜ | | | A TEA-seq or DOGMA-seq deposition satisfies `multiome_trimodal` end to end |
| P1.6 | Pairing evidence beyond text inference | ⬜ | | | Observed per-cell barcode intersection upgrades a bundle from `pairing:unverified` to a same-cell claim backed by `FILE_OBSERVED` evidence. The durable answer to P0.0: text inference is what overclaims, observed barcodes cannot |

### Data Card conformance (P1.1)

The current card is nine lines: bundle id, candidate, materialization status, data
roles, standard object, licence basis. That is enough to audit admission and not
enough for anyone to decide whether to train on it.

Align with an existing standard rather than inventing one — HuggingFace Dataset
Card structure for the document shape, Croissant for machine-readable metadata.
Required additions:

- Organism, tissue, disease state, cell types
- Cell count, sample count, donor count
- Genome build (already computed into `provenance.json` as `genome_builds`, simply
  not surfaced on the card)
- Pairing level **and the evidence class it rests on**
- QC metrics and the gate verdict
- Source accessions and the citation
- Corpus pool assignment (pretrain / finetune / evaluation vault)

Non-negotiable per `CONTRIBUTING.md` §2: every field is traceable to an evidence
record, or it is written as unknown. A plausible-looking value that nobody observed
is worse than a blank.

---

## P2 — Generalisation and demo

| # | Task | Status | Owner | Updated | Acceptance criterion |
|---|---|---|---|---|---|
| P2.1 | Make `BUNDLE_SCHEMAS` a registry rather than a fixed enum | ⬜ | | | A new modality direction can be added as data + role rules, without editing the completeness engine |
| P2.2 | Role inference beyond ATAC-centric filename conventions | ⬜ | | | Test cases drawn from real listings in a second direction (scRNA-only, spatial, or methylation) classify correctly |
| P2.3 | Demo path in the control room | ⬜ | | | Single page carries question → plan → candidates → approval → data card; a bystander can follow it without narration |
| P2.4 | Reproducible demo script + pre-seeded database | ⬜ | | | Runs offline from a clean checkout; no API key, no network, same output every time |

**What "generalise" concretely costs:** `BUNDLE_SCHEMAS` (`domain/requirements.py`)
and filename role inference are ATAC-centric by construction. Modality *recognition*
already generalises — `MODALITY_ALIASES` is shared by both compiler paths, and an
unrecognised technology name survives as `supplementary_search_terms`. What does not
generalise is the judgement of **completeness**: a new direction needs its own
schemas and role rules. P2.1 is about making that an addition rather than a surgery.

---

## Deferred — deliberately not now

Real work, but none of it moves P0–P2. Revisit after P1 lands.

- Normalised donor/sample/cell entities with ontology IDs
- Corpus optimiser: diversity, rarity, redundancy caps, auditable sampling weights
- Reference-backed TSS enrichment, doublet detection, full matrix sparsity
- FASTQ → matrix processing (explicitly high-cost; keep it an opt-in workflow)
- Semantic relation of extracted accessions (`primary_data_deposition` vs
  `reused_dataset` vs `supplementary_reference`)
- Single Cell Portal, HCA, BioStudies/ArrayExpress adapters
- EGA / dbGaP stay metadata-only, permanently — this is a design decision, not a gap
