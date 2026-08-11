"""Preflight over reconciled inventory and progressive evidence maturity."""

from __future__ import annotations

from celllore.application.agents.shared import (
    AgentContext,
    latest_artifact,
    need_discovery,
)
from celllore.application.collaboration import AgentName, ArtifactKind, artifact
from celllore.application.resource_pipeline import resolved_assembly_claim
from celllore.domain.curation.decision import (
    EvidenceGate,
    evaluate_evidence_completeness,
)
from celllore.domain.curation.profiles import profile_by_id
from celllore.domain.identifiers import stable_id
from celllore.domain.licensing import policies_for_urls
from celllore.domain.lifecycle import (
    AuditQueueAction,
    AuditTask,
    AuditTaskCategoryFamily,
    BundleStatus,
    EvidenceClaim,
    LicenseStatus,
    PreflightBundleReport,
    SemanticBundle,
)
from celllore.domain.models import (
    DataAsset,
    DataRole,
    GateStatus,
    PairingLevel,
    best_pairing_level,
    pairing_satisfies,
)
from celllore.domain.resource_pipeline import (
    BundleAccessStatus,
    EvidenceEscalationReport,
    RemoteBundleInventorySummary,
)

_CATEGORY_FAMILY_BY_PREFIX: dict[str, AuditTaskCategoryFamily] = {
    "has_pair_relationship": AuditTaskCategoryFamily.PAIRING,
    "has_omics_layer": AuditTaskCategoryFamily.METADATA,
    "resolved_assembly": AuditTaskCategoryFamily.ASSEMBLY,
    "bundle:missing_roles": AuditTaskCategoryFamily.MISSING_FILE,
    "archive:uninspected": AuditTaskCategoryFamily.MISSING_FILE,
    "access:": AuditTaskCategoryFamily.DOWNLOAD,
    "modality:": AuditTaskCategoryFamily.METADATA,
    "license:": AuditTaskCategoryFamily.METADATA,
}


def _category_family(blocking_category: str) -> AuditTaskCategoryFamily:
    for prefix, family in _CATEGORY_FAMILY_BY_PREFIX.items():
        if blocking_category.startswith(prefix):
            return family
    return AuditTaskCategoryFamily.OTHER


