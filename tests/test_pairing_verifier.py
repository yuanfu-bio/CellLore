"""PairingVerifier: only this component may produce a has_pair_relationship
claim, and only in the evidence-strength order R06 describes."""

import json

import pytest

from scdata_agent.application.agents.pairing_verifier import PairingVerifier
from scdata_agent.domain.lifecycle import EvidenceClaim
from scdata_agent.domain.models import (
    AssayProfile,
    DatasetCandidate,
    EvidenceClass,
    GeoSampleRecord,
    PairingLevel,
    ProfileBasis,
)
from scdata_agent.domain.pair_units import ProposedPairUnit


class _FakeClient:
    def __init__(self, response: str | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


class _StructuredFakeClient(_FakeClient):
    def __init__(self, response: str):
        super().__init__(response=response)
        self.structured_kwargs = None

    async def complete_structured(self, messages, **kwargs):
        self.calls += 1
        self.structured_kwargs = kwargs
        return self.response


def _pair_unit(member_samples=("GSM1", "GSM2"), evidence_ids=("ev1", "ev2"), is_resolved=True):
    return ProposedPairUnit(
        proposed_unit_id="unit_1", candidate_id="cand_1",
        member_assets=["a1", "a2"], member_samples=list(member_samples),
        grouping_rationale="Both titles name HF1.", evidence_ids=list(evidence_ids),
        is_resolved=is_resolved,
    )


def _sample(gsm, raw_text, evidence_ids=None):
    return GeoSampleRecord(
        gsm_accession=gsm, series_accession="GSE1", raw_text=raw_text,
        evidence_ids=evidence_ids or [f"ev_{gsm}"],
    )


@pytest.mark.asyncio
class TestTier1NamedTechnology:
    async def test_named_technology_pairing_wins_without_any_llm_call(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="10x Multiome study",
            assay_profiles=[AssayProfile(
                label="10x_multiome_rna_atac",
                pairing_level=PairingLevel.SAME_NUCLEUS,
                inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
                rationale="Named co-assay technology was observed.",
            )],
        )
        client = _FakeClient(response="should never be called")
        verifier = PairingVerifier(client=client)

        claim = await verifier.verify(_pair_unit(), candidate, [])

        assert claim is not None
        assert claim.value == PairingLevel.SAME_NUCLEUS.value
        assert claim.evidence_class == EvidenceClass.REPOSITORY_REPORTED
        assert client.calls == 0

    async def test_layer_inference_profile_is_not_treated_as_named_technology(self):
        """Only a NAMED_TECHNOLOGY-basis profile counts for tier 1 -- a
        merely-inferred layer combination is a weaker claim."""
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="study",
            assay_profiles=[AssayProfile(
                label="rna_atac_coassay",
                pairing_level=PairingLevel.SAME_CELL,
                inferred_from=ProfileBasis.LAYER_INFERENCE,
                rationale="Inferred from separate layer mentions.",
            )],
        )
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(_pair_unit(member_samples=(), evidence_ids=()), candidate, [])

        assert claim is None


