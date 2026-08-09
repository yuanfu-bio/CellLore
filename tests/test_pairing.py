"""Same-cell pairing as an expressible, searchable, verifiable requirement.

A baseline run with an explicitly paired question showed the gap: of 15 inferred
assay profiles only 5 claimed same-cell or same-nucleus measurement, yet the
`pairing_evidence` gate passed 7 of 7 bundles because it only checked whether an
assay profile existed at all. Asking for paired data changed nothing downstream.
"""

import pytest

from scdata_agent.application.agents.progressive_preflight import (
    ProgressivePreflightAssessmentAgent,
)
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    AgentName,
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.application.resource_pipeline import bundle_shape_for
from scdata_agent.domain.curation.profiles.paired_atac_rna_human import (
    PAIRED_ATAC_RNA_HUMAN,
)
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.lifecycle import BundleStatus, EvidenceClaim, SemanticBundle
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    EvidenceClass,
    GateStatus,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
    QueryPlan,
    RelationshipKind,
    ResearchRequirement,
    TransferProtocol,
    best_pairing_level,
    pairing_satisfies,
    relationship_kind,
)
from scdata_agent.domain.requirements import (
    BUNDLE_SCHEMAS,
    QueryPlanner,
    RuleBasedRequirementCompiler,
    applicable_bundle_schemas_by_layers,
)
from scdata_agent.domain.resource_pipeline import (
    BundleAccessStatus,
    RemoteBundleInventorySummary,
)


class TestPairingOrdering:
    def test_same_cell_and_same_nucleus_are_one_tier(self):
        """Both mean one physical unit, which is what enables supervision."""
        assert pairing_satisfies(PairingLevel.SAME_NUCLEUS, PairingLevel.SAME_CELL)
        assert pairing_satisfies(PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS)

    def test_sample_matching_does_not_satisfy_same_cell(self):
        for weaker in (
            PairingLevel.SAME_SAMPLE, PairingLevel.SAME_DONOR,
            PairingLevel.STUDY_LEVEL, PairingLevel.UNVERIFIED,
        ):
            assert not pairing_satisfies(weaker, PairingLevel.SAME_CELL)

    def test_same_cell_satisfies_a_weaker_requirement(self):
        assert pairing_satisfies(PairingLevel.SAME_CELL, PairingLevel.SAME_SAMPLE)

    def test_not_applicable_never_satisfies_a_requirement(self):
        """A standalone assay has no pairing, which is not merely unproven."""
        assert not pairing_satisfies(
            PairingLevel.NOT_APPLICABLE, PairingLevel.STUDY_LEVEL
        )

    def test_no_requirement_accepts_anything(self):
        assert pairing_satisfies(PairingLevel.STUDY_LEVEL, None)

    def test_best_level_wins_across_profiles(self):
        profiles = [
            AssayProfile(label="a", pairing_level=PairingLevel.STUDY_LEVEL, rationale="x"),
            AssayProfile(label="b", pairing_level=PairingLevel.SAME_CELL, rationale="x"),
        ]
        assert best_pairing_level(profiles) == PairingLevel.SAME_CELL
        assert best_pairing_level([]) is None


class TestRelationshipKind:
    """Physical co-measurement is a category, not a high rank.

    A hard pairing requirement must exclude same-donor/same-sample and any
    future computational/imputed pairing method outright -- no matter how
    high it would rank on the ordinal scale -- which is exactly what
    ``PairingRequirement`` (set membership) can do and ``pairing_satisfies``
    (rank comparison) cannot.
    """

    def test_same_cell_and_same_nucleus_are_physical_co_measurement(self):
        assert relationship_kind(PairingLevel.SAME_CELL) == RelationshipKind.PHYSICAL_CO_MEASUREMENT
        assert relationship_kind(PairingLevel.SAME_NUCLEUS) == RelationshipKind.PHYSICAL_CO_MEASUREMENT

    def test_sample_and_donor_matching_are_provenance_not_physical(self):
        assert relationship_kind(PairingLevel.SAME_SAMPLE) == RelationshipKind.PROVENANCE_MATCH
        assert relationship_kind(PairingLevel.SAME_DONOR) == RelationshipKind.PROVENANCE_MATCH

    def test_not_applicable_and_unverified_are_their_own_categories(self):
        assert relationship_kind(PairingLevel.NOT_APPLICABLE) == RelationshipKind.NOT_APPLICABLE
        assert relationship_kind(PairingLevel.STUDY_LEVEL) == RelationshipKind.UNVERIFIED
        assert relationship_kind(PairingLevel.UNVERIFIED) == RelationshipKind.UNVERIFIED