# (audit_method, audit_steps, pass_criteria) per blocking_category prefix --
# grounded in what this codebase already knows how to check (the evidence
# tiers PairingVerifier itself runs, the role-inference machinery, the
# licence-policy precedence), not a generic template. A curation-catalog
# export (04_Audit_Hold) reads these three fields directly, so leaving them
# blank on every AuditTask -- which is what happened before this table
# existed -- meant that sheet had a blocking reason but no guidance on how to
# actually resolve it.
_AUDIT_GUIDANCE: dict[str, tuple[str, str, str]] = {
    "has_pair_relationship": (
        "Read this pair unit's own GSM sample text for a named co-assay "
        "technology or an explicit same-cell/same-nucleus statement; never "
        "infer from GSM accession adjacency.",
        "1) Open every member GSM's GEO record. 2) Check for a named "
        "co-assay technology (10x Multiome, SNARE-seq, ...) or explicit "
        "same-cell/same-nucleus wording in that sample's own text. 3) If "
        "neither is stated, a post-download barcode-overlap check is the "
        "only remaining path to a claim.",
        "A repository_reported has_pair_relationship claim at same_cell or "
        "same_nucleus is recorded for this pair unit.",
    ),
    "has_omics_layer": (
        "Confirm which omics layers this candidate's own text (series "
        "title/abstract, per-GSM sample titles) actually declares.",
        "1) Re-read the study/series text for the missing layer's "
        "technology terms. 2) Check per-GSM sample titles for a layer this "
        "candidate was never scored as declaring.",
        "A has_omics_layer claim for the missing layer is recorded, sourced "
        "from the study's own text.",
    ),
    "resolved_assembly": (
        "Confirm the genome build every required file was aligned/called "
        "against.",
        "1) Check each processed file's own header or the study's methods "
        "text for a genome build. 2) If files disagree, treat the bundle as "
        "assembly-ambiguous rather than picking one.",
        "A single, evidence-backed genome build is recorded for every "
        "required file in the bundle.",
    ),
    "bundle:missing_roles": (
        "Identify which required file role has no matching asset in this "
        "bundle's inventory.",
        "1) List this bundle's observed roles against its schema's required "
        "role groups. 2) Search the candidate's remaining supplementary "
        "files or archives for a file matching the missing role. 3) If "
        "genuinely absent, this bundle cannot satisfy the schema.",
        "Every required role group has at least one matching asset.",
    ),
    "archive:uninspected": (
        "Open the unopened archive and enumerate its members before "
        "judging bundle completeness -- an archive's contents are unknown, "
        "not absent.",
        "1) Download or remotely list the archive's table of contents. "
        "2) Match member file names against the missing roles via the same "
        "role-inference rules used elsewhere. 3) Re-judge the bundle on the "
        "real contents.",
        "The archive's members are enumerated and role-inferred; the "
        "bundle is re-judged on real contents, not an unopened container.",
    ),
    "access:": (
        "Confirm public accessibility of the recorded URLs.",
        "1) Re-probe each asset's declared URL. 2) Check the repository "
        "page directly for an access restriction or embargo notice.",
        "All required assets resolve with a public, unauthenticated "
        "response.",
    ),
    "modality:": (
        "Re-check whether this candidate actually declares the omics "
        "layers the active profile requires.",
        "1) Re-read the candidate's own declared assay profiles. 2) Confirm "
        "the mismatch is real and not an artifact of narrowing by the "
        "wrong requirement.",
        "The candidate's declared layers are confirmed to match or "
        "genuinely not match what the profile requires.",
    ),
    "license:": (
        "Read the record's own declared licence, or confirm every archive "
        "serving its files has a registered policy covering redistribution.",
        "1) Check the repository record for an explicit licence field. "
        "2) If absent, confirm every serving archive's terms are covered by "
        "a registered licensing policy (domain.licensing).",
        "Every asset's licence basis is approved or repository_policy, "
        "never restricted or unknown.",
    ),
    "profile:out_of_scope": (
        "Confirm this bundle's applicable schemas genuinely fall outside "
        "the active profile's bundle_schemas.",
        "1) List the bundle's applicable_schema_ids. 2) Compare against the "
        "resolved profile's own bundle_schemas. 3) If they should overlap, "
        "the layer/role inference that fed bundle_shape_for is what needs "
        "fixing, not this exclusion.",
        "Either the bundle stays excluded as genuinely out of scope, or the "
        "upstream schema-applicability computation is corrected.",
    ),
    "evidence:unresolved": (
        "Resolve the specific evidence claim(s) still flagged as unresolved "
        "for this bundle.",
        "1) Read the preflight report's rationale for which evidence field "
        "is unresolved. 2) Re-run the escalation step that produces it, or "
        "source it manually from the record.",
        "No evidence field required for this decision remains unresolved.",
    ),
}


def _audit_guidance(blocking_category: str) -> tuple[str, str, str]:
    for prefix, guidance in _AUDIT_GUIDANCE.items():
        if blocking_category.startswith(prefix):
            return guidance
    return (
        "Review the blocking reason directly; no standard checklist is "
        "registered for this category yet.",
        "",
        "",
    )


