"""Conservative, explainable feasibility gates."""

from __future__ import annotations

from celllore.domain.identifiers import stable_id
from celllore.domain.models import (
    AccessLevel,
    CurationDecision,
    DatasetCandidate,
    FeasibilityDimension,
    FeasibilityReport,
    GateStatus,
    ResearchRequirement,
)


class FeasibilityAssessor:
    def assess(
        self,
        requirement: ResearchRequirement,
        candidate: DatasetCandidate,
    ) -> FeasibilityReport:
        dimensions: dict[str, FeasibilityDimension] = {}

        requested_layers = set(requirement.required_omics_layers)
        observed_layers = {
            layer for profile in candidate.assay_profiles for layer in profile.omics_layers
        }
        # A pairing requirement means the requested layers are asked for
        # *together*, not as alternatives -- "chromatin accessibility and
        # transcriptome in the same cell" is not satisfied by a candidate
        # that supplies only one of the two. Requiring the full subset here
        # (rather than any overlap) is what keeps a standalone-ATAC record
        # out of a same-cell-multiome question's topic-relevance gate.
        is_joint_requirement = requirement.pairing_requirement is not None
        relevant = (
            requested_layers <= observed_layers
            if is_joint_requirement
            else bool(observed_layers & requested_layers)
        )
        if relevant:
            dimensions["layer_relevance"] = FeasibilityDimension(
                status=GateStatus.PASS,
                rationale=(
                    "All requested omics layers are observed together."
                    if is_joint_requirement
                    else "At least one observed omics layer matches the requirement."
                ),
            )
        elif observed_layers:
            dimensions["layer_relevance"] = FeasibilityDimension(
                status=GateStatus.FAIL,
                rationale=(
                    "The requirement asks for these layers together; only a "
                    "subset was observed."
                    if is_joint_requirement
                    else "Observed omics layers do not match the requirement."
                ),
            )
        else:
            dimensions["layer_relevance"] = FeasibilityDimension(
                status=GateStatus.UNKNOWN,
                rationale="Omics layers have not yet been verified from repository or files.",
            )

        if requirement.pairing_requirement is not None:
            matching_profiles = [
                profile
                for profile in candidate.assay_profiles
                if set(profile.omics_layers) & requested_layers
            ]
            if any(
                requirement.pairing_requirement.is_satisfied_by(profile.pairing_level)
                for profile in matching_profiles
            ):
                dimensions["pairing_evidence"] = FeasibilityDimension(
                    status=GateStatus.PASS,
                    rationale="A pairing relationship satisfying the requirement is stated.",
                )
            elif matching_profiles:
                dimensions["pairing_evidence"] = FeasibilityDimension(
                    status=GateStatus.UNKNOWN,
                    rationale="Multiple assay layers are reported, but their pairing level is not verified.",
                )
            else:
                dimensions["pairing_evidence"] = FeasibilityDimension(
                    status=GateStatus.UNKNOWN,
                    rationale="The requested paired assay has not yet been verified from source metadata.",
                )
        else:
            dimensions["pairing_evidence"] = FeasibilityDimension(
                status=GateStatus.PASS,
                rationale="A paired assay is not a hard requirement for this collection profile.",
            )

        if requirement.organisms and candidate.organisms:
            expected = {value.casefold() for value in requirement.organisms}
            observed = {value.casefold() for value in candidate.organisms}
            status = GateStatus.PASS if expected & observed else GateStatus.FAIL
            rationale = (
                "Observed organism matches the requirement."
                if status == GateStatus.PASS
                else "Observed organism does not match the requirement."
            )
        elif requirement.organisms:
            status = GateStatus.UNKNOWN
            rationale = "Organism is required but not yet verified."
        else:
            status = GateStatus.PASS
            rationale = "No organism constraint was specified."
        dimensions["organism"] = FeasibilityDimension(
            status=status, rationale=rationale
        )

        excluded_access = set(requirement.excluded_access_levels)
        if candidate.access_level in excluded_access:
            status = GateStatus.FAIL
            rationale = "The access level is explicitly excluded by the research requirement."
        elif requirement.public_only:
            if candidate.access_level in {
                AccessLevel.PUBLIC_VERIFIED,
                AccessLevel.PUBLIC_DECLARED,
            }:
                status = GateStatus.PASS
                rationale = "The source declares public access."
            elif candidate.access_level in {
                AccessLevel.CONTROLLED,
                AccessLevel.AUTH_REQUIRED,
                AccessLevel.EMBARGOED,
            }:
                status = GateStatus.FAIL
                rationale = "The access level conflicts with the public-only policy."
            else:
                status = GateStatus.UNKNOWN
                rationale = "Content access has not been probed."
        else:
            status = GateStatus.PASS
            rationale = "Controlled or authenticated sources are allowed."
        dimensions["access"] = FeasibilityDimension(
            status=status, rationale=rationale
        )

        excluded_sources = {item.casefold() for item in requirement.excluded_biological_sources}
        candidate_text = " ".join(filter(None, [candidate.title, candidate.description])).casefold()
        if "cell_line" in excluded_sources and any(term in candidate_text for term in ("cell line", "cell-line", "细胞系")):
            dimensions["biological_source"] = FeasibilityDimension(status=GateStatus.FAIL, rationale="Candidate text indicates an explicitly excluded cell-line source.")
        elif excluded_sources:
            dimensions["biological_source"] = FeasibilityDimension(status=GateStatus.UNKNOWN, rationale="Excluded biological source requires metadata verification.")

        missing_roles = sorted(
            set(requirement.required_data_roles)
            - set(candidate.observed_data_roles),
            key=str,
        )
        if not missing_roles:
            status = GateStatus.PASS
            rationale = "All required data roles have been observed."
        elif candidate.observed_data_roles:
            status = GateStatus.WARN
            rationale = "Some required data roles are missing."
        else:
            status = GateStatus.UNKNOWN
            rationale = "A file manifest has not yet been inspected."
        dimensions["asset_bundle"] = FeasibilityDimension(
            status=status, rationale=rationale
        )

        if requirement.minimum_donors is None:
            status = GateStatus.PASS
            rationale = "No minimum donor constraint was specified."
        elif candidate.donor_count is None:
            status = GateStatus.UNKNOWN
            rationale = "Donor count is required but not yet verified."
        elif candidate.donor_count >= requirement.minimum_donors:
            status = GateStatus.PASS
            rationale = "Donor count satisfies the minimum."
        else:
            status = GateStatus.FAIL
            rationale = "Donor count is below the required minimum."
        dimensions["study_design"] = FeasibilityDimension(
            status=status, rationale=rationale
        )

        failures = [
            name
            for name, dimension in dimensions.items()
            if dimension.status == GateStatus.FAIL
        ]
        unknowns = [
            name
            for name, dimension in dimensions.items()
            if dimension.status in {GateStatus.UNKNOWN, GateStatus.WARN}
        ]
        if failures:
            decision = CurationDecision.EXCLUDE
            reasons = [f"Failed hard gate: {name}" for name in failures]
        elif unknowns:
            decision = CurationDecision.MANUAL_REVIEW
            reasons = [f"Needs more evidence: {name}" for name in unknowns]
        else:
            decision = CurationDecision.ELIGIBLE
            reasons = ["All currently evaluated gates passed."]

        return FeasibilityReport(
            report_id=stable_id(
                "feasibility", f"{candidate.candidate_id}:{requirement.question}"
            ),
            candidate_id=candidate.candidate_id,
            dimensions=dimensions,
            missing_required_roles=missing_roles,
            decision=decision,
            decision_reasons=reasons,
        )
