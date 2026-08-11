"""omics_layer_claims / resolved_assembly_claim in isolation: the two new
EvidenceClaim producers closing the "no producer yet" gap for
has_omics_layer:*/resolved_assembly. Both stay REPOSITORY_REPORTED --
FILE_OBSERVED-level evidence is Phase 5B's job once files are in hand.
"""

from celllore.application.resource_pipeline import (
    omics_layer_claims,
    resolved_assembly_claim,
)
from celllore.domain.lifecycle import SemanticBundle
from celllore.domain.models import (
    AssayProfile,
    DataAsset,
    DataRole,
    DatasetCandidate,
    EvidenceClass,
    OmicsLayer,
    ResearchRequirement,
)
from celllore.domain.resource_pipeline import BundleAccessStatus, RemoteBundleInventorySummary


class TestOmicsLayerClaims:
    def test_a_declared_layer_produces_one_repository_reported_claim(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study",
            assay_profiles=[AssayProfile(
                label="x",
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
                rationale="x", evidence_ids=["ev1"], confidence=0.9,
            )],
        )
        claims = omics_layer_claims(candidate)

        assert len(claims) == 1
        assert claims[0].predicate == "has_omics_layer:chromatin_accessibility"
        assert claims[0].subject_id == "cand_1"
        assert claims[0].evidence_class == EvidenceClass.REPOSITORY_REPORTED
        assert claims[0].evidence_ids == ["ev1"]
        assert claims[0].confidence == 0.9

    def test_no_assay_profiles_produces_zero_claims_not_a_fabricated_one(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        assert omics_layer_claims(candidate) == []

    def test_repository_manifest_roles_supply_missing_layer_claims(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        assets = [
            DataAsset(
                asset_id="rna", candidate_id="cand_1", role=DataRole.RNA_MATRIX,
                file_name="raw_feature_bc_matrix.h5",
            ),
            DataAsset(
                asset_id="atac", candidate_id="cand_1", role=DataRole.FRAGMENTS,
                file_name="atac_fragments.tsv.gz",
            ),
        ]

        claims = omics_layer_claims(candidate, assets=assets)

        assert {item.predicate for item in claims} == {
            "has_omics_layer:chromatin_accessibility",
            "has_omics_layer:transcriptome",
        }
        assert all(item.evidence_class == EvidenceClass.REPOSITORY_REPORTED for item in claims)
        assert all("Repository-listed assets" in item.rationale for item in claims)

    def test_multiple_layers_produce_one_claim_each(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study",
            assay_profiles=[AssayProfile(
                label="x",
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
                rationale="x",
            )],
        )
        predicates = {item.predicate for item in omics_layer_claims(candidate)}
        assert predicates == {
            "has_omics_layer:chromatin_accessibility", "has_omics_layer:transcriptome",
        }

    def test_narrowed_by_required_omics_layers_like_candidate_layers(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study",
            assay_profiles=[AssayProfile(
                label="x",
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
                rationale="x",
            )],
        )
        requirement = ResearchRequirement(
            question="q", required_omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
        )
        predicates = {item.predicate for item in omics_layer_claims(candidate, requirement)}
        assert predicates == {"has_omics_layer:chromatin_accessibility"}

    def test_confidence_is_the_strongest_profile_for_that_layer(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study",
            assay_profiles=[
                AssayProfile(
                    label="weak",
                    omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
                    rationale="x", confidence=0.5,
                ),
                AssayProfile(
                    label="strong",
                    omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
                    rationale="x", confidence=0.95,
                ),
            ],
        )
        claims = omics_layer_claims(candidate)
        assert claims[0].confidence == 0.95


class TestResolvedAssemblyClaim:
    def _bundle(self):
        return SemanticBundle(
            bundle_id="bundle_1", candidate_id="cand_1", bundle_type="processed",
            evidence_ids=["ev1"],
        )

    def test_no_summary_yields_no_claim(self):
        assert resolved_assembly_claim(self._bundle(), None) is None

    def test_empty_genome_builds_yields_no_claim(self):
        summary = RemoteBundleInventorySummary(
            summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
            access_status=BundleAccessStatus.VERIFIED, asset_count=1,
            verified_asset_count=1, known_bytes=10, total_bytes=10,
            genome_builds=[],
        )
        assert resolved_assembly_claim(self._bundle(), summary) is None

    def test_declared_build_produces_a_bundle_scoped_claim(self):
        summary = RemoteBundleInventorySummary(
            summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
            access_status=BundleAccessStatus.VERIFIED, asset_count=1,
            verified_asset_count=1, known_bytes=10, total_bytes=10,
            genome_builds=["GRCh38"],
        )
        claim = resolved_assembly_claim(self._bundle(), summary)

        assert claim is not None
        assert claim.subject_id == "bundle_1"
        assert claim.predicate == "resolved_assembly"
        assert claim.value == ["GRCh38"]
        assert claim.evidence_class == EvidenceClass.REPOSITORY_REPORTED

    def test_multiple_builds_surface_the_full_ambiguity_not_just_the_first(self):
        summary = RemoteBundleInventorySummary(
            summary_id="s1", bundle_id="bundle_1", candidate_id="cand_1",
            access_status=BundleAccessStatus.VERIFIED, asset_count=2,
            verified_asset_count=2, known_bytes=10, total_bytes=10,
            genome_builds=["GRCh37", "GRCh38"],
        )
        claim = resolved_assembly_claim(self._bundle(), summary)
        assert claim.value == ["GRCh37", "GRCh38"]
