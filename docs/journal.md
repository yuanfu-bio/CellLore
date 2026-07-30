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
- Bundle completeness is evaluated per modality against alternative `BundleSchema` shapes with OR-semantics role groups; satisfying any one schema completes the Bundle, and acquisition scores each schema independently. Standalone scATAC no longer inherits multiome requirements.
- Role inference reads the whole asset path (10x encodes modality in the directory) and covers 10x, SNARE-seq and ENA conventions. Archives get a dedicated `ARCHIVE` role so unopened containers report `archive:uninspected` instead of a completeness verdict that was never verified.
- Pairing is a first-class requirement. `ResearchRequirement.minimum_pairing_level` is compiled from paired/same-cell/配对 phrasing; `PairingLevel` has tiers (same-cell ≡ same-nucleus > same-sample ≡ same-donor > study-level ≡ unverified, with not-applicable never satisfying a requirement) exposed as `pairing_satisfies`. A same-cell requirement switches the query to named co-assay technologies, makes the preflight `pairing_evidence` gate compare observed against required rather than merely checking that a profile exists, and routes shortfalls to manual review as `pairing:unverified` — never a hard exclusion, since pairing is inferred from text.
- `BUNDLE_SCHEMAS` separates `multiome_same_cell`, `multiome_trimodal` (adds a protein matrix) and `matched_scrna_scatac`; sharing a sample is a weaker claim than sharing a cell and must not pass as multiome.
- Each repository adapter has its own asset budget and processed-data resolvers run before raw reads, so a study with thousands of ENA runs no longer prevents its GEO matrices from being fetched. Exhausted listings are reported as `truncated_resolvers` through the inventory delta to `inventory:truncated` at preflight; a truncated Bundle withholds `total_bytes` rather than reporting a subset sum, and goes to manual review. `--max-assets` is exposed on `agent-run` and the API.
- Modality-dependent judgements read the candidate's own declared modalities via `agents.shared.candidate_modalities`, never the research question's union. Both bundle-schema selection and evidence escalation use it; deriving pairing requirements from the question made standalone scATAC studies owe multiome evidence and triggered needless full-text fetches.
- Licence resolution has four states in strict precedence: `restricted` (hard blocker) > `approved` (declared and allowlisted, or operator-approved) > `repository_policy` (no per-record licence, every file served by an archive whose published terms cover redistribution) > `unknown`. Archive terms never override a licence that was read; coverage must be total but may span several archives, since GEO-processed plus ENA-raw is the normal deposition shape. The policies relied on travel with the preflight report, acquisition reasons, corpus snapshot and Data Card. `--strict-license` disables the fallback.
- The offline planner fallback is domain-neutral: clinical-outcome and spatial-validation needs are added only when the question actually carries disease or outcome cues, and the CLI warns when a plan came from the template rather than the model.
- The LLM-derived planner path (`research_planner.py::_compile_contracts`) recognises modalities via the same `MODALITY_ALIASES` table the rule-based compiler scans (`_known_modalities`, matching every applicable modality rather than at most one), instead of a separately maintained, narrower keyword list that could not name trimodal, ASAP-seq/ISSAAC-seq, or matched scRNA/scATAC. Same-cell pairing narrowing (`restrict_modalities_to_pairing`) and role derivation (`roles_for_modalities`) are single shared functions used by both the rule-based compiler and both LLM integration points (`research_planner.py`, `requirement_compiler.py`), replacing three independently drifted copies -- one of which lacked the PROTEIN_MATRIX branch entirely. A technology name that matches no known alias is preserved as `ResearchRequirement.supplementary_search_terms` rather than discarded; `QueryPlanner` ORs a bounded number of these into the query, alongside the deterministic modality terms rather than replacing them.
- `agent-run` defaults to the planner's `deep` mode (`--planner-mode`, four sequential LLM calls, ~600s budget) instead of `fast` (two parallel calls racing a 30s-per-branch budget, prone to timing out into the offline template on a demanding question). `compile`/`discover` keep defaulting to `fast` for quick iteration.

## Next

- Preserve the semantic relation of every extracted accession (`primary_data_deposition`, `reused_dataset`, `supplementary_reference`); only primary deposition should form a paper-owned Bundle by default.
- Read archive manifests after transfer so `archive:uninspected` Bundles can be assessed rather than parked in manual review; roughly 40% of remotely observed GEO assets are archives, and this is now the main brake on the recommended tier.
- Register a licence policy for each new repository adapter in `domain/licensing.py`; an adapter without one leaves its Bundles at `license:unknown`.
- Add ENCODE, BioStudies/ArrayExpress, Single Cell Portal, and HCA adapters; keep EGA/dbGaP metadata-only.
- Add normalized donor/sample/cell entities and ontology IDs so lineage can move beyond accession and checksum families.
- Add reference-backed TSS enrichment, doublet methods, full matrix sparsity, and explicit RNA/ATAC barcode intersection.
- Add converters for common 10x processed layouts to AnnData/MuData; FASTQ-to-matrix remains an explicit high-cost workflow.
- Add a corpus optimizer for diversity, rarity, redundancy caps, license constraints, and auditable sampling weights.
