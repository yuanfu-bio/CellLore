"""Preflight over reconciled inventory and progressive evidence maturity."""

from __future__ import annotations

from scdata_agent.application.agents.shared import (
    AgentContext,
    latest_artifact,
    need_discovery,
)
from scdata_agent.application.collaboration import AgentName, ArtifactKind, artifact
from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.licensing import policies_for_urls
from scdata_agent.domain.lifecycle import (
    BundleStatus,
    LicenseStatus,
    PreflightBundleReport,
    SemanticBundle,
)
from scdata_agent.domain.models import (
    DataAsset,
    DataRole,
    GateStatus,
    best_pairing_level,
    pairing_satisfies,
)
from scdata_agent.domain.resource_pipeline import (
    BundleAccessStatus,
    EvidenceEscalationReport,
    RemoteBundleInventorySummary,
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
            observed_license_ids = {
                item.license_id for item in bundle_assets if item.license_id
            }
            expected_bytes = summary.total_bytes if summary else None
            access_status = (
                summary.access_status
                if summary else BundleAccessStatus.UNKNOWN
            )
            required_modalities = {
                item.value
                for item in (
                    [*context.requirement.modalities,
                     *context.requirement.acceptable_modalities]
                    if context.requirement else []
                )
            }
            observed_modalities = set(
                summary.reported_modalities if summary else []
            )
            modality_gate = (
                GateStatus.PASS
                if observed_modalities and (
                    not required_modalities
                    or bool(observed_modalities & required_modalities)
                )
                else GateStatus.FAIL
                if required_modalities and observed_modalities
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
            required_pairing = (
                context.requirement.minimum_pairing_level
                if context.requirement
                else None
            )
            observed_pairing = best_pairing_level(candidate.assay_profiles)
            if observed_pairing is None:
                pairing_gate = GateStatus.UNKNOWN
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
                "scientific_relevance": modality_gate,
                "modality_authenticity": modality_gate,
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
            if modality_gate == GateStatus.FAIL:
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
            elif pairing_gate == GateStatus.UNKNOWN and required_pairing is not None:
                # Pairing was asked for and could not be established either way.
                # Unknown is not absent, but it is also not a pass: silently
                # recommending it would assert a relationship nobody checked.
                warnings.append("pairing:unknown")
            if summary is not None and summary.inventory_truncated:
                warnings.append("inventory:truncated")
            if unresolved_evidence:
                warnings.append("evidence:unresolved")
            if hard_blockers:
                decision = BundleStatus.REJECTED
            elif not bundle.complete or unresolved_evidence:
                decision = BundleStatus.NEEDS_EVIDENCE
            else:
                decision = BundleStatus.PREFLIGHT_READY
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
                    "evidence_boundary": "remote_only",
                },
                parents=[
                    item.artifact_id
                    for item in (inventory, escalation_artifact)
                    if item is not None
                ],
            )
        ]

