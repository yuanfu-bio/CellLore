"""Identifier extraction and canonical entity helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from scdata_agent.domain.models import Identifier

ACCESSION_PATTERNS: dict[str, re.Pattern[str]] = {
    "geo_series": re.compile(r"\bGSE\d+\b", re.IGNORECASE),
    "geo_sample": re.compile(r"\bGSM\d+\b", re.IGNORECASE),
    "sra_project": re.compile(r"\b(?:SRP|ERP|DRP)\d+\b", re.IGNORECASE),
    "sra_experiment": re.compile(r"\b(?:SRX|ERX|DRX)\d+\b", re.IGNORECASE),
    "sra_run": re.compile(r"\b(?:SRR|ERR|DRR)\d+\b", re.IGNORECASE),
    "sra_sample": re.compile(r"\b(?:SRS|ERS|DRS)\d+\b", re.IGNORECASE),
    "bioproject": re.compile(r"\bPRJ(?:NA|EB|DB)\d+\b", re.IGNORECASE),
    "biosample": re.compile(r"\b(?:SAMN|SAMEA|SAMD)\d+\b", re.IGNORECASE),
    "ega_study": re.compile(r"\bEGAS\d+\b", re.IGNORECASE),
    "ega_dataset": re.compile(r"\bEGAD\d+\b", re.IGNORECASE),
    "dbgap": re.compile(r"\bphs\d+(?:\.v\d+\.p\d+)?\b", re.IGNORECASE),
    "zenodo_record": re.compile(
        r"(?:zenodo\.org/(?:records|record)/|10\.5281/zenodo\.)(\d+)", re.IGNORECASE
    ),
    "figshare_article": re.compile(r"10\.6084/m9\.figshare\.(\d+)", re.IGNORECASE),
}


class AccessionRelation(StrEnum):
    PRIMARY_DATA_DEPOSITION = "primary_data_deposition"
    REUSED_DATASET = "reused_dataset"
    SUPPLEMENTARY_REFERENCE = "supplementary_reference"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IdentifierMention:
    identifier: Identifier
    relation: AccessionRelation
    confidence: float
    context_excerpt: str


_PRIMARY_SELF_CUES = (
    "generated in this study", "generated in the present study",
    "data generated for this study", "we generated", "we deposited",
    "have been deposited", "has been deposited",
)
_PRIMARY_DEPOSIT_CUES = (
    "data are available", "data is available", "available from geo",
    "available at geo", "deposited in geo", "deposited at geo",
    "under accession", "accession number", "data availability",
)
_REUSE_CUES = (
    "publicly available dataset", "publicly available data",
    "previously published", "downloaded from", "we downloaded", "obtained from",
    "retrieved from", "we used dataset", "we used data",
    "we analyzed dataset", "we analysed dataset", "reanalysis",
    "re-analy", "benchmark dataset", "external dataset",
)
_SUPPLEMENTARY_CUES = (
    "supplementary table", "supplementary data", "for comparison", "see also",
)


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def extract_identifiers(text: str | None) -> list[Identifier]:
    return [item.identifier for item in extract_identifier_mentions(text)]


def extract_identifier_mentions(
    text: str | None,
    *,
    context_chars: int = 280,
) -> list[IdentifierMention]:
    """Extract accessions with conservative article-to-dataset semantics."""
    if not text:
        return []
    mentions: dict[str, IdentifierMention] = {}
    for namespace, pattern in ACCESSION_PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            identifier = Identifier(namespace=namespace, value=value.upper())
            excerpt = _mention_context(
                text, match.start(), match.end(), context_chars
            )
            relation, confidence = _classify_relation(excerpt.casefold())
            mention = IdentifierMention(
                identifier=identifier,
                relation=relation,
                confidence=confidence,
                context_excerpt=excerpt[:800],
            )
            existing = mentions.get(identifier.canonical)
            if (
                existing is None
                or _mention_priority(mention) > _mention_priority(existing)
            ):
                mentions[identifier.canonical] = mention
    return sorted(
        mentions.values(), key=lambda item: item.identifier.canonical
    )


def _mention_context(
    text: str, start: int, end: int, context_chars: int
) -> str:
    left_floor = max(0, start - context_chars)
    right_ceiling = min(len(text), end + context_chars)
    left = max(
        [text.rfind(marker, left_floor, start) for marker in (".", "!", "?", ";", "\n")]
    )
    left = left + 1 if left >= left_floor else left_floor
    right_candidates = [
        position
        for marker in (".", "!", "?", ";", "\n")
        for position in [text.find(marker, end, right_ceiling)]
        if position >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else right_ceiling
    return " ".join(text[left:right].split())


def _classify_relation(text: str) -> tuple[AccessionRelation, float]:
    if any(cue in text for cue in _PRIMARY_SELF_CUES):
        return AccessionRelation.PRIMARY_DATA_DEPOSITION, 0.98
    if any(cue in text for cue in _REUSE_CUES):
        return AccessionRelation.REUSED_DATASET, 0.9
    if any(cue in text for cue in _PRIMARY_DEPOSIT_CUES):
        return AccessionRelation.PRIMARY_DATA_DEPOSITION, 0.82
    if any(cue in text for cue in _SUPPLEMENTARY_CUES):
        return AccessionRelation.SUPPLEMENTARY_REFERENCE, 0.65
    return AccessionRelation.UNKNOWN, 0.35


def _mention_priority(mention: IdentifierMention) -> tuple[int, float]:
    priority = {
        AccessionRelation.REUSED_DATASET: 4,
        AccessionRelation.PRIMARY_DATA_DEPOSITION: 3,
        AccessionRelation.SUPPLEMENTARY_REFERENCE: 2,
        AccessionRelation.UNKNOWN: 1,
    }
    return priority[mention.relation], mention.confidence


def deduplicate_identifiers(values: Iterable[Identifier]) -> list[Identifier]:
    unique = {item.canonical: item for item in values}
    return sorted(unique.values(), key=lambda item: item.canonical)


def preferred_candidate_key(identifiers: Iterable[Identifier], fallback: str) -> str:
    priority = (
        "geo_series",
        "bioproject",
        "sra_project",
        "ega_study",
        "geo_sample",
        "sra_experiment",
        "sra_run",
    )
    by_namespace: dict[str, list[Identifier]] = {}
    for item in identifiers:
        by_namespace.setdefault(item.namespace.lower(), []).append(item)
    for namespace in priority:
        if namespace in by_namespace:
            item = sorted(by_namespace[namespace], key=lambda value: value.value)[0]
            return item.canonical
    return fallback

