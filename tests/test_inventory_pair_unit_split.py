"""InventoryReconcileAgent builds one bundle per resolved pair unit.

The gap this closes: a six-patient study reconciled to one undifferentiated
bundle no matter how many independent RNA+ATAC pairings it actually
contained. With PairUnitResolver/PairingVerifier wired in, a two-patient
study now reconciles to two independently-judgeable bundles.
"""

import pytest

from scdata_agent.application.agents.inventory_reconcile import (
    InventoryReconcileAgent,
)
from scdata_agent.application.agents.pair_unit_resolver import (
    PairUnitResolutionDraft,
    PairUnitResolver,
)
from scdata_agent.application.agents.pairing_verifier import PairingVerifier
from scdata_agent.application.agents.shared import AgentContext
from scdata_agent.application.collaboration import (
    ArtifactKind,
    CollaborationLedger,
    artifact,
)
from scdata_agent.domain.models import (
    AccessLevel,
    AssayProfile,
    AssetAvailability,
    AssetLocation,
    DataAsset,
    DataRole,
    DatasetCandidate,
    DiscoveryRunResult,
    GeoSampleRecord,
    OmicsLayer,
    PairingLevel,
    ProfileBasis,
    QueryPlan,
    ResearchRequirement,
    TransferProtocol,
)
from scdata_agent.domain.resource_pipeline import AssetInventoryDelta, ScoutPhase


class _FakeClient:
    def __init__(self, response: str):
        self.response = response

    async def complete(self, messages, *, temperature=0.1, max_tokens=2_000):
        return self.response


def _asset(asset_id: str, candidate_id: str, role: DataRole, file_name: str) -> DataAsset:
    return DataAsset(
        asset_id=asset_id, candidate_id=candidate_id, role=role, file_name=file_name,
        locations=[AssetLocation(
            url=f"https://example.test/{file_name}",
            protocol=TransferProtocol.HTTPS,
            access_level=AccessLevel.PUBLIC_VERIFIED,
            availability=AssetAvailability.OBSERVED, size_bytes=1024,
        )],
    )


def _sample(gsm, title, supplementary_files):
    return GeoSampleRecord(
        gsm_accession=gsm, series_accession="GSE_TEST", title=title,
        supplementary_files=supplementary_files, evidence_ids=[f"ev_{gsm}"],
    )


async def _run(tmp_path, candidate, assets, samples, resolver, verifier, policy=None):
    requirement = ResearchRequirement(question="paired multiome")
    discovery = DiscoveryRunResult(
        run_id="run_1", requirement=requirement,
        query_plan=QueryPlan(requirement=requirement, queries=[]),
        candidates=[candidate], source_records=[], feasibility_reports=[],
    )
    database = tmp_path / "reconcile.sqlite"
    context = AgentContext(
        run_id="run_1", question=requirement.question, database_path=str(database),
        ledger=CollaborationLedger(database),
        # Tests must never touch the network (CONTRIBUTING.md); the probe's
        # own HTTP behavior is exercised offline in
        # test_geo_sample_probe_upgrades_declared_assets_to_verified below.
        policy={"use_llm": False, "remote_probe_enabled": False, **(policy or {})},
        requirement=requirement, discovery=discovery,
    )
    context.artifacts.append(artifact(
        "run_1", ArtifactKind.ASSET_INVENTORY_DELTA, InventoryReconcileAgent.name,
        AssetInventoryDelta(
            inventory_id="delta_1", run_id="run_1", phase=ScoutPhase.DIRECT, assets=assets,
        ).model_dump(mode="json"),
    ))
    context.artifacts.append(artifact(
        "run_1", ArtifactKind.GEO_SAMPLE_METADATA, InventoryReconcileAgent.name,
        {"samples_by_candidate": {
            candidate.candidate_id: [item.model_dump(mode="json") for item in samples]
        }},
    ))
    outputs = await InventoryReconcileAgent(resolver=resolver, verifier=verifier).run(context)
    return outputs[0].payload


