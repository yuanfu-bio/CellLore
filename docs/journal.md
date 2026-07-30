# Project knowledge

## Current architecture

- The system advances independent semantic Bundles through evidence-linked, remote-inventoried, preflight, approved, transferred, materialized, QC, and governed states.
- Discovery publishes explicit resource pointers and starts direct Asset Scout while Evidence Curator performs non-blocking metadata review. Evidence pointer deltas trigger an enriched scout pass; reconciled inventory is the only Preflight input.
- Evidence escalation joins direct inventory with metadata review and fetches PMC open full text only for unresolved hard fields; paywall or authentication bypass is forbidden.
- Asset Scout resolves ENA, GEO supplementary, Zenodo, and Figshare manifests, actively verifies public HTTP URLs with bounded HEAD and zero-byte Range fallback, and publishes Bundle accessibility/size/type/license/modality summaries. Transfer execution is unreachable before a persisted user checkpoint.
- Reconciled Asset Scout inventory forks into remote-only Preflight and early Lineage & Leakage; Acquisition joins both branches with the inventory. `FILE_OBSERVED` and `RECOMPUTED` evidence require local bytes and cannot be produced before transfer.
- The run ledger stores tasks, messages, artifacts, and checkpoints; the append-only Catalog stores cross-run claims, assets, Bundle states, QC, lineage, and corpus snapshots.
- Dataset Curator preserves source assets, emits provenance, Data Card, and processed-first packages. Post-materialization QC is independent from transformation.
- Final governance runs after late lineage/QC; hard gates are deterministic and an immutable Corpus Snapshot needs a second user approval.

## Implemented

- Python 3.12 mamba environment `scdata-agent`.
- Additive v2.2 contracts cover remote Bundle summaries, preflight scale/gate details, bundle lineage assessments, and three-tier acquisition decisions while preserving older payload defaults.
- Central LangGraph Supervisor uses progressive dual-entry asset scouting plus per-Bundle state, download approval, and corpus approval checkpoints.
- Resumable HTTPS transfer supports range continuation, disk/budget checks, checksums, failure receipts, and idempotent destinations.
- Processed-first materialization distinguishes `RAW_ONLY`, writes immutable provenance/manifest/Data Card, and recognizes AnnData/MuData inputs.
- Local QC checks readability, compression, HDF5 signatures, genome consistency, barcode linkage, sampled fragments/FRiP, and optional AnnData/MuData semantics.
- Early lineage transitively unions canonical accessions, publication IDs, normalized URLs, and declared checksums; late lineage adds verified checksums. Evaluation membership propagates to the full family.
- Acquisition deterministically classifies Bundles as recommended, manual-review, or excluded. Unavailable/controlled/evaluation-family Bundles are excluded; declared-only access, unknown size/license, and non-representative duplicates require manual review.
- FastAPI exposes run control, the cross-run Catalog, corpus snapshots, and bounded remote-probe policy; CLI also exposes a remote-probe toggle alongside data root, byte budget, Evaluation Vault IDs, license allowlist, and full-text policy.
- Legacy Agent/artifact enum values remain readable. The superseded v1 graph
  (`application.orchestration` and its v1 specialist modules) was removed before
  the first public release; `application.orchestration_v2` is the only graph.
- Offline tests cover old behavior plus state transitions, open-full-text access, repository resolvers, transfer receipts, QC, governance, and both approval gates.

## Next

- Split training-ready requirements into alternative Bundle schemas: scATAC should not require RNA matrix or barcode linkage, while same-cell multiome should; acquisition must score each schema independently.
- Preserve the semantic relation of every extracted accession (`primary_data_deposition`, `reused_dataset`, `supplementary_reference`); only primary deposition should form a paper-owned Bundle by default.
- Add ENCODE, BioStudies/ArrayExpress, Single Cell Portal, and HCA adapters; keep EGA/dbGaP metadata-only.
- Add normalized donor/sample/cell entities and ontology IDs so lineage can move beyond accession and checksum families.
- Add reference-backed TSS enrichment, doublet methods, full matrix sparsity, and explicit RNA/ATAC barcode intersection.
- Add converters for common 10x processed layouts to AnnData/MuData; FASTQ-to-matrix remains an explicit high-cost workflow.
- Add a corpus optimizer for diversity, rarity, redundancy caps, license constraints, and auditable sampling weights.