class TestPairingRequirementIsSetMembershipNotRank:
    def test_acceptable_levels_admit_only_the_declared_set(self):
        requirement = PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        )
        assert requirement.is_satisfied_by(PairingLevel.SAME_CELL)
        assert requirement.is_satisfied_by(PairingLevel.SAME_NUCLEUS)

    def test_provenance_match_never_satisfies_a_physical_requirement(self):
        """Same-donor/same-sample must not satisfy same-cell/nucleus even
        though it ranks below it on the ordinal scale -- the point is that
        rank is not the mechanism this gate uses at all."""
        requirement = PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        )
        assert not requirement.is_satisfied_by(PairingLevel.SAME_SAMPLE)
        assert not requirement.is_satisfied_by(PairingLevel.SAME_DONOR)
        assert not requirement.is_satisfied_by(PairingLevel.STUDY_LEVEL)

    def test_unset_observed_level_is_not_satisfied(self):
        requirement = PairingRequirement(acceptable_levels=[PairingLevel.SAME_CELL])
        assert not requirement.is_satisfied_by(None)


class TestCompilerExpressesPairing:
    @pytest.mark.parametrize(
        "question",
        [
            "配对的scATAC-seq多组学数据",
            "同细胞同时测量染色质可及性与转录组",
            "paired same-cell multiome RNA and ATAC",
            "10x Multiome joint profiling",
        ],
    )
    def test_paired_phrasing_sets_a_same_cell_requirement(self, question):
        requirement = RuleBasedRequirementCompiler().compile(question)
        assert requirement.minimum_pairing_level == PairingLevel.SAME_CELL

    def test_paired_questions_stop_recalling_standalone_scatac(self):
        """The defect: asking for paired data still admitted standalone scATAC.

        `pairing_requirement` being set is what makes
        `applicable_bundle_schemas_by_layers` exclude the relationship-gated
        `scatac_standalone` schema downstream -- see
        TestApplicableBundleSchemasByLayers in test_requirements.py."""
        requirement = RuleBasedRequirementCompiler().compile("配对的scATAC-seq多组学数据")
        assert requirement.pairing_requirement is not None

    def test_paired_phrasing_also_sets_the_acceptable_levels_allow_list(self):
        """pairing_requirement is the hard-gate successor to minimum_pairing_level
        and must be populated in parallel, as an explicit allow-list."""
        requirement = RuleBasedRequirementCompiler().compile("paired same-cell multiome RNA and ATAC")
        assert requirement.pairing_requirement is not None
        assert set(requirement.pairing_requirement.acceptable_levels) == {
            PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS,
        }
        assert requirement.pairing_requirement.is_satisfied_by(PairingLevel.SAME_SAMPLE) is False

    def test_unpaired_question_leaves_pairing_requirement_unset(self):
        requirement = RuleBasedRequirementCompiler().compile("human lung scATAC-seq")
        assert requirement.pairing_requirement is None

    def test_trimodal_phrasing_selects_the_trimodal_modality(self):
        requirement = RuleBasedRequirementCompiler().compile(
            "TEA-seq and DOGMA-seq trimodal data"
        )
        assert OmicsLayer.PROTEIN in requirement.required_omics_layers

    def test_an_unpaired_question_is_left_alone(self):
        requirement = RuleBasedRequirementCompiler().compile("human lung scATAC-seq")
        assert requirement.minimum_pairing_level is None
        assert OmicsLayer.CHROMATIN_ACCESSIBILITY in requirement.required_omics_layers


