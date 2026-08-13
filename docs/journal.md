# Project knowledge

## Release contract

- CellLore 1.0 is complete within a deliberately bounded research scope:
  GEO-led discovery and evidence-first curation of public human
  same-cell/same-nucleus RNA–ATAC data.
- The reproducible contract is the offline test and evaluation suite. Live archive
  contents and model outputs are allowed to vary; scientific gates are not.
- No required blocker or follow-up remains inside the 1.0 scope. A capability outside
  that boundary belongs to an explicitly scoped future release.

## Architecture

- `CurationProfile` is the source of hard requirements, search vocabulary, Bundle
  schemas, evidence floors, deterministic validators and manifest policy.
  `paired_atac_rna_human` is the registered 1.0 profile.
- `application.orchestration_v2` is the only runtime graph. Older serialized enum
  values remain readable solely for persisted-run compatibility.
- The supervisor advances independent semantic Bundles through planning, discovery,
  reconciled inventory, preflight, approval, transfer, materialization, QC, governance
  and immutable snapshots.
- The run ledger persists tasks, messages, artifacts, checkpoints and model-call
  metadata. The append-only Catalog persists evidence claims, assets, Bundle states,
  lineage, QC and Corpus Snapshots.
- Source assets are preserved. Materialization adds manifests, provenance, Data Cards
  and curated layouts; QC remains independent from transformation.

## Scientific invariants

- Pairing is a physical relationship, never accession proximity. Same-sample or
  same-donor assays do not satisfy a same-cell requirement.
- A provisional PairUnit cannot produce verified pairing evidence.
- `FILE_OBSERVED` and `RECOMPUTED` claims require local bytes. Model inference alone
  cannot satisfy a corpus-ready physical-pairing gate.
- Acquisition and governance decisions are deterministic. Models plan, interpret and
  summarize but do not bypass hard evidence or validator requirements.
- Inaccessible, controlled, restricted-licence and evaluation-family Bundles are
  excluded. Incomplete evidence is retained for review rather than guessed.
- Transfer requires persisted approval, and Corpus Snapshot publication requires a
  second approval after local QC and late lineage.
- Evaluation-family membership propagates transitively through canonical identifiers,
  publications, normalized URLs and checksums.

## Operational boundaries

- Direct adapters cover GEO, SRA, ENA and Europe PMC. Zenodo and Figshare are resolved
  when explicitly linked. EGA and dbGaP remain metadata-only.
- Unopened archives, unfamiliar file conventions, truncated inventories and weak
  licence evidence are explicit audit states.
- Private acquisition, access-control circumvention, arbitrary atlas ingestion,
  archive extraction, general matrix conversion and FASTQ-to-matrix processing are
  outside the 1.0 contract.
- LLM-backed execution fails fast without a configured provider. `--no-llm` is for
  tests and offline debugging, not corpus-admission decisions.

## Maintenance

- Keep only durable project knowledge here.
- Keep reusable commands in `docs/commands.md`.
- Record scope changes in release documentation, not as an open-ended backlog.