class ProgressivePreflightAssessmentAgent:
    name = AgentName.PREFLIGHT_ASSESSMENT

    async def run(self, context: AgentContext):
        discovery = need_discovery(context)
        inventory = latest_artifact(
            context.artifacts, ArtifactKind.ASSET_INVENTORY.value
        )
        assets = [
            DataAsset.model_validate(item)
            for item in (
                inventory.payload.get("assets", [])
                if inventory
                else []
            )
        ]
        bundles = [
            SemanticBundle.model_validate(item)
            for item in (
                inventory.payload.get("bundles", [])
                if inventory
                else []
            )
        ]
        summaries = {
            item.bundle_id: item
            for item in (
                RemoteBundleInventorySummary.model_validate(raw)
                for raw in (
                    inventory.payload.get("bundle_summaries", [])
                    if inventory else []
                )
            )
        }
        # Pair-unit-level `has_pair_relationship` claims (Phase 4): the
        # authoritative source for whether *this specific* unit's physical
        # relationship was proven, as opposed to reading the candidate's
        # assay profile, which only states what the assay generally supports.
        claims_by_pair_unit = {
            item.subject_id: item
            for item in (
                EvidenceClaim.model_validate(raw)
                for raw in (
                    inventory.payload.get("pairing_claims", [])
                    if inventory else []
                )
            )
            if item.predicate == "has_pair_relationship"
        }
        # Candidate-scoped `has_omics_layer:*` claims (Phase 5A).
        layer_claims_by_candidate: dict[str, list[EvidenceClaim]] = {}
        for raw in (inventory.payload.get("layer_claims", []) if inventory else []):
            claim = EvidenceClaim.model_validate(raw)
            layer_claims_by_candidate.setdefault(claim.subject_id, []).append(claim)
        # The registered CurationProfile this run's requirement declares, if
        # any. None means the evidence-completeness formula below stays
        # inactive and this agent falls back to its pre-Phase-5A logic
        # entirely -- additive/opt-in, never a crash for a caller that hasn't
        # threaded a profile through yet.
        profile = None
        if context.requirement is not None and context.requirement.profile_id is not None:
            try:
                profile = profile_by_id(context.requirement.profile_id)
            except KeyError:
                profile = None
        escalation_artifact = latest_artifact(
            context.artifacts, ArtifactKind.EVIDENCE_ESCALATION.value
        )
        escalation = (
            EvidenceEscalationReport.model_validate(
                escalation_artifact.payload
            )
            if escalation_artifact is not None
            else None
        )
        escalation_by_candidate = {
            item.candidate_id: item
            for item in (escalation.candidates if escalation else [])
        }
        by_asset = {item.asset_id: item for item in assets}
        by_candidate = {
            item.candidate_id: item for item in discovery.candidates
        }
        approved_candidates = set(
            context.policy.get("approved_license_candidate_ids", [])
        )
        approved_license_ids = {
            str(item).casefold()
            for item in context.policy.get("approved_license_ids", [])
        }
        restricted_license_ids = {
            str(item).casefold()
            for item in context.policy.get("restricted_license_ids", [])
        }
        reports: list[PreflightBundleReport] = []
        audit_tasks: list[AuditTask] = []
        assembly_claims: list[EvidenceClaim] = []

        for bundle in bundles:
            candidate = by_candidate[bundle.candidate_id]
            bundle_assets = [
                by_asset[asset_id]
                for asset_id in bundle.asset_ids
                if asset_id in by_asset
            ]
            summary = summaries.get(bundle.bundle_id)
            escalation_decision = escalation_by_candidate.get(
                bundle.candidate_id
            )
            unresolved_evidence = list(
                escalation_decision.unresolved_fields
                if escalation_decision is not None
                else []
            )
            if bundle_assets:
                unresolved_evidence = [
                    item
                    for item in unresolved_evidence
                    if item
                    not in {
                        "repository_pointer",
                        "remote_asset_manifest",
                    }
                ]
            if (
                "multiome_pairing" in unresolved_evidence
                and bundle.pair_unit_id in claims_by_pair_unit
            ):
                # "multiome_pairing" was flagged unresolved back at evidence
                # escalation, from nothing stronger than the candidate's own
                # shallow, discovery-time AssayProfile.pairing_level (often
                # study_level/unverified, since a series' own title/abstract
                # rarely states its pairing method even when a per-sample GSM
                # does). PairUnitResolver/PairingVerifier have since run and
                # reached a real, per-unit answer for this specific bundle --
                # claims_by_pair_unit holding an entry means that question was
                # actually asked and answered, positively or negatively, not
                # skipped. Continuing to call it "unresolved" here would keep
                # a bundle at NEEDS_EVIDENCE on stale information the
                # evidence-completeness formula above already re-evaluates
                # correctly against the real claim's evidence_class.
                unresolved_evidence = [
                    item for item in unresolved_evidence if item != "multiome_pairing"
                ]
            observed_license_ids = {
                item.license_id for item in bundle_assets if item.license_id
            }
            expected_bytes = summary.total_bytes if summary else None
            access_status = (
                summary.access_status
                if summary else BundleAccessStatus.UNKNOWN
            )
            required_layers = {
                item.value
                for item in (
                    context.requirement.required_omics_layers if context.requirement else []
                )
            }
            observed_layers = set(
                summary.reported_layers if summary else []
            )
            # A pairing requirement asks for the required layers *together*,
            # not as alternatives -- a standalone-ATAC bundle must not pass
            # this gate for a same-cell-multiome search just by sharing one
            # of the two requested layers. See domain.feasibility's identical
            # fix for the legacy DiscoveryWorkflow path.
            layer_relevant = (
                required_layers <= observed_layers
                if context.requirement and context.requirement.pairing_requirement is not None
                else bool(observed_layers & required_layers)
            )
            layer_gate = (
                GateStatus.PASS
                if observed_layers and (not required_layers or layer_relevant)
                else GateStatus.FAIL
                if required_layers and observed_layers
                else GateStatus.UNKNOWN
            )
            access_gate = {
                BundleAccessStatus.VERIFIED: GateStatus.PASS,
                BundleAccessStatus.PARTIAL: GateStatus.WARN,
                BundleAccessStatus.DECLARED_ONLY: GateStatus.WARN,
                BundleAccessStatus.AUTH_REQUIRED: GateStatus.FAIL,
                BundleAccessStatus.UNAVAILABLE: GateStatus.FAIL,
                BundleAccessStatus.UNKNOWN: GateStatus.UNKNOWN,
            }[access_status]
            # Precedence, strongest evidence first. A licence that was actually
            # read always decides; archive policy may only fill a genuine gap,
            # never override or upgrade an observed licence.
            # Pairing is judged on the level actually claimed, not on whether an
            # assay profile merely exists. The previous check passed every
            # candidate that had any profile, which included studies that only
            # report RNA and ATAC side by side with no stated relationship.
            #
            # Set membership, not rank: a hard pairing requirement is only
            # satisfied by an explicit allow-list of relationship kinds
            # (PairingRequirement.is_satisfied_by), never by "ranks at least as
            # high as" -- pairing_satisfies/pairing_rank are for soft ranking
            # only (see domain.models.PairingRequirement's docstring).
            # `minimum_pairing_level`/`pairing_satisfies` remain the fallback
            # for a requirement built without the newer field (e.g. a
            # hand-constructed ResearchRequirement that never went through
            # RuleBasedRequirementCompiler, which always sets both together).
            required_pairing = (
                context.requirement.minimum_pairing_level
                if context.requirement
                else None
            )
            pairing_requirement = (
                context.requirement.pairing_requirement
                if context.requirement
                else None
            )
            pairing_was_required = (
                pairing_requirement is not None or required_pairing is not None
            )
            if bundle.pair_unit_id is not None:
                # Pair-unit-resolved path (Phase 4): this bundle is one
                # PairUnitResolver-proposed unit, not the whole candidate. Its
                # relationship must be proven for THIS unit specifically --
                # reading the candidate's assay profile here would let a
                # study-level technology prior (e.g. "this is a DOGMA-seq
                # study") stand in for a grouping that was never verified, or
                # that PairingVerifier refused to verify because it was still
                # provisional. No claim means unknown, full stop -- it is
                # never backfilled from the candidate.
                claim = claims_by_pair_unit.get(bundle.pair_unit_id)
                observed_pairing = (
                    PairingLevel(claim.value) if claim is not None else None
                )
            else:
                # No pair-unit resolution ran for this bundle (e.g. curated-
                # manifest ingestion, which has its own file-level pairing
                # key) -- fall back to the candidate-level assay-profile
                # reading, same as before Phase 4.
                observed_pairing = best_pairing_level(candidate.assay_profiles)
            if observed_pairing is None:
                pairing_gate = GateStatus.UNKNOWN
            elif pairing_requirement is not None:
                pairing_gate = (
                    GateStatus.PASS
                    if pairing_requirement.is_satisfied_by(observed_pairing)
                    else GateStatus.WARN
                )
            elif pairing_satisfies(observed_pairing, required_pairing):
                pairing_gate = GateStatus.PASS
            else:
                pairing_gate = GateStatus.WARN

            license_values = {item.casefold() for item in observed_license_ids}
            license_policies = (
                policies_for_urls(
                    [
                        location.url
                        for item in bundle_assets
                        for location in item.locations
                    ]
                )
                if not license_values
                and not context.policy.get("strict_license", False)
                else []
            )
            if license_values & restricted_license_ids:
                license_status = LicenseStatus.RESTRICTED
            elif candidate.candidate_id in approved_candidates or (
                license_values and license_values <= approved_license_ids
            ):
                license_status = LicenseStatus.APPROVED
            elif license_policies:
                license_status = LicenseStatus.REPOSITORY_POLICY
            else:
                # Includes the case of an observed licence that is real but not
                # on the allowlist: unrecognised is not the same as permitted.
                license_status = LicenseStatus.UNKNOWN
            dimensions = {
                "scientific_relevance": layer_gate,
                "layer_authenticity": layer_gate,
                "remote_asset_inventory": (
                    GateStatus.PASS if bundle_assets else GateStatus.FAIL
                ),
                "semantic_bundle": (
                    GateStatus.PASS if bundle.complete else GateStatus.WARN
                ),
                "access": access_gate,
                "reference_declaration": (
                    GateStatus.PASS
                    if summary and summary.genome_builds
                    else GateStatus.UNKNOWN
                ),
                "pairing_evidence": pairing_gate,
                "evidence_maturity": (
                    GateStatus.PASS
                    if not unresolved_evidence else GateStatus.UNKNOWN
                ),
            }
            hard_blockers = []
            if access_status in {
                BundleAccessStatus.AUTH_REQUIRED,
                BundleAccessStatus.UNAVAILABLE,
            }:
                hard_blockers.append(f"access:{access_status.value}")
            if layer_gate == GateStatus.FAIL:
                hard_blockers.append("modality:mismatch")
            if license_status == LicenseStatus.RESTRICTED:
                hard_blockers.append("license:restricted")
            # An archive's payload cannot be identified without downloading it,
            # so an archive-only bundle is "contents unknown", not "roles
            # absent". Saying the latter would be a claim we never verified.
            archive_assets = [
                item for item in bundle_assets if item.role == DataRole.ARCHIVE
            ]
            # An archive sitting alongside a metadata file (e.g. donor_metadata
            # + multiome_archive) can still be the reason no schema is
            # satisfied yet -- restricting this to "every asset is an archive"
            # would judge the unopened archive's contents as verified-absent
            # whenever anything else happens to be in the bundle.
            archive_may_explain_gap = bool(archive_assets) and not bundle.satisfied_schema_ids
            warnings = []
            if access_status != BundleAccessStatus.VERIFIED:
                warnings.append(f"access:{access_status.value}")
            if expected_bytes is None:
                warnings.append("size:unknown")
            if license_status == LicenseStatus.UNKNOWN:
                warnings.append("license:unknown")
            elif license_status == LicenseStatus.REPOSITORY_POLICY:
                # Recorded, not silent: the reviewer sees what is relied upon.
                warnings.extend(
                    f"license:repository_policy:{item.policy_id}"
                    for item in license_policies
                )
            if not bundle.complete:
                warnings.append(
                    "archive:uninspected" if archive_may_explain_gap else "bundle:missing_roles"
                )
            elif archive_assets:
                warnings.append("archive:uninspected")
            if pairing_gate == GateStatus.WARN:
                warnings.append("pairing:unverified")
            elif pairing_gate == GateStatus.UNKNOWN and pairing_was_required:
                # Pairing was asked for and could not be established either way.
                # Unknown is not absent, but it is also not a pass: silently
                # recommending it would assert a relationship nobody checked.
                warnings.append("pairing:unknown")
            if summary is not None and summary.inventory_truncated:
                warnings.append("inventory:truncated")
            if unresolved_evidence:
                warnings.append("evidence:unresolved")
            # Data availability (bundle.complete -- do we have the files?) and
            # scientific relationship eligibility (pairing_gate -- do the files
            # prove the required relationship?) are orthogonal questions. A
            # bundle whose only applicable schema is `raw_reads` (files exist,
            # unprocessed) is acquisition-worthy, but raw reads alone prove
            # nothing about pairing -- so `bundle.complete` must never be the
            # sole gate when a pairing requirement exists and is unresolved.
            pairing_insufficient = (
                pairing_was_required and pairing_gate != GateStatus.PASS
            )

            # Evidence-completeness Decision (Phase 5A): "Decision = required
            # evidence completeness + blocking deterministic validators --
            # never LLM confidence." Only active when:
            #  1. a profile is resolvable -- context.requirement.profile_id
            #     is now set by domain.curation.profiles.select_profile,
            #     evaluated once against the research question itself before
            #     compilation even happens (application.research_planning /
            #     infrastructure.llm.research_planner). A question this
            #     system has no specialized profile for (e.g. a plain scATAC
            #     question) never gets profile_id set in the first place, so
            #     this whole formula is inactive for it -- selection is not
            #     this agent's job and must not be re-derived here.
            #  2. this bundle went through pair-unit resolution
            #     (bundle.pair_unit_id is not None) -- a curated-manifest
            #     bundle never has one (manifest candidates declare
            #     assay_profiles=[] by design, so it could never satisfy
            #     has_omics_layer/*).
            # Given #1 and #2, whether this specific bundle actually claims
            # one of the profile's own bundle_schemas is a *scope* question,
            # not an "is the profile active" question (Round 5 correction):
            # even within a correctly paired-multiome-profiled search, one
            # discovered candidate can still turn out to be a standalone
            # scATAC study (applicable_schema_ids like scatac_standalone/
            # raw_reads, never multiome_same_cell/multiome_trimodal). That
            # bundle is out of scope for *this profile's* corpus and must be
            # excluded outright -- it must NOT fall back to the pre-profile
            # legacy pairing_gate below, because that legacy path reads
            # candidate.assay_profiles unscoped by any profile and could
            # therefore admit the bundle as PREFLIGHT_READY on the strength
            # of an assay-level pairing prior this profile never asked
            # about. The legacy fallback is reserved for bundles that never
            # had a profile or pair-unit resolution to begin with (#1/#2
            # false), not for ones that had both but missed on scope.
            assembly_claim = resolved_assembly_claim(bundle, summary)
            if assembly_claim is not None:
                assembly_claims.append(assembly_claim)
            evidence_result = None
            profile_out_of_scope = False
            profile_schema_ids = (
                {item.schema_id for item in profile.bundle_schemas} if profile else set()
            )
            profile_applies_to_bundle = bool(
                profile_schema_ids & set(bundle.applicable_schema_ids)
            )
            if profile is not None and bundle.pair_unit_id is not None:
                if profile_applies_to_bundle:
                    bundle_claims = [
                        *layer_claims_by_candidate.get(bundle.candidate_id, []),
                        *([assembly_claim] if assembly_claim is not None else []),
                        *(
                            [claims_by_pair_unit[bundle.pair_unit_id]]
                            if bundle.pair_unit_id in claims_by_pair_unit
                            else []
                        ),
                    ]
                    evidence_result = evaluate_evidence_completeness(
                        profile, EvidenceGate.ACQUISITION, bundle_claims
                    )
                    dimensions["evidence_completeness"] = (
                        GateStatus.PASS if evidence_result.satisfied else GateStatus.FAIL
                    )
                    evidence_insufficient = not evidence_result.satisfied
                else:
                    profile_out_of_scope = True
                    evidence_insufficient = True
                    dimensions["profile_scope"] = GateStatus.FAIL
            else:
                # Legacy path: no profile resolvable, or no pair-unit
                # resolution ran for this bundle (curated-manifest path).
                evidence_insufficient = pairing_insufficient

            if hard_blockers:
                decision = BundleStatus.REJECTED
            elif profile_out_of_scope:
                decision = BundleStatus.REJECTED
            elif not bundle.complete or unresolved_evidence or evidence_insufficient:
                decision = BundleStatus.NEEDS_EVIDENCE
            else:
                decision = BundleStatus.PREFLIGHT_READY

            audit_task: AuditTask | None = None
            if decision == BundleStatus.REJECTED:
                if hard_blockers:
                    blocking_category = hard_blockers[0]
                    rationale = "; ".join(hard_blockers)
                else:
                    # profile_out_of_scope: hard_blockers is empty here by
                    # construction (that branch takes priority above), so
                    # this can only be the scope exclusion.
                    blocking_category = f"profile:out_of_scope:{profile.profile_id}"
                    rationale = (
                        f"Bundle claims {', '.join(bundle.applicable_schema_ids) or 'no'} "
                        f"schema(s), none of which belong to "
                        f"{profile.profile_id}'s bundle_schemas "
                        f"({', '.join(sorted(profile_schema_ids)) or 'none'}); excluded "
                        "from this profile's corpus rather than evaluated against its "
                        "required_evidence."
                    )
                audit_method, audit_steps, pass_criteria = _audit_guidance(blocking_category)
                audit_task = AuditTask(
                    task_id=stable_id("audit_task", f"{bundle.bundle_id}:{context.run_id}"),
                    run_id=context.run_id,
                    subject_id=bundle.pair_unit_id or bundle.candidate_id,
                    bundle_id=bundle.bundle_id,
                    queue_action=AuditQueueAction.EXCLUDED,
                    blocking_category=blocking_category,
                    category_family=_category_family(blocking_category),
                    audit_method=audit_method,
                    audit_steps=audit_steps,
                    pass_criteria=pass_criteria,
                    rationale=rationale,
                )
            elif decision == BundleStatus.NEEDS_EVIDENCE:
                if evidence_result is not None and evidence_result.missing:
                    blocking_category = evidence_result.missing[0].predicate
                    queue_action = AuditQueueAction.VERIFY_THEN_ARCHIVE
                elif not bundle.complete:
                    blocking_category = "bundle:missing_roles"
                    queue_action = AuditQueueAction.DEFERRED
                else:
                    blocking_category = "evidence:unresolved"
                    queue_action = AuditQueueAction.VERIFY_THEN_ARCHIVE
                audit_method, audit_steps, pass_criteria = _audit_guidance(blocking_category)
                audit_task = AuditTask(
                    task_id=stable_id("audit_task", f"{bundle.bundle_id}:{context.run_id}"),
                    run_id=context.run_id,
                    subject_id=bundle.pair_unit_id or bundle.candidate_id,
                    bundle_id=bundle.bundle_id,
                    queue_action=queue_action,
                    blocking_category=blocking_category,
                    category_family=_category_family(blocking_category),
                    audit_method=audit_method,
                    audit_steps=audit_steps,
                    pass_criteria=pass_criteria,
                    rationale=(
                        "; ".join(item.predicate for item in evidence_result.missing)
                        if evidence_result is not None and evidence_result.missing
                        else "; ".join(warnings) or "incomplete bundle"
                    ),
                )
            if audit_task is not None:
                audit_tasks.append(audit_task)
            dimensions["license"] = (
                GateStatus.PASS
                if license_status == LicenseStatus.APPROVED
                else GateStatus.FAIL
                if license_status == LicenseStatus.RESTRICTED
                # Archive policy is a real basis but a weaker one than a licence
                # that was read, so it warns rather than passing outright.
                else GateStatus.WARN
                if license_status == LicenseStatus.REPOSITORY_POLICY
                else GateStatus.UNKNOWN
            )
            report = PreflightBundleReport(
                report_id=stable_id(
                    "preflight", f"{bundle.bundle_id}:{context.run_id}"
                ),
                bundle_id=bundle.bundle_id,
                candidate_id=bundle.candidate_id,
                dimensions=dimensions,
                rationale={
                    "evidence_boundary": (
                        "No local file content was opened; findings are "
                        "repository-reported or remotely observed."
                    ),
                    "license": (
                        "No per-record licence is published for this bundle; "
                        "relying on "
                        + " Also relying on ".join(
                            item.as_basis() for item in license_policies
                        )
                        + " "
                        + " ".join(item.caveat for item in license_policies)
                        if license_status == LicenseStatus.REPOSITORY_POLICY
                        else "Repository-reported licenses: "
                        f"{sorted(observed_license_ids) or ['unknown']}; "
                        "training use requires an explicit allowlist or "
                        "candidate approval."
                    ),
                    "unresolved_evidence": (
                        ", ".join(unresolved_evidence)
                        if unresolved_evidence
                        else "none"
                    ),
                    "pairing": (
                        f"Required {required_pairing.value}; strongest claim "
                        f"observed is {observed_pairing.value} from "
                        f"{', '.join(sorted({item.label for item in candidate.assay_profiles}))}."
                        if required_pairing is not None and observed_pairing is not None
                        else "No pairing requirement was set."
                        if required_pairing is None
                        else "No assay profile could be inferred, so the pairing "
                        "relationship is unknown rather than absent."
                    ),
                    "inventory_completeness": (
                        "Listing cut short by the asset budget for: "
                        f"{', '.join(summary.truncated_resolvers)}. The asset "
                        "set is a subset of the repository's holdings and the "
                        "reported size is a lower bound; raise --max-assets to "
                        "enumerate the rest."
                        if summary is not None and summary.inventory_truncated
                        else "Remote listings were enumerated in full."
                    ),
                    "bundle_schema": (
                        "Satisfies "
                        f"{', '.join(bundle.satisfied_schema_ids)}"
                        if bundle.satisfied_schema_ids
                        else (
                            "Contents are archived and were not opened; "
                            "completeness is unknown, not disproven."
                            if archive_may_explain_gap
                            else "Satisfies none of "
                            f"{', '.join(bundle.applicable_schema_ids) or 'n/a'}; "
                            f"observed roles: "
                            f"{', '.join(str(item) for item in bundle.observed_roles) or 'none'}"
                        )
                    ),
                },
                license_status=license_status,
                license_policy_ids=(
                    [item.policy_id for item in license_policies]
                    if license_status == LicenseStatus.REPOSITORY_POLICY
                    else []
                ),
                license_basis=(
                    "; ".join(item.as_basis() for item in license_policies)
                    if license_status == LicenseStatus.REPOSITORY_POLICY
                    else None
                ),
                expected_bytes=expected_bytes,
                storage_scale={
                    "asset_count": summary.asset_count if summary else len(bundle_assets),
                    "known_bytes": summary.known_bytes if summary else 0,
                    "total_bytes": expected_bytes,
                    "unknown_size_asset_count": (
                        summary.unknown_size_asset_count if summary else len(bundle_assets)
                    ),
                },
                biological_scale={
                    "donor_count": candidate.donor_count,
                    "sample_count": candidate.sample_count,
                    "cell_count": candidate.cell_count,
                },
                file_type_counts=summary.file_type_counts if summary else {},
                role_counts=summary.role_counts if summary else {},
                access_status=access_status.value,
                hard_blockers=hard_blockers,
                warnings=warnings,
                decision=decision,
                evidence_ids=bundle.evidence_ids,
            )
            reports.append(report)
            if context.catalog is not None:
                context.catalog.append(
                    record_id=report.report_id,
                    record_type="preflight_report",
                    run_id=context.run_id,
                    value=report,
                )
                context.catalog.transition_bundle(
                    bundle.bundle_id,
                    decision,
                    run_id=context.run_id,
                )

        if context.catalog is not None:
            for item in assembly_claims:
                context.catalog.append(
                    record_id=item.claim_id,
                    record_type="evidence_claim",
                    run_id=context.run_id,
                    value=item,
                )
            for item in audit_tasks:
                context.catalog.append(
                    record_id=item.task_id,
                    record_type="audit_task",
                    run_id=context.run_id,
                    value=item,
                )

        return [
            artifact(
                context.run_id,
                ArtifactKind.PREFLIGHT_REPORT,
                self.name,
                {
                    "reports": [
                        item.model_dump(mode="json")
                        for item in reports
                    ],
                    "audit_tasks": [
                        item.model_dump(mode="json") for item in audit_tasks
                    ],
                    "assembly_claims": [
                        item.model_dump(mode="json") for item in assembly_claims
                    ],
                    "evidence_boundary": "remote_only",
                },
                parents=[
                    item.artifact_id
                    for item in (inventory, escalation_artifact)
                    if item is not None
                ],
            )
        ]