class TestPairingDrivesTheQuery:
    def test_same_cell_requirement_searches_named_coassay_technologies(self):
        requirement = RuleBasedRequirementCompiler().compile("配对的多组学数据")
        query = QueryPlanner().plan(requirement).queries[0].query
        for technology in ("SNARE-seq", "SHARE-seq", "sci-CAR", "Paired-seq"):
            assert technology in query
        # The generic blob would have recalled standalone ATAC alongside.
        assert "single-cell ATAC-seq" not in query

    def test_an_unpaired_question_keeps_the_broad_query(self):
        requirement = RuleBasedRequirementCompiler().compile("human lung scATAC-seq")
        query = QueryPlanner().plan(requirement).queries[0].query
        assert "single-cell ATAC-seq" in query


class TestSchemasNoLongerConflate:
    def test_matched_samples_have_their_own_schema(self):
        matched = applicable_bundle_schemas_by_layers(
            {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME},
            PairingLevel.SAME_SAMPLE,
        )
        ids = {item.schema_id for item in matched}
        assert "matched_scrna_scatac" in ids
        # Separate assays must not pass as same-cell multiome.
        assert "multiome_same_cell" not in ids

    def test_trimodal_requires_the_protein_layer(self):
        schema = next(
            item for item in BUNDLE_SCHEMAS if item.schema_id == "multiome_trimodal"
        )
        assert not schema.is_satisfied_by(
            {DataRole.FRAGMENTS, DataRole.RNA_MATRIX, DataRole.CELL_METADATA}
        )
        assert schema.is_satisfied_by(
            {
                DataRole.FRAGMENTS, DataRole.RNA_MATRIX,
                DataRole.PROTEIN_MATRIX, DataRole.CELL_METADATA,
            }
        )


def _preflight_context(tmp_path, pairing_level, required):
    profile = AssayProfile(
        label="rna_atac_coassay",
        omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        pairing_level=pairing_level,
        rationale="inferred from source text",
    )
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        assay_profiles=[profile],
    )
    requirement = ResearchRequirement(
        question="paired multiome",
        required_omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        minimum_pairing_level=required,
    )
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=[candidate], feasibility_reports=[],
    )
    asset = DataAsset(
        asset_id="asset_1", candidate_id="cand_1", role=DataRole.FRAGMENTS,
        file_name="atac_fragments.tsv.gz",
        locations=[AssetLocation(
            url="https://ftp.ncbi.nlm.nih.gov/geo/f.tsv.gz",
            protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_VERIFIED,
            availability=AssetAvailability.OBSERVED, size_bytes=10,
        )],
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="cand_1", bundle_type="processed",
        asset_ids=["asset_1"], applicable_schema_ids=["multiome_same_cell"],
        satisfied_schema_ids=["multiome_same_cell"],
    )
    summary = RemoteBundleInventorySummary(
        summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
        access_status=BundleAccessStatus.VERIFIED, asset_count=1,
        verified_asset_count=1, known_bytes=10, total_bytes=10,
        reported_layers=[
            OmicsLayer.CHROMATIN_ACCESSIBILITY.value, OmicsLayer.TRANSCRIPTOME.value,
        ],
    )
    database = tmp_path / "pairing.sqlite"
    context = AgentContext(
        run_id="run_1", question="paired multiome", database_path=str(database),
        ledger=CollaborationLedger(database), policy={"use_llm": False},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(
        artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT, {
            "assets": [asset.model_dump(mode="json")],
            "bundles": [bundle.model_dump(mode="json")],
            "bundle_summaries": [summary.model_dump(mode="json")],
        })
    )
    return context


@pytest.mark.asyncio
async def test_study_level_candidate_is_flagged_when_same_cell_is_required(tmp_path):
    """The headline fix: study-level co-reporting must no longer pass."""
    context = _preflight_context(
        tmp_path, PairingLevel.STUDY_LEVEL, PairingLevel.SAME_CELL
    )
    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == GateStatus.WARN
    assert "pairing:unverified" in report["warnings"]
    assert "study_level" in report["rationale"]["pairing"]