@pytest.mark.asyncio
async def test_two_patients_resolve_to_two_independent_bundles(tmp_path):
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="Two-patient paired multiome study",
        assay_profiles=[AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    assets = [
        _asset("a1", "cand_1", DataRole.FRAGMENTS, "GSM1_HF1_atac_fragments.tsv.gz"),
        _asset("a2", "cand_1", DataRole.RNA_MATRIX, "GSM2_HF1_filtered_feature_bc_matrix.h5"),
        _asset("a5", "cand_1", DataRole.CELL_METADATA, "GSM1_HF1_barcodes.tsv.gz"),
        _asset("a3", "cand_1", DataRole.FRAGMENTS, "GSM3_HF2_atac_fragments.tsv.gz"),
        _asset("a4", "cand_1", DataRole.RNA_MATRIX, "GSM4_HF2_filtered_feature_bc_matrix.h5"),
        _asset("a6", "cand_1", DataRole.CELL_METADATA, "GSM3_HF2_barcodes.tsv.gz"),
    ]
    samples = [
        _sample("GSM1", "Sample HF1 ATAC", ["GSM1_HF1_atac_fragments.tsv.gz", "GSM1_HF1_barcodes.tsv.gz"]),
        _sample("GSM2", "Sample HF1 RNA", ["GSM2_HF1_filtered_feature_bc_matrix.h5"]),
        _sample("GSM3", "Sample HF2 ATAC", ["GSM3_HF2_atac_fragments.tsv.gz", "GSM3_HF2_barcodes.tsv.gz"]),
        _sample("GSM4", "Sample HF2 RNA", ["GSM4_HF2_filtered_feature_bc_matrix.h5"]),
    ]
    draft = PairUnitResolutionDraft(units=[
        {"proposed_unit_id": "hf1", "member_samples": ["GSM1", "GSM2"], "grouping_rationale": "Both name HF1."},
        {"proposed_unit_id": "hf2", "member_samples": ["GSM3", "GSM4"], "grouping_rationale": "Both name HF2."},
    ])
    resolver = PairUnitResolver(client=_FakeClient(draft.model_dump_json()))
    verifier = PairingVerifier(client=None)  # tier 1 (named technology) fires deterministically

    payload = await _run(tmp_path, candidate, assets, samples, resolver, verifier)
    bundles = payload["bundles"]

    assert len(bundles) == 2
    pair_unit_ids = {item["pair_unit_id"] for item in bundles}
    assert len(pair_unit_ids) == 2  # distinct, non-null
    assert all(item["pair_unit_id"] for item in bundles)

    by_assets = {frozenset(item["asset_ids"]): item for item in bundles}
    assert frozenset({"a1", "a2", "a5"}) in by_assets
    assert frozenset({"a3", "a4", "a6"}) in by_assets

    # Both units satisfy multiome_same_cell independently -- the physical
    # relationship claim (tier 1, named technology) applies to both.
    for item in bundles:
        assert "multiome_same_cell" in item["satisfied_schema_ids"]

    assert len(payload["proposed_pair_units"]) == 2
    assert len(payload["pairing_claims"]) == 2
    # Candidate-scoped, not per-unit: one candidate declaring two layers
    # produces two layer_claims regardless of how many pair units it split into.
    assert {item["predicate"] for item in payload["layer_claims"]} == {
        "has_omics_layer:chromatin_accessibility", "has_omics_layer:transcriptome",
    }
    assert all(
        claim["evidence_class"] == "repository_reported"
        for claim in payload["pairing_claims"]
    )


@pytest.mark.asyncio
async def test_one_patient_missing_rna_does_not_block_the_other_patients_bundle(tmp_path):
    """The whole point of splitting: an incomplete pair unit must not drag
    down a sibling unit's admission the way one undifferentiated bundle did."""
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="Two-patient study, one incomplete",
        assay_profiles=[AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    assets = [
        _asset("a1", "cand_1", DataRole.FRAGMENTS, "GSM1_HF1_atac_fragments.tsv.gz"),
        _asset("a2", "cand_1", DataRole.RNA_MATRIX, "GSM2_HF1_filtered_feature_bc_matrix.h5"),
        _asset("a5", "cand_1", DataRole.CELL_METADATA, "GSM1_HF1_barcodes.tsv.gz"),
        # HF2 is missing its RNA matrix.
        _asset("a3", "cand_1", DataRole.FRAGMENTS, "GSM3_HF2_atac_fragments.tsv.gz"),
    ]
    samples = [
        _sample("GSM1", "Sample HF1 ATAC", ["GSM1_HF1_atac_fragments.tsv.gz", "GSM1_HF1_barcodes.tsv.gz"]),
        _sample("GSM2", "Sample HF1 RNA", ["GSM2_HF1_filtered_feature_bc_matrix.h5"]),
        _sample("GSM3", "Sample HF2 ATAC", ["GSM3_HF2_atac_fragments.tsv.gz"]),
    ]
    draft = PairUnitResolutionDraft(units=[
        {"proposed_unit_id": "hf1", "member_samples": ["GSM1", "GSM2"], "grouping_rationale": "Both name HF1."},
        {"proposed_unit_id": "hf2", "member_samples": ["GSM3"], "grouping_rationale": "Names HF2; no RNA sample in this study."},
    ])
    resolver = PairUnitResolver(client=_FakeClient(draft.model_dump_json()))
    verifier = PairingVerifier(client=None)

    payload = await _run(tmp_path, candidate, assets, samples, resolver, verifier)
    bundles = {frozenset(item["asset_ids"]): item for item in payload["bundles"]}

    complete_unit = bundles[frozenset({"a1", "a2", "a5"})]
    incomplete_unit = bundles[frozenset({"a3"})]
    assert "multiome_same_cell" in complete_unit["satisfied_schema_ids"]
    assert "multiome_same_cell" not in incomplete_unit["satisfied_schema_ids"]


@pytest.mark.asyncio
async def test_no_geo_sample_metadata_falls_back_to_one_bundle_for_the_candidate(tmp_path):
    """Backward compatibility: a candidate this system has no per-sample
    metadata for degrades to today's one-bundle-per-candidate shape."""
    candidate = DatasetCandidate(candidate_id="cand_1", title="No GEO metadata available")
    assets = [
        _asset("a1", "cand_1", DataRole.FRAGMENTS, "atac_fragments.tsv.gz"),
        _asset("a2", "cand_1", DataRole.RNA_MATRIX, "filtered_feature_bc_matrix.h5"),
    ]
    payload = await _run(
        tmp_path, candidate, assets, samples=[],
        resolver=PairUnitResolver(client=None), verifier=PairingVerifier(client=None),
    )

    assert len(payload["bundles"]) == 1
    assert set(payload["bundles"][0]["asset_ids"]) == {"a1", "a2"}


@pytest.mark.asyncio
async def test_gsm_supplementary_files_become_real_assets_reaching_a_bundle(tmp_path):
    """Regression for the real-run gap found against GSE228428: a series'
    only asset-inventory-delta assets were unrelated series-level files (a
    genotype VCF and an unopened RAW.tar archive), while the real per-sample
    RNA/ATAC files were only ever known through GeoSampleMetadataAgent's
    ``supplementary_files`` -- and never became DataAssets, so the bundle
    that formed had no RNA/ATAC roles at all despite the data being real and
    file-resolved. Each GSM's own supplementary-file locators must become
    DataAssets with correctly inferred roles so the bundle they belong to can
    actually satisfy a schema."""
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="10x Multiome, one donor",
        assay_profiles=[AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    # Series-level supplementary files unrelated to any GSM's own data --
    # this is what GEOSupplementaryAssetResolver alone produced for the real
    # study: a demultiplexing VCF and an unopened archive.
    assets = [
        _asset("a_vcf", "cand_1", DataRole.OTHER, "GSE1_array_vcf_imputed.recode.vcf.gz"),
        _asset("a_raw", "cand_1", DataRole.ARCHIVE, "GSE1_RAW.tar"),
    ]
    samples = [
        _sample(
            "GSM1", "Donor 1 snRNA-seq",
            ["ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM1nnn/GSM1/suppl/GSM1_filtered_feature_bc_matrix.h5"],
        ),
        _sample(
            "GSM2", "Donor 1 snATAC-seq",
            [
                "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM2nnn/GSM2/suppl/GSM2_atac_fragments.tsv.gz",
                "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM2nnn/GSM2/suppl/GSM2_atac_fragments.tsv.gz.tbi",
                "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM2nnn/GSM2/suppl/GSM2_barcodes.tsv.gz",
            ],
        ),
    ]
    draft = PairUnitResolutionDraft(units=[
        {"proposed_unit_id": "donor1", "member_samples": ["GSM1", "GSM2"], "grouping_rationale": "Both name Donor 1."},
    ])
    resolver = PairUnitResolver(client=_FakeClient(draft.model_dump_json()))
    verifier = PairingVerifier(client=None)  # tier 1 (named technology) fires deterministically

    payload = await _run(tmp_path, candidate, assets, samples, resolver, verifier)

    gsm_assets = {
        item["file_name"]: item
        for item in payload["assets"]
        if item["file_name"] not in {"GSE1_array_vcf_imputed.recode.vcf.gz", "GSE1_RAW.tar"}
    }
    assert set(gsm_assets) == {
        "GSM1_filtered_feature_bc_matrix.h5",
        "GSM2_atac_fragments.tsv.gz",
        "GSM2_atac_fragments.tsv.gz.tbi",
        "GSM2_barcodes.tsv.gz",
    }
    assert gsm_assets["GSM1_filtered_feature_bc_matrix.h5"]["role"] == DataRole.RNA_MATRIX
    assert gsm_assets["GSM2_atac_fragments.tsv.gz"]["role"] == DataRole.FRAGMENTS
    assert gsm_assets["GSM1_filtered_feature_bc_matrix.h5"]["locations"][0]["url"].startswith("https://")
    assert gsm_assets["GSM1_filtered_feature_bc_matrix.h5"]["evidence_ids"] == ["ev_GSM1"]

    assert len(payload["bundles"]) == 1
    bundle = payload["bundles"][0]
    assert set(bundle["asset_ids"]) >= {
        gsm_assets["GSM1_filtered_feature_bc_matrix.h5"]["asset_id"],
        gsm_assets["GSM2_atac_fragments.tsv.gz"]["asset_id"],
    }
    assert "multiome_same_cell" in bundle["satisfied_schema_ids"]


@pytest.mark.asyncio
async def test_gsm_supplementary_file_already_resolved_by_another_route_is_not_duplicated(tmp_path):
    """A file a direct pointer or series-level resolver already turned into a
    DataAsset must not also get a second, separately-id'd GSM-derived
    DataAsset for the same file_name -- that would double-count it in role
    completeness and evidence without adding real information."""
    candidate = DatasetCandidate(candidate_id="cand_1", title="Already-resolved file")
    assets = [
        _asset("a1", "cand_1", DataRole.RNA_MATRIX, "GSM1_filtered_feature_bc_matrix.h5"),
    ]
    samples = [
        _sample(
            "GSM1", "Donor 1 snRNA-seq",
            ["ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM1nnn/GSM1/suppl/GSM1_filtered_feature_bc_matrix.h5"],
        ),
    ]
    payload = await _run(
        tmp_path, candidate, assets, samples,
        resolver=PairUnitResolver(client=None), verifier=PairingVerifier(client=None),
    )
    matching = [
        item for item in payload["assets"]
        if item["file_name"] == "GSM1_filtered_feature_bc_matrix.h5"
    ]
    assert len(matching) == 1
    assert matching[0]["asset_id"] == "a1"


@pytest.mark.asyncio
async def test_verified_per_unit_pairing_upgrades_a_weak_candidate_level_profile(tmp_path):
    """Regression for the real GSE228428 shape: the series' own title/abstract
    only says RNA and ATAC were co-reported (``study_level``,
    ``layer_inference``) -- ``PairingVerifier`` proves same-nucleus from an
    explicit statement in the *unit's own* GSM sample text, a claim
    ``bundle_shape_for`` never used to see because it only read the
    candidate-level AssayProfile. Without threading the verified claim
    through, ``multiome_same_cell`` stays inapplicable no matter how complete
    the files are -- exactly what left GSE228428 recommending 0 bundles."""
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="Multiomic profiling, no pairing phrase in the title",
        assay_profiles=[AssayProfile(
            label="scrna_and_scatac_co_reported",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.STUDY_LEVEL,
            inferred_from=ProfileBasis.LAYER_INFERENCE,
            rationale="RNA and ATAC are co-reported, but no pairing relationship is stated.",
        )],
    )
    assets = [
        _asset("a1", "cand_1", DataRole.FRAGMENTS, "GSM1_HF1_atac_fragments.tsv.gz"),
        _asset("a2", "cand_1", DataRole.RNA_MATRIX, "GSM2_HF1_filtered_feature_bc_matrix.h5"),
        _asset("a3", "cand_1", DataRole.CELL_METADATA, "GSM1_HF1_barcodes.tsv.gz"),
    ]
    samples = [
        _sample("GSM1", "Sample HF1 ATAC", ["GSM1_HF1_atac_fragments.tsv.gz", "GSM1_HF1_barcodes.tsv.gz"])
        .model_copy(update={"raw_text": "!Sample_title = HF1 [Lane 1 - ATAC]\nsame nucleus multiome"}),
        _sample("GSM2", "Sample HF1 RNA", ["GSM2_HF1_filtered_feature_bc_matrix.h5"])
        .model_copy(update={"raw_text": "!Sample_title = HF1 [Lane 1 - RNA]\nsame nucleus multiome"}),
    ]
    draft = PairUnitResolutionDraft(units=[
        {"proposed_unit_id": "hf1", "member_samples": ["GSM1", "GSM2"], "grouping_rationale": "Both name HF1."},
    ])
    resolver = PairUnitResolver(client=_FakeClient(draft.model_dump_json()))
    verifier = PairingVerifier(client=None)  # tier 2 (explicit statement) is deterministic

    payload = await _run(tmp_path, candidate, assets, samples, resolver, verifier)

    assert len(payload["bundles"]) == 1
    bundle = payload["bundles"][0]
    assert bundle["missing_roles"] == []
    assert "multiome_same_cell" in bundle["satisfied_schema_ids"]
    assert len(payload["pairing_claims"]) == 1
    assert payload["pairing_claims"][0]["evidence_class"] == "repository_reported"
    assert payload["pairing_claims"][0]["value"] == "same_nucleus"


@pytest.mark.asyncio
async def test_genome_build_stated_in_series_text_reaches_gsm_derived_assets(tmp_path):
    """A genome build named in the series' own description -- not on any
    file, since GEO per-sample listings almost never state one -- should
    still land on the GSM-derived DataAssets, so the existing bundle-scoped
    resolved_assembly_claim/catalog assembly column pick it up without
    waiting on a real download."""
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="10x Multiome, one donor",
        description="Reads were aligned to the GRCh38 reference genome.",
        assay_profiles=[AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    samples = [
        _sample("GSM1", "Donor 1 snRNA-seq", ["GSM1_filtered_feature_bc_matrix.h5"]),
    ]
    payload = await _run(
        tmp_path, candidate, assets=[], samples=samples,
        resolver=PairUnitResolver(client=None), verifier=PairingVerifier(client=None),
    )

    asset = next(
        item for item in payload["assets"]
        if item["file_name"] == "GSM1_filtered_feature_bc_matrix.h5"
    )
    assert asset["genome_build"] == "GRCh38"


class _FakeProbe:
    """Marks every direct location OBSERVED/PUBLIC_VERIFIED, offline.

    Mirrors test_manifest_ingestion.py's ``_FakeProbe``: it proves
    InventoryReconcileAgent wires the probe (and only probes GSM-derived
    assets, not ones already resolved by another route) without asserting
    anything about RemoteAvailabilityProbe's own HTTP behavior.
    """

    instances: list["_FakeProbe"] = []

    def __init__(self, **kwargs) -> None:
        self.seen_assets: list[DataAsset] | None = None
        _FakeProbe.instances.append(self)

    async def probe_assets(self, assets):
        self.seen_assets = list(assets)
        upgraded = []
        for asset in assets:
            location = asset.locations[0].model_copy(update={
                "availability": AssetAvailability.OBSERVED,
                "access_level": AccessLevel.PUBLIC_VERIFIED,
                "size_bytes": 1024,
            })
            upgraded.append(asset.model_copy(update={"locations": [location]}))
        return upgraded, []


@pytest.mark.asyncio
async def test_geo_sample_probe_upgrades_declared_assets_to_verified(tmp_path, monkeypatch):
    """GSM-derived assets are built here, not by ProgressiveAssetScoutAgent,
    so without probing them they stay PUBLIC_DECLARED forever and the bundle
    never clears acquisition's access:declared_only gate no matter how solid
    the pairing evidence is. A pre-resolved asset (already probed upstream,
    on another route) must not be re-probed."""
    _FakeProbe.instances.clear()
    monkeypatch.setattr(
        "scdata_agent.application.agents.inventory_reconcile.RemoteAvailabilityProbe",
        _FakeProbe,
    )
    candidate = DatasetCandidate(
        candidate_id="cand_1", title="10x Multiome, one donor",
        assay_profiles=[AssayProfile(
            label="10x_multiome_rna_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
            pairing_level=PairingLevel.SAME_NUCLEUS,
            inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            rationale="Named co-assay technology was observed.",
        )],
    )
    pre_resolved = _asset("asset_pre", "cand_1", DataRole.RNA_MATRIX, "GSM1_pre_resolved.h5")
    samples = [
        _sample("GSM1", "Donor 1", [
            "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM1nnn/GSM1/suppl/GSM1_pre_resolved.h5",
            "ftp://ftp.ncbi.nlm.nih.gov/geo/samples/GSM1nnn/GSM1/suppl/GSM1_atac_fragments.tsv.gz",
        ]),
    ]
    payload = await _run(
        tmp_path, candidate, assets=[pre_resolved], samples=samples,
        resolver=PairUnitResolver(client=None), verifier=PairingVerifier(client=None),
        policy={"remote_probe_enabled": True},
    )

    probe = _FakeProbe.instances[0]
    assert {item.file_name for item in probe.seen_assets} == {"GSM1_atac_fragments.tsv.gz"}

    by_name = {item["file_name"]: item for item in payload["assets"]}
    assert by_name["GSM1_atac_fragments.tsv.gz"]["locations"][0]["availability"] == "observed"
    assert by_name["GSM1_atac_fragments.tsv.gz"]["locations"][0]["access_level"] == "public_verified"
    assert by_name["GSM1_pre_resolved.h5"]["locations"][0]["availability"] == "observed"

    summary = payload["bundle_summaries"][0]
    assert summary["access_status"] == "verified"
