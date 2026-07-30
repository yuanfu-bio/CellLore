"""Typed contracts shared by discovery, curation, and governance stages."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Modality(StrEnum):
    SCATAC = "scatac_seq"
    SNATAC = "snatac_seq"
    MULTIOME_RNA_ATAC = "multiome_rna_atac"
    MATCHED_SCRNA_SCATAC = "matched_scrna_scatac"
    MULTIOME_ATAC_PROTEIN = "multiome_atac_protein"
    MULTIOME_RNA_ATAC_PROTEIN = "multiome_rna_atac_protein"
    ATAC_OTHER_MULTIOMICS = "atac_other_multiomics"
    SCRNA = "scrna_seq"
    OTHER = "other"
    UNKNOWN = "unknown"


class OmicsLayer(StrEnum):
    CHROMATIN_ACCESSIBILITY = "chromatin_accessibility"
    TRANSCRIPTOME = "transcriptome"
    PROTEIN = "protein"
    DNA_METHYLATION = "dna_methylation"
    HISTONE_MARK = "histone_mark"
    PERTURBATION = "perturbation"
    SPATIAL = "spatial"
    OTHER = "other"


class PairingLevel(StrEnum):
    SAME_CELL = "same_cell"
    SAME_NUCLEUS = "same_nucleus"
    SAME_SAMPLE = "same_sample"
    SAME_DONOR = "same_donor"
    STUDY_LEVEL = "study_level"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"


# How strongly two omics layers are tied together. Cell and nucleus are one
# tier: both mean the layers were measured in the same physical unit, which is
# what makes cross-modality supervision possible. Sample and donor pairing is a
# genuinely weaker claim -- the assays were run separately.
_PAIRING_RANK: dict[PairingLevel, int] = {
    PairingLevel.NOT_APPLICABLE: 0,
    PairingLevel.UNVERIFIED: 1,
    PairingLevel.STUDY_LEVEL: 1,
    PairingLevel.SAME_DONOR: 2,
    PairingLevel.SAME_SAMPLE: 2,
    PairingLevel.SAME_NUCLEUS: 3,
    PairingLevel.SAME_CELL: 3,
}


def pairing_rank(level: PairingLevel) -> int:
    """Strength of a pairing claim; higher binds the layers more tightly."""
    return _PAIRING_RANK.get(level, 0)


def pairing_satisfies(observed: PairingLevel, minimum: PairingLevel | None) -> bool:
    """Whether an observed pairing level meets a required one.

    ``NOT_APPLICABLE`` never satisfies a requirement: a standalone assay has no
    pairing to offer, which is different from having one that is merely unproven.
    """
    if minimum is None:
        return True
    if observed == PairingLevel.NOT_APPLICABLE:
        return False
    return pairing_rank(observed) >= pairing_rank(minimum)


class AssayProfile(StrictModel):
    label: str
    modalities: list[Modality] = Field(default_factory=list)
    omics_layers: list[OmicsLayer] = Field(default_factory=list)
    pairing_level: PairingLevel = PairingLevel.UNVERIFIED
    technology_terms: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


def best_pairing_level(profiles: list[AssayProfile]) -> PairingLevel | None:
    """The strongest pairing any inferred assay profile claims, or None.

    Profiles are alternative readings of the same study, so the strongest one
    is the study's best case; preflight then decides whether that is enough.
    """
    if not profiles:
        return None
    return max((item.pairing_level for item in profiles), key=pairing_rank)


class DataRole(StrEnum):
    STANDARD_OBJECT = "standard_anndata_mudata"
    RAW_READS = "raw_reads"
    ALIGNED_READS = "aligned_reads"
    FRAGMENTS = "chromatin_fragments"
    FRAGMENT_INDEX = "fragment_index"
    PEAKS = "peak_calls"
    PEAK_MATRIX = "peak_by_cell_matrix"
    RNA_MATRIX = "rna_count_matrix"
    PROTEIN_MATRIX = "protein_count_matrix"
    CELL_METADATA = "cell_metadata"
    SAMPLE_METADATA = "sample_metadata"
    DONOR_METADATA = "donor_metadata"
    BARCODE_LINKAGE = "barcode_linkage"
    QC_METRICS = "qc_metrics"
    REFERENCE = "reference_files"
    PIPELINE = "pipeline_code"
    # A container whose payload cannot be identified without opening it. Kept
    # distinct from OTHER so remote assessment can say "unknown, not absent".
    ARCHIVE = "archive"
    OTHER = "other"


class AccessLevel(StrEnum):
    PUBLIC_VERIFIED = "public_verified"
    PUBLIC_DECLARED = "public_declared"
    AUTH_REQUIRED = "auth_required"
    CONTROLLED = "controlled"
    EMBARGOED = "embargoed"
    WITHDRAWN = "withdrawn"
    UNKNOWN = "unknown"


class EvidenceClass(StrEnum):
    REPOSITORY_REPORTED = "repository_reported"
    ARTICLE_REPORTED = "article_reported"
    FILE_OBSERVED = "file_observed"
    RECOMPUTED = "recomputed"
    ONTOLOGY_NORMALIZED = "ontology_normalized"
    MODEL_INFERRED = "model_inferred"
    USER_CORRECTED = "user_corrected"


class ValidationStatus(StrEnum):
    VALIDATED = "validated"
    CONFLICT = "conflict"
    UNVALIDATED = "unvalidated"
    REJECTED = "rejected"


class GateStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"


class CurationDecision(StrEnum):
    ELIGIBLE = "eligible"
    MANUAL_REVIEW = "manual_review"
    EXCLUDE = "exclude"


class AssetAvailability(StrEnum):
    OBSERVED = "observed"
    DECLARED = "declared"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TransferProtocol(StrEnum):
    HTTPS = "https"
    FTP = "ftp"
    ASPERA = "aspera"
    S3 = "s3"
    GLOBUS = "globus"
    MANUAL = "manual"


class CorpusPool(StrEnum):
    PRETRAIN = "pretrain"
    FINETUNE = "finetune"
    EVALUATION_VAULT = "evaluation_vault"
    QUARANTINE = "quarantine"
    EXCLUDE = "exclude"


class Identifier(StrictModel):
    namespace: str
    value: str

    @field_validator("namespace", "value")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        return value.strip()

    @property
    def canonical(self) -> str:
        return f"{self.namespace.lower()}:{self.value.upper()}"


class Evidence(StrictModel):
    evidence_id: str
    source_name: str
    source_url: str | None = None
    locator: str
    raw_value: Any
    evidence_class: EvidenceClass
    retrieved_at: datetime = Field(default_factory=utc_now)
    snapshot_hash: str | None = None
    extractor_version: str = "scdata-agent/0.1.0"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED


class EvaluationPolicy(StrictModel):
    reserve_independent_studies: bool = False
    holdout_unit: str | None = None
    prevent_lineage_leakage: bool = False


class BundleSchema(StrictModel):
    """One acceptable shape for a training-ready bundle.

    A single flat list of required roles cannot express what these datasets
    actually look like: standalone scATAC must not be asked for an RNA matrix,
    and accessibility signal is equally usable as fragments or as a peak matrix.
    ``required_role_groups`` is therefore a conjunction of disjunctions — every
    group must be satisfied by at least one of its roles.
    """

    schema_id: str
    label: str = ""
    modalities: list[Modality] = Field(default_factory=list)
    required_role_groups: list[list[DataRole]] = Field(default_factory=list)
    preferred_roles: list[DataRole] = Field(default_factory=list)

    def unmet_groups(self, observed_roles: set[DataRole]) -> list[list[DataRole]]:
        """Return the role groups that no observed role satisfies."""
        return [
            group
            for group in self.required_role_groups
            if not set(group) & observed_roles
        ]

    def is_satisfied_by(self, observed_roles: set[DataRole]) -> bool:
        return not self.unmet_groups(observed_roles)


class ResearchRequirement(StrictModel):
    question: str
    organisms: list[str] = Field(default_factory=list)
    tissues: list[str] = Field(default_factory=list)
    diseases: list[str] = Field(default_factory=list)
    cell_types: list[str] = Field(default_factory=list)
    modalities: list[Modality] = Field(default_factory=list)
    acceptable_modalities: list[Modality] = Field(default_factory=list)
    preferred_modalities: list[Modality] = Field(default_factory=list)
    required_data_roles: list[DataRole] = Field(default_factory=list)
    preferred_data_roles: list[DataRole] = Field(default_factory=list)
    minimum_donors: int | None = Field(default=None, ge=1)
    # Weakest pairing a candidate may offer and still count. Unset means the
    # question did not ask for paired data and pairing is not gated at all.
    minimum_pairing_level: PairingLevel | None = None
    public_only: bool = True
    excluded_access_levels: list[AccessLevel] = Field(default_factory=list)
    excluded_biological_sources: list[str] = Field(default_factory=list)
    downstream_tasks: list[str] = Field(default_factory=list)
    evaluation_policy: EvaluationPolicy = Field(default_factory=EvaluationPolicy)
    hard_constraints: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    unresolved_constraints: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    # Free text the LLM identified as relevant but that does not map to any
    # known Modality -- e.g. a co-assay technology not yet in MODALITY_ALIASES.
    # QueryPlanner ORs a bounded number of these into the query so recall for
    # a technology can improve before anyone updates the alias table for it.
    supplementary_search_terms: list[str] = Field(default_factory=list)
    compiler: str = "rule_based_v1"
    compiler_warnings: list[str] = Field(default_factory=list)
    planner_diagnostics: list[str] = Field(default_factory=list)


class QuerySpec(StrictModel):
    source: str
    query: str
    purpose: str
    max_results: int = Field(default=20, ge=1, le=500)


class QueryPlan(StrictModel):
    requirement: ResearchRequirement
    queries: list[QuerySpec]
    uncovered_sources: list[str] = Field(default_factory=list)


class SourceRecord(StrictModel):
    record_id: str
    source_name: str
    external_id: str
    title: str
    description: str | None = None
    source_url: str | None = None
    identifiers: list[Identifier] = Field(default_factory=list)
    publication_identifiers: list[Identifier] = Field(default_factory=list)
    organisms: list[str] = Field(default_factory=list)
    modalities: list[Modality] = Field(default_factory=list)
    assay_profiles: list[AssayProfile] = Field(default_factory=list)
    access_level: AccessLevel = AccessLevel.UNKNOWN
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=utc_now)


class DatasetCandidate(StrictModel):
    candidate_id: str
    title: str
    description: str | None = None
    identifiers: list[Identifier] = Field(default_factory=list)
    publication_identifiers: list[Identifier] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    organisms: list[str] = Field(default_factory=list)
    tissues: list[str] = Field(default_factory=list)
    diseases: list[str] = Field(default_factory=list)
    modalities: list[Modality] = Field(default_factory=list)
    assay_profiles: list[AssayProfile] = Field(default_factory=list)
    observed_data_roles: list[DataRole] = Field(default_factory=list)
    donor_count: int | None = Field(default=None, ge=0)
    sample_count: int | None = Field(default=None, ge=0)
    cell_count: int | None = Field(default=None, ge=0)
    access_level: AccessLevel = AccessLevel.UNKNOWN
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class FeasibilityDimension(StrictModel):
    status: GateStatus
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)


class FeasibilityReport(StrictModel):
    report_id: str
    candidate_id: str
    dimensions: dict[str, FeasibilityDimension]
    missing_required_roles: list[DataRole] = Field(default_factory=list)
    decision: CurationDecision
    decision_reasons: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class AssetLocation(StrictModel):
    url: str
    protocol: TransferProtocol
    access_level: AccessLevel = AccessLevel.UNKNOWN
    size_bytes: int | None = Field(default=None, ge=0)
    checksum_algorithm: str | None = None
    checksum: str | None = None
    availability: AssetAvailability = AssetAvailability.UNKNOWN
    evidence_ids: list[str] = Field(default_factory=list)


class DataAsset(StrictModel):
    asset_id: str
    candidate_id: str
    role: DataRole
    file_name: str
    media_type: str | None = None
    compression: str | None = None
    genome_build: str | None = None
    license_id: str | None = None
    license_url: str | None = None
    locations: list[AssetLocation] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class AssetBundle(StrictModel):
    bundle_id: str
    candidate_id: str
    bundle_type: str
    required_roles: list[DataRole]
    optional_roles: list[DataRole] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list)
    missing_roles: list[DataRole] = Field(default_factory=list)
    complete: bool = False


class TransferItem(StrictModel):
    asset_id: str
    source_url: str
    destination: str
    protocol: TransferProtocol
    expected_size_bytes: int | None = Field(default=None, ge=0)
    checksum_algorithm: str | None = None
    checksum: str | None = None


class TransferPlan(StrictModel):
    plan_id: str
    bundle_id: str
    items: list[TransferItem] = Field(default_factory=list)
    unresolved_asset_ids: list[str] = Field(default_factory=list)
    total_expected_bytes: int | None = Field(default=None, ge=0)
    executable: bool = False
    policy_version: str = "transfer_policy_v1"


class CorpusDecisionRecord(StrictModel):
    decision_id: str
    candidate_id: str
    pool: CorpusPool
    eligible: bool
    reason_codes: list[str]
    feasibility_report_id: str
    bundle_ids: list[str] = Field(default_factory=list)
    lineage_family_ids: list[str] = Field(default_factory=list)
    policy_version: str = "corpus_policy_v1"
    decided_at: datetime = Field(default_factory=utc_now)


class AssetResolutionResult(StrictModel):
    candidate_id: str
    queried_accessions: list[str] = Field(default_factory=list)
    assets: list[DataAsset] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    # Adapters whose listing was cut short by a budget. Their assets are a
    # subset, so any total derived from them is a lower bound, not a size.
    truncated_resolvers: list[str] = Field(default_factory=list)


class DiscoveryRunResult(StrictModel):
    run_id: str
    requirement: ResearchRequirement
    query_plan: QueryPlan
    source_records: list[SourceRecord]
    candidates: list[DatasetCandidate]
    feasibility_reports: list[FeasibilityReport]
    errors: list[str] = Field(default_factory=list)