@pytest.mark.asyncio
async def test_same_nucleus_candidate_passes(tmp_path):
    context = _preflight_context(
        tmp_path, PairingLevel.SAME_NUCLEUS, PairingLevel.SAME_CELL
    )
    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == GateStatus.PASS
    assert "pairing:unverified" not in report["warnings"]


@pytest.mark.asyncio
async def test_no_pairing_requirement_leaves_study_level_alone(tmp_path):
    """Questions that never asked for pairing keep their previous behaviour."""
    context = _preflight_context(tmp_path, PairingLevel.STUDY_LEVEL, None)
    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == GateStatus.PASS
    assert "pairing:unverified" not in report["warnings"]


@pytest.mark.asyncio
async def test_unverifiable_pairing_is_not_silently_accepted(tmp_path):
    """A candidate with no assay profile must not pass as paired.

    The first acceptance run recommended three such bundles: pairing was
    required, no profile could be inferred, and nothing flagged it. Unknown is
    not absent, but it is not a pass either.
    """
    context = _preflight_context(
        tmp_path, PairingLevel.STUDY_LEVEL, PairingLevel.SAME_CELL
    )
    # Strip the inferred profile so pairing becomes genuinely unknowable.
    context.discovery.candidates[0].assay_profiles.clear()
    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert report["dimensions"]["pairing_evidence"] == GateStatus.UNKNOWN
    assert "pairing:unknown" in report["warnings"]


@pytest.mark.asyncio
async def test_unknown_pairing_is_ignored_when_pairing_was_not_required(tmp_path):
    context = _preflight_context(tmp_path, PairingLevel.STUDY_LEVEL, None)
    context.discovery.candidates[0].assay_profiles.clear()
    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert "pairing:unknown" not in report["warnings"]


class TestPairingOrthogonalToDataAvailability:
    """Data availability (bundle.complete) and scientific relationship
    eligibility (pairing_gate) must be independent gates. A bundle whose only
    applicable schema is `raw_reads` -- files exist, nothing processed yet --
    is acquisition-worthy, but raw reads alone prove nothing about pairing, so
    an unresolved pairing requirement must still block PREFLIGHT_READY even
    when the bundle is otherwise "complete"."""

    @pytest.mark.asyncio
    async def test_pairing_insufficiency_blocks_preflight_ready_despite_a_complete_bundle(
        self, tmp_path
    ):
        context = _preflight_context(
            tmp_path, PairingLevel.STUDY_LEVEL, PairingLevel.SAME_CELL
        )
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["dimensions"]["semantic_bundle"] == GateStatus.PASS  # bundle.complete
        assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value

    @pytest.mark.asyncio
    async def test_unresolvable_pairing_also_blocks_preflight_ready(self, tmp_path):
        context = _preflight_context(
            tmp_path, PairingLevel.STUDY_LEVEL, PairingLevel.SAME_CELL
        )
        context.discovery.candidates[0].assay_profiles.clear()
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value

    @pytest.mark.asyncio
    async def test_satisfied_pairing_still_reaches_preflight_ready(self, tmp_path):
        context = _preflight_context(
            tmp_path, PairingLevel.SAME_NUCLEUS, PairingLevel.SAME_CELL
        )
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["decision"] == BundleStatus.PREFLIGHT_READY.value

    @pytest.mark.asyncio
    async def test_raw_reads_only_bundle_does_not_bypass_an_unresolved_pairing_requirement(
        self, tmp_path
    ):
        """The exact scenario this class exists to close off: RNA+ATAC
        observed, pairing unresolved (study-level co-reporting), only raw
        FASTQ available. `bundle_shape_for` correctly offers only the
        `raw_reads` schema (real layer/relationship selection, not a
        hand-set bundle fixture) and it is trivially satisfied -- but that
        must not translate into PREFLIGHT_READY."""
        profile = AssayProfile(
            label="rna_atac_co_reported",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.STUDY_LEVEL,
            rationale="RNA and ATAC co-reported without a stated relationship",
        )
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study", assay_profiles=[profile],
        )
        raw_asset = DataAsset(
            asset_id="asset_1", candidate_id="cand_1", role=DataRole.RAW_READS,
            file_name="reads.fastq.gz",
            locations=[AssetLocation(
                url="https://ftp.ncbi.nlm.nih.gov/geo/reads.fastq.gz",
                protocol=TransferProtocol.HTTPS,
                access_level=AccessLevel.PUBLIC_VERIFIED,
                availability=AssetAvailability.OBSERVED, size_bytes=10,
            )],
        )
        requirement = ResearchRequirement(
            question="paired multiome",
            pairing_requirement=PairingRequirement(
                acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
            ),
        )
        shape = bundle_shape_for(candidate, [raw_asset], requirement)
        assert shape.applicable_schema_ids == ["raw_reads"]

        bundle = SemanticBundle(
            bundle_id="bundle_1", candidate_id="cand_1", bundle_type=shape.bundle_type,
            asset_ids=["asset_1"], applicable_schema_ids=shape.applicable_schema_ids,
            satisfied_schema_ids=shape.satisfied_schema_ids,
        )
        assert bundle.complete  # raw_reads is trivially satisfied by a raw asset

        discovery = DiscoveryRunResult(
            run_id="run_1", requirement=requirement,
            query_plan=QueryPlan(requirement=requirement, queries=[]),
            source_records=[], candidates=[candidate], feasibility_reports=[],
        )
        summary = RemoteBundleInventorySummary(
            summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
            access_status=BundleAccessStatus.VERIFIED, asset_count=1,
            verified_asset_count=1, known_bytes=10, total_bytes=10,
        )
        database = tmp_path / "raw_reads_pairing.sqlite"
        context = AgentContext(
            run_id="run_1", question="paired multiome", database_path=str(database),
            ledger=CollaborationLedger(database), policy={"use_llm": False},
            requirement=requirement, discovery=discovery,
        )
        context.artifacts.append(
            artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT, {
                "assets": [raw_asset.model_dump(mode="json")],
                "bundles": [bundle.model_dump(mode="json")],
                "bundle_summaries": [summary.model_dump(mode="json")],
            })
        )
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value
        assert "pairing:unverified" in report["warnings"]