@pytest.mark.asyncio
class TestTier2ExplicitStatement:
    async def test_explicit_same_cell_statement_in_member_sample_text(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        samples = [
            _sample("GSM1", "!Sample_title = same nucleus multiome sample"),
            _sample("GSM2", "!Sample_title = paired ATAC file"),
        ]
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(_pair_unit(), candidate, samples)

        assert claim is not None
        assert claim.value == PairingLevel.SAME_NUCLEUS.value
        assert claim.evidence_class == EvidenceClass.REPOSITORY_REPORTED
        assert set(claim.evidence_ids) == {"ev_GSM1", "ev_GSM2"}

    async def test_statement_in_a_sample_outside_this_unit_does_not_leak_in(self):
        """A same-cell statement belonging to a *different* pair unit in the
        same series must not support this unit's claim."""
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        outside_sample = _sample("GSM99", "!Sample_title = same nucleus multiome sample")
        member_samples_without_statement = [
            _sample("GSM1", "!Sample_title = plain ATAC sample"),
            _sample("GSM2", "!Sample_title = plain RNA sample"),
        ]
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(
            _pair_unit(), candidate, [*member_samples_without_statement, outside_sample]
        )

        # _explicit_statement_relationship only receives samples whose
        # gsm_accession is in pair_unit.member_samples -- GSM99 is filtered
        # out by the verifier itself before this check runs.
        assert claim is None


@pytest.mark.asyncio
class TestTier3LLMInference:
    async def test_llm_inference_only_runs_when_tiers_1_and_2_are_empty(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        samples = [_sample("GSM1", "no relationship stated"), _sample("GSM2", "no relationship stated")]
        response = json.dumps({
            "pairing_level": "same_cell", "confidence": 0.4,
            "rationale": "Inferred from indirect phrasing.",
        })
        client = _FakeClient(response=response)
        verifier = PairingVerifier(client=client)

        claim = await verifier.verify(_pair_unit(), candidate, samples)

        assert client.calls == 1
        assert claim is not None
        assert claim.evidence_class == EvidenceClass.MODEL_INFERRED
        assert claim.value == PairingLevel.SAME_CELL.value
        assert claim.rationale == "Inferred from indirect phrasing."

    async def test_llm_inference_prefers_small_structured_completion(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        samples = [_sample("GSM1", "no relationship stated")]
        client = _StructuredFakeClient(json.dumps({
            "pairing_level": "unverified",
            "confidence": 0.7,
            "rationale": "No physical pairing statement was found.",
        }))
        verifier = PairingVerifier(client=client)

        claim = await verifier.verify(
            _pair_unit(member_samples=("GSM1",)), candidate, samples
        )

        assert claim is not None
        assert client.calls == 1
        assert client.structured_kwargs["schema_name"] == "PairingInference"
        assert client.structured_kwargs["max_tokens"] == 800

    async def test_llm_error_yields_no_claim_not_a_crash(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        samples = [_sample("GSM1", "no relationship stated")]
        verifier = PairingVerifier(client=_FakeClient(error=RuntimeError("boom")))

        claim = await verifier.verify(_pair_unit(member_samples=("GSM1",)), candidate, samples)

        assert claim is None


@pytest.mark.asyncio
async def test_no_evidence_and_no_client_yields_no_claim():
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    verifier = PairingVerifier(client=None)

    claim = await verifier.verify(_pair_unit(member_samples=(), evidence_ids=()), candidate, [])

    assert claim is None


@pytest.mark.asyncio
async def test_model_inferred_evidence_alone_is_recorded_as_the_weaker_class():
    """MODEL_INFERRED must never look identical to REPOSITORY_REPORTED --
    downstream evidence-completeness gating (Phase 5) depends on this
    distinction to refuse MODEL_INFERRED alone as sufficient."""
    candidate = DatasetCandidate(candidate_id="cand_1", title="study")
    samples = [_sample("GSM1", "no relationship stated")]
    response = json.dumps({"pairing_level": "same_nucleus", "confidence": 0.9})
    verifier = PairingVerifier(client=_FakeClient(response=response))

    claim = await verifier.verify(_pair_unit(member_samples=("GSM1",)), candidate, samples)

    assert isinstance(claim, EvidenceClaim)
    assert claim.evidence_class != EvidenceClass.REPOSITORY_REPORTED
    assert claim.evidence_class == EvidenceClass.MODEL_INFERRED


@pytest.mark.asyncio
class TestProvisionalUnitsAreRefusedOutright:
    """The exact wrong-admission scenario this guard exists to close: PairUnit
    resolution fails or never runs -> every asset kept in one undifferentiated
    provisional unit -> the study happens to be a named same-cell technology
    -> that assay-level prior must NOT be allowed to auto-grant the specific,
    never-verified grouping a same-cell claim."""

    async def test_named_technology_does_not_apply_to_a_provisional_unit(self):
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="10x Multiome study, six patients",
            assay_profiles=[AssayProfile(
                label="10x_multiome_rna_atac",
                pairing_level=PairingLevel.SAME_NUCLEUS,
                inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
                rationale="Named co-assay technology was observed.",
            )],
        )
        provisional = _pair_unit(is_resolved=False)
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(provisional, candidate, [])

        assert claim is None

    async def test_explicit_statement_does_not_apply_to_a_provisional_unit(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        samples = [_sample("GSM1", "!Sample_title = same nucleus multiome sample")]
        provisional = _pair_unit(member_samples=("GSM1",), is_resolved=False)
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(provisional, candidate, samples)

        assert claim is None

    async def test_llm_inference_is_never_attempted_for_a_provisional_unit(self):
        candidate = DatasetCandidate(candidate_id="cand_1", title="study")
        client = _FakeClient(response="should never be called")
        provisional = _pair_unit(is_resolved=False)
        verifier = PairingVerifier(client=client)

        claim = await verifier.verify(provisional, candidate, [])

        assert claim is None
        assert client.calls == 0

    async def test_resolved_unit_from_the_same_study_is_still_verified_normally(self):
        """The guard is per-unit, not per-study -- a resolved sibling unit
        must still be verifiable even if another unit from the same study is
        provisional."""
        candidate = DatasetCandidate(
            candidate_id="cand_1", title="10x Multiome study",
            assay_profiles=[AssayProfile(
                label="10x_multiome_rna_atac",
                pairing_level=PairingLevel.SAME_NUCLEUS,
                inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
                rationale="Named co-assay technology was observed.",
            )],
        )
        resolved = _pair_unit(is_resolved=True)
        verifier = PairingVerifier(client=None)

        claim = await verifier.verify(resolved, candidate, [])

        assert claim is not None
        assert claim.value == PairingLevel.SAME_NUCLEUS.value
