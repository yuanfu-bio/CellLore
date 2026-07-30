"""Merge heterogeneous source records into canonical dataset candidates."""

from __future__ import annotations

from collections import defaultdict

from scdata_agent.domain.identifiers import (
    deduplicate_identifiers,
    preferred_candidate_key,
    stable_id,
)
from scdata_agent.domain.models import AccessLevel, DatasetCandidate, SourceRecord


def merge_source_records(records: list[SourceRecord]) -> list[DatasetCandidate]:
    """Merge records that share any dataset identifier.

    Records with no dataset identifier remain independent literature candidates.
    This conservative rule avoids merging biologically similar but distinct studies.
    """

    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_seen: dict[str, int] = {}
    for index, record in enumerate(records):
        for identifier in record.identifiers:
            canonical = identifier.canonical
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