def _evidence_completeness_context(tmp_path, pairing_claim_evidence_class):
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        assay_profiles=[AssayProfile(
            label="x",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_CELL, rationale="x",
        )],
    )
    requirement = ResearchRequirement(
        question="paired multiome",
        profile_id=PAIRED_ATAC_RNA_HUMAN.profile_id,
        pairing_requirement=PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    )
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=[candidate], feasibility_reports=[],
    )
    asset = DataAsset(
        asset_id="asset_1", candidate_id="cand_1", role=DataRole.FRAGMENTS,
        file_name="atac_fragments.tsv.gz",
        locations=[AssetLocation(
            url="https://ftp.ncbi.nlm.nih.gov/geo/f.tsv.gz", protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_VERIFIED,
            availability=AssetAvailability.OBSERVED, size_bytes=10,
        )],
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="cand_1", pair_unit_id="pair_unit_1",
        bundle_type="processed", asset_ids=["asset_1"],
        applicable_schema_ids=["multiome_same_cell"], satisfied_schema_ids=["multiome_same_cell"],
    )
    summary = RemoteBundleInventorySummary(
        summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
        access_status=BundleAccessStatus.VERIFIED, asset_count=1,
        verified_asset_count=1, known_bytes=10, total_bytes=10,
        reported_layers=[
            OmicsLayer.CHROMATIN_ACCESSIBILITY.value, OmicsLayer.TRANSCRIPTOME.value,
        ],
        genome_builds=["GRCh38"],
    )
    pairing_claim = EvidenceClaim(
        claim_id=stable_id("claim", "pairing"), subject_id="pair_unit_1",
        predicate="has_pair_relationship", value=PairingLevel.SAME_CELL.value,
        evidence_class=pairing_claim_evidence_class,
    )
    layer_claims = [
        EvidenceClaim(
            claim_id=stable_id("claim", "atac_layer"), subject_id="cand_1",
            predicate="has_omics_layer:chromatin_accessibility", value=True,
            evidence_class=EvidenceClass.REPOSITORY_REPORTED,
        ),
        EvidenceClaim(
            claim_id=stable_id("claim", "rna_layer"), subject_id="cand_1",
            predicate="has_omics_layer:transcriptome", value=True,
            evidence_class=EvidenceClass.REPOSITORY_REPORTED,
        ),
    ]
    database = tmp_path / "evidence_completeness.sqlite"
    context = AgentContext(
        run_id="run_1", question="paired multiome", database_path=str(database),
        ledger=CollaborationLedger(database), policy={"use_llm": False},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(
        artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT, {
            "assets": [asset.model_dump(mode="json")],
            "bundles": [bundle.model_dump(mode="json")],
            "bundle_summaries": [summary.model_dump(mode="json")],
            "pairing_claims": [pairing_claim.model_dump(mode="json")],
            "layer_claims": [item.model_dump(mode="json") for item in layer_claims],
        })
    )
    return context


