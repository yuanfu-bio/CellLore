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
from scdata_agent.domain.lifecycle import SemanticBundle
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    GateStatus,
    Modality,
    PairingLevel,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
    best_pairing_level,
    pairing_satisfies,
)
from scdata_agent.domain.requirements import (
    BUNDLE_SCHEMAS,
    QueryPlanner,
    RuleBasedRequirementCompiler,
    applicable_bundle_schemas,
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
        """The defect: asking for paired data still admitted standalone scATAC."""
        requirement = RuleBasedRequirementCompiler().compile("配对的scATAC-seq多组学数据")
        assert Modality.SCATAC not in requirement.modalities

    def test_trimodal_phrasing_selects_the_trimodal_modality(self):
        requirement = RuleBasedRequirementCompiler().compile(
            "TEA-seq and DOGMA-seq trimodal data"
        )
        assert Modality.MULTIOME_RNA_ATAC_PROTEIN in requirement.modalities

    def test_an_unpaired_question_is_left_alone(self):
        requirement = RuleBasedRequirementCompiler().compile("human lung scATAC-seq")
        assert requirement.minimum_pairing_level is None
        assert Modality.SCATAC in requirement.modalities


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
        matched = applicable_bundle_schemas([Modality.MATCHED_SCRNA_SCATAC])
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
        modalities=[Modality.MULTIOME_RNA_ATAC],
        pairing_level=pairing_level,
        rationale="inferred from source text",
    )
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="study",
        modalities=[Modality.MULTIOME_RNA_ATAC], assay_profiles=[profile],
    )
    requirement = ResearchRequirement(
        question="paired multiome",
        modalities=[Modality.MULTIOME_RNA_ATAC],
        acceptable_modalities=[Modality.MULTIOME_RNA_ATAC],
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
        reported_modalities=[Modality.MULTIOME_RNA_ATAC.value],
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
