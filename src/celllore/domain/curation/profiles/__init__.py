"""Registered ``CurationProfile`` instances.

Mirrors ``domain.manifest``'s ``DIALECTS``/``dialect_by_id`` pattern: adding a
new profile means adding an instance to ``PROFILES``, not a branch anywhere
that reads one.
"""

from __future__ import annotations

from celllore.domain.curation.profile import CurationProfile
from celllore.domain.curation.profiles.paired_atac_rna_human import (
    PAIRED_ATAC_RNA_HUMAN,
)
from celllore.domain.models import OmicsLayer
from celllore.domain.requirements import _contains

PROFILES: tuple[CurationProfile, ...] = (PAIRED_ATAC_RNA_HUMAN,)


def profile_by_id(profile_id: str) -> CurationProfile:
    for profile in PROFILES:
        if profile.profile_id == profile_id:
            return profile
    known = ", ".join(item.profile_id for item in PROFILES)
    raise KeyError(f"Unknown CurationProfile {profile_id!r}. Known: {known}.")


def select_profile(question: str) -> CurationProfile | None:
    """Select the ``CurationProfile`` a research question is actually asking
    for, or ``None`` when no registered profile matches.

    Runs on the raw question text, before requirement compilation: profile
    selection is a decision about the research goal, made once, up front, and
    threaded through compilation/discovery/decision from there -- it must
    never be inferred backward from what a discovered candidate's bundle
    happened to look like (a candidate can be out of scope for an otherwise
    correctly-selected profile for its own, separate reasons -- see
    ``application.agents.progressive_preflight``'s own per-bundle scope
    check). A question this system has no specialized objective for must
    leave every profile-gated decision inactive, not fall back to whichever
    profile happens to be registered.
    """
    for profile in PROFILES:
        if _profile_matches(question, profile):
            return profile
    return None


def _profile_matches(question: str, profile: CurationProfile) -> bool:
    # A named co-assay technology, or a bare combinatorial phrase like
    # "multiome"/"joint rna and atac" (also folded into technology_seeds --
    # see paired_atac_rna_human), is a direct, strong signal. Without this, a
    # phrase like "multiome RNA and ATAC" would fail the layer check below
    # purely because bare "RNA" is not itself a transcriptome-layer alias.
    if _contains(question, profile.search_hints.technology_seeds):
        return True
    # Otherwise, fall back to the open-vocabulary layer check: the question
    # must independently name every layer this profile's required_evidence
    # declares has_omics_layer:<layer> for -- a pure-ATAC question does not
    # also mention RNA/transcriptome terms, so it will not match a profile
    # that requires both.
    required_layers = {
        OmicsLayer(item.predicate.split(":", 1)[1])
        for item in profile.required_evidence
        if item.predicate.startswith("has_omics_layer:")
    }
    if not required_layers:
        return False
    return all(
        _contains(question, profile.search_hints.layer_aliases.get(layer, ()))
        for layer in required_layers
    )