class TestEvidenceCompletenessGatesAcquisition:
    """Phase 5A's headline behavior change: today, a has_pair_relationship
    claim's evidence_class was never checked, only its PairingLevel value
    against the pairing requirement. A MODEL_INFERRED claim (PairingVerifier's
    LLM tier 3) must now correctly fail the profile's REPOSITORY_REPORTED
    acquisition floor, exactly as `RequiredEvidence.notes` already promises."""

    @pytest.mark.asyncio
    async def test_model_inferred_pairing_evidence_never_reaches_preflight_ready(self, tmp_path):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.MODEL_INFERRED)
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["dimensions"]["evidence_completeness"] == GateStatus.FAIL
        assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value

    @pytest.mark.asyncio
    async def test_repository_reported_pairing_evidence_still_reaches_preflight_ready(self, tmp_path):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.REPOSITORY_REPORTED)
        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["dimensions"]["evidence_completeness"] == GateStatus.PASS
        assert report["decision"] == BundleStatus.PREFLIGHT_READY.value

    @pytest.mark.asyncio
    async def test_audit_task_names_the_missing_predicate(self, tmp_path):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.MODEL_INFERRED)
        payload = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload

        audit_tasks = payload["audit_tasks"]
        assert len(audit_tasks) == 1
        assert audit_tasks[0]["blocking_category"] == "has_pair_relationship"
        assert audit_tasks[0]["category_family"] == "pairing"
        assert audit_tasks[0]["subject_id"] == "pair_unit_1"
        # A blank audit_method/steps/pass_criteria looks like a resolved
        # audit row with nothing left to do; a curator reading 04_Audit_Hold
        # needs an actual checklist, not silence.
        assert audit_tasks[0]["audit_method"]
        assert audit_tasks[0]["audit_steps"]
        assert audit_tasks[0]["pass_criteria"]

    @pytest.mark.asyncio
    async def test_candidate_missing_an_assay_profile_entirely_needs_evidence(self, tmp_path):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.REPOSITORY_REPORTED)
        context.discovery.candidates[0].assay_profiles.clear()
        # Strip the layer_claims artifact payload too -- no AssayProfile means
        # InventoryReconcileAgent would never have produced them either.
        inventory_artifact = context.artifacts[0]
        inventory_artifact.payload["layer_claims"] = []

        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["dimensions"]["evidence_completeness"] == GateStatus.FAIL
        assert report["decision"] == BundleStatus.NEEDS_EVIDENCE.value


