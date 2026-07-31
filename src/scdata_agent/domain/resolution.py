"""Merge heterogeneous source records into canonical dataset candidates."""

from __future__ import annotations

from collections import defaultdict

from scdata_agent.domain.identifiers import (
    deduplicate_identifiers,
    preferred_candidate_key,
    stable_id,
)
from scdata_agent.domain.models import AccessLevel, DatasetCandidate, SourceRecord

# Identifiers that name one deposition, as opposed to the programme that funded
# it or the archive project it was filed under. Two records that each carry one
# of these and do not share it are different studies, whatever else they have in
# common.
STUDY_NAMESPACES = frozenset({"geo_series", "ega_study"})


def merge_source_records(records: list[SourceRecord]) -> list[DatasetCandidate]:
    """Merge records that share a dataset identifier, unless they disagree.

    Sharing an identifier is evidence of identity, but not all identifiers carry
    the same weight. ENCODE files every experiment under one BioProject
    (PRJNA63443) and one SRA study (SRP012412); unioning on those merged 13
    distinct GEO Series into a single candidate, whose modality list was then the
    union of everything ENCODE has ever deposited. A same-cell question admitted
    the whole blob because one member of it was genuinely multiome.

    So study identifiers are applied first, and any later identifier may only
    merge two groups that do not already disagree about which study they are.
    This needs no list of which accessions are umbrellas: an umbrella is simply
    an identifier whose members turn out to disagree.

    Records with no dataset identifier remain independent literature candidates.
    """

    parent = list(range(len(records)))
    study_ids: list[set[str]] = [
        {
            identifier.canonical
            for identifier in record.identifiers
            if identifier.namespace.lower() in STUDY_NAMESPACES
        }
        for record in records
    ]

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        left_studies, right_studies = study_ids[left_root], study_ids[right_root]
        if left_studies and right_studies and left_studies.isdisjoint(right_studies):
            return
        parent[right_root] = left_root
        study_ids[left_root] = left_studies | right_studies

    # Study identifiers first, so that by the time a broader identifier is
    # considered, the groups it would join already know which study they are.
    # Sorted within each pass to keep the outcome independent of input order.
    mentions: list[tuple[int, str, int]] = [
        (
            0 if identifier.namespace.lower() in STUDY_NAMESPACES else 1,
            identifier.canonical,
            index,
        )
        for index, record in enumerate(records)
        for identifier in record.identifiers
    ]
    first_seen: dict[str, int] = {}
    for _, canonical, index in sorted(mentions):
        if canonical in first_seen:
            union(index, first_seen[canonical])
        else:
            first_seen[canonical] = index

    groups: dict[int, list[SourceRecord]] = defaultdict(list)
    for index, record in enumerate(records):
        groups[find(index)].append(record)

    candidates = [_merge_group(group) for group in groups.values()]
    return sorted(candidates, key=lambda item: item.candidate_id)


def _merge_group(records: list[SourceRecord]) -> DatasetCandidate:
    identifiers = deduplicate_identifiers(
        identifier for record in records for identifier in record.identifiers
    )
    publication_identifiers = deduplicate_identifiers(
        identifier
        for record in records
        for identifier in record.publication_identifiers
    )
    fallback = min(record.record_id for record in records)
    key = preferred_candidate_key(identifiers, fallback=fallback)
    candidate_id = stable_id("ds", key)
    best_record = max(
        records,
        key=lambda record: (
            record.source_name != "europe_pmc",
            len(record.description or ""),
        ),
    )
    access_levels = {record.access_level for record in records}
    if AccessLevel.PUBLIC_VERIFIED in access_levels:
        access = AccessLevel.PUBLIC_VERIFIED
    elif AccessLevel.PUBLIC_DECLARED in access_levels:
        access = AccessLevel.PUBLIC_DECLARED
    else:
        access = next(iter(access_levels), AccessLevel.UNKNOWN)

    return DatasetCandidate(
        candidate_id=candidate_id,
        title=best_record.title,
        description=best_record.description,
        identifiers=identifiers,
        publication_identifiers=publication_identifiers,
        source_record_ids=sorted(record.record_id for record in records),
        organisms=_unique(
            organism for record in records for organism in record.organisms
        ),
        modalities=_unique(
            modality for record in records for modality in record.modalities
        ),
        assay_profiles=_unique(
            profile for record in records for profile in record.assay_profiles
        ),
        access_level=access,
        evidence=_unique(
            evidence for record in records for evidence in record.evidence
        ),
    )


def _unique(values):
    seen = set()
    result = []
    for value in values:
        key = (
            value.model_dump_json()
            if hasattr(value, "model_dump_json")
            else str(value)
        )
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result