class TestProfileScopeExclusion:
    """Round 5 correction: a profile-schema mismatch on a pair-unit-resolved
    bundle is a hard, terminal exclusion from *this profile's* corpus -- it
    must never silently fall back to the legacy candidate-level pairing_gate,
    which reads assay_profiles unscoped by any profile and could wrongly
    admit the bundle as PREFLIGHT_READY on an assay-level pairing prior this
    profile never asked about."""

    @pytest.mark.asyncio
    async def test_a_bundle_outside_the_profiles_schemas_is_rejected_not_defaulted_to_legacy(
        self, tmp_path
    ):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.REPOSITORY_REPORTED)
        inventory_artifact = context.artifacts[0]
        bundle_payload = inventory_artifact.payload["bundles"][0]
        # multiome_same_cell -> scatac_standalone: still pair-unit-resolved,
        # but no longer claiming any of PAIRED_ATAC_RNA_HUMAN's own schemas.
        bundle_payload["applicable_schema_ids"] = ["scatac_standalone"]
        bundle_payload["satisfied_schema_ids"] = ["scatac_standalone"]

        report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

        assert report["decision"] == BundleStatus.REJECTED.value
        assert "evidence_completeness" not in report["dimensions"]
        assert report["dimensions"]["profile_scope"] == GateStatus.FAIL

    @pytest.mark.asyncio
    async def test_out_of_scope_audit_task_names_the_profile_not_a_missing_predicate(
        self, tmp_path
    ):
        context = _evidence_completeness_context(tmp_path, EvidenceClass.REPOSITORY_REPORTED)
        inventory_artifact = context.artifacts[0]
        bundle_payload = inventory_artifact.payload["bundles"][0]
        bundle_payload["applicable_schema_ids"] = ["scatac_standalone"]
        bundle_payload["satisfied_schema_ids"] = ["scatac_standalone"]

        payload = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload
        audit_tasks = payload["audit_tasks"]

        assert len(audit_tasks) == 1
        assert audit_tasks[0]["blocking_category"] == (
            f"profile:out_of_scope:{PAIRED_ATAC_RNA_HUMAN.profile_id}"
        )
        assert audit_tasks[0]["queue_action"] == "excluded"


@pytest.mark.asyncio
async def test_standalone_atac_bundle_fails_the_layer_gate_for_a_joint_requirement(
    tmp_path,
):
    """The same P0.0-shaped regression as
    test_feasibility.py::TestJointLayerRequirementIsNotSatisfiedByOneLayer,
    pinned here for progressive_preflight's own layer_gate: a bundle that
    only reports the ATAC layer must not pass a same-cell-multiome
    requirement's layer_gate just because ATAC is one of the two requested
    layers -- the requirement asks for both together."""
    requirement = ResearchRequirement(
        question="paired same-cell multiome RNA and ATAC",
        required_omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        pairing_requirement=PairingRequirement(
            acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
        ),
    )
    candidate = DatasetCandidate(candidate_id="cand_1", title="Standalone snATAC-seq study")
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        source_records=[], candidates=[candidate], feasibility_reports=[],
    )
    asset = DataAsset(
        asset_id="asset_1", candidate_id="cand_1", role=DataRole.FRAGMENTS,
        file_name="atac_fragments.tsv.gz",
        locations=[AssetLocation(
            url="https://ftp.ncbi.nlm.nih.gov/geo/f.tsv.gz", protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_VERIFIED,
            availability=AssetAvailability.OBSERVED, size_bytes=10,
        )],
    )
    bundle = SemanticBundle(
        bundle_id="bundle_1", candidate_id="cand_1", bundle_type="processed",
        asset_ids=["asset_1"], applicable_schema_ids=["scatac_standalone"],
        satisfied_schema_ids=["scatac_standalone"],
    )
    summary = RemoteBundleInventorySummary(
        summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
        access_status=BundleAccessStatus.VERIFIED, asset_count=1,
        verified_asset_count=1, known_bytes=10, total_bytes=10,
        reported_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY.value],
    )
    database = tmp_path / "layer_gate_joint.sqlite"
    context = AgentContext(
        run_id="run_1", question=requirement.question, database_path=str(database),
        ledger=CollaborationLedger(database), policy={"use_llm": False},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(
        artifact("run_1", ArtifactKind.ASSET_INVENTORY, AgentName.ASSET_SCOUT, {
            "assets": [asset.model_dump(mode="json")],
            "bundles": [bundle.model_dump(mode="json")],
            "bundle_summaries": [summary.model_dump(mode="json")],
        })
    )

    report = (await ProgressivePreflightAssessmentAgent().run(context))[0].payload["reports"][0]

    assert report["dimensions"]["layer_authenticity"] == GateStatus.FAIL
    assert "modality:mismatch" in report["hard_blockers"]
