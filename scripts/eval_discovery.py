#!/usr/bin/env python
"""Score discovery and curation judgement against a labelled replay corpus.

Two people changing gating logic in the same week cannot tell whose change
helped without a number that moves. This harness produces that number.

It replays real source records -- captured from live runs against Europe PMC,
GEO and SRA -- through the *actual* pipeline: the offline requirement compiler,
the query planner, record merging and the feasibility assessor. Nothing is
stubbed except the network itself, so a change to inference or to a gate shows
up here immediately.

Two properties are deliberate:

* The replay corpus stores only raw archive fields. Assay profiles and omics
  layers are re-derived on every run rather than replayed from the capture,
  which is what makes inference changes measurable.
* No LLM and no network. The scorecard must be reproducible by both people and
  by CI, and a number that depends on a model's mood is not a baseline.

Usage:
    python scripts/eval_discovery.py
    python scripts/eval_discovery.py --out var/scorecard_today.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from scdata_agent.application.discovery import DiscoveryWorkflow
from scdata_agent.domain.models import (
    CurationDecision,
    GateStatus,
    OmicsLayer,
    PairingLevel,
    QuerySpec,
    SourceRecord,
    best_pairing_level,
    pairing_rank,
    pairing_satisfies,
)
from scdata_agent.domain.requirements import RuleBasedRequirementCompiler
from scdata_agent.infrastructure.sources.common import infer_assay_profiles

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECORDS = REPO_ROOT / "eval" / "records.jsonl"
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden_questions.yaml"


class ReplayAdapter:
    """Serve captured records for one source, re-deriving everything derived.

    The adapter mirrors what the live adapters do at the only point that
    matters here: it calls ``infer_assay_profiles`` on the archive's own title
    and description rather than trusting an assay classification frozen into
    the capture.
    """

    def __init__(self, name: str, records: list[dict[str, Any]]) -> None:
        self.name = name
        self._records = records

    async def search(self, query: QuerySpec) -> list[SourceRecord]:
        return [self._build(raw) for raw in self._records]

    @staticmethod
    def _build(raw: dict[str, Any]) -> SourceRecord:
        profiles = infer_assay_profiles((raw.get("title"), raw.get("description")))
        return SourceRecord(
            **{key: value for key, value in raw.items() if key != "retrieved_at"},
            assay_profiles=profiles,
        )


@dataclass
class RecordScore:
    record_id: str
    kind: str
    profile_inferred: bool
    profile_expected: bool
    layers_ok: bool | None
    pairing_ok: bool | None
    pairing_overclaimed: bool | None
    observed_labels: list[str]
    observed_pairing: str | None


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def score_inference(
    records: list[dict[str, Any]], labels: dict[str, dict[str, Any]]
) -> tuple[list[RecordScore], dict[str, Any]]:
    """Score assay inference per record, independent of any question."""
    scores: list[RecordScore] = []
    for raw in records:
        label = labels.get(raw["record_id"])
        if label is None:
            continue
        profiles = infer_assay_profiles((raw.get("title"), raw.get("description")))
        observed_layers = {
            layer for profile in profiles for layer in profile.omics_layers
        }
        pairing = best_pairing_level(profiles)

        expected_layers = {OmicsLayer(item) for item in label.get("expect_layers", [])}
        floor = label.get("expect_pairing_at_least")
        ceiling = label.get("forbid_pairing_above")

        scores.append(
            RecordScore(
                record_id=raw["record_id"],
                kind=label["kind"],
                profile_inferred=bool(profiles),
                profile_expected=bool(label.get("expect_profile", False)),
                layers_ok=(
                    expected_layers <= observed_layers if expected_layers else None
                ),
                pairing_ok=(
                    (
                        pairing is not None
                        and pairing_satisfies(pairing, PairingLevel(floor))
                    )
                    if floor
                    else None
                ),
                pairing_overclaimed=(
                    (
                        pairing is not None
                        and pairing_rank(pairing) > pairing_rank(PairingLevel(ceiling))
                    )
                    if ceiling
                    else None
                ),
                observed_labels=[profile.label for profile in profiles],
                observed_pairing=pairing.value if pairing else None,
            )
        )

    def rate(hits: int, total: int) -> float | None:
        return round(hits / total, 4) if total else None

    expected = [item for item in scores if item.profile_expected]
    layer_checked = [item for item in scores if item.layers_ok is not None]
    pairing_checked = [item for item in scores if item.pairing_ok is not None]
    ceiling_checked = [item for item in scores if item.pairing_overclaimed is not None]

    summary = {
        "records_labelled": len(scores),
        # The headline number for assay inference: how often a profile that
        # should have been inferable actually was. Everything downstream that
        # says "pairing:unknown" traces back to this.
        "profile_inferred_rate": rate(
            sum(1 for item in expected if item.profile_inferred), len(expected)
        ),
        "profile_missing": sorted(
            item.record_id for item in expected if not item.profile_inferred
        ),
        "layer_recall": rate(
            sum(1 for item in layer_checked if item.layers_ok), len(layer_checked)
        ),
        "pairing_recall": rate(
            sum(1 for item in pairing_checked if item.pairing_ok), len(pairing_checked)
        ),
        "pairing_missed": sorted(
            item.record_id for item in pairing_checked if not item.pairing_ok
        ),
        # The expensive error: a standalone assay reporting a pairing claim its
        # data cannot support. Must be zero; anything else admits unpaired data
        # into a corpus that was asked for paired data.
        "pairing_overclaim_rate": rate(
            sum(1 for item in ceiling_checked if item.pairing_overclaimed),
            len(ceiling_checked),
        ),
        "pairing_overclaimed": sorted(
            f"{item.record_id}:{item.observed_pairing}"
            for item in ceiling_checked
            if item.pairing_overclaimed
        ),
    }
    return scores, summary


async def score_question(
    spec: dict[str, Any],
    records: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Run one question through the real workflow and score the outcome."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for raw in records:
        by_source.setdefault(raw["source_name"], []).append(raw)
    adapters = [ReplayAdapter(name, rows) for name, rows in sorted(by_source.items())]

    workflow = DiscoveryWorkflow(
        adapters=adapters,
        ledger=None,
        requirement_compiler=RuleBasedRequirementCompiler(),
    )
    result = await workflow.run(spec["question"], max_results=200)

    kind_by_record = {key: value["kind"] for key, value in labels.items()}
    reports = {report.candidate_id: report for report in result.feasibility_reports}

    decisions: Counter[str] = Counter()
    layer_gate: Counter[str] = Counter()
    # Of the candidates a topic filter would admit, how many own data?
    layer_pass_primary = 0
    layer_pass_total = 0
    methods_papers_passing: list[str] = []
    # Records this question must *not* admit. `deposition_precision` cannot
    # express this: excluding a record that should never have passed lowers the
    # number, so a fix and a regression look alike. A named list does not move
    # when the corpus does, which is what makes it worth arguing about.
    forbidden = set(spec.get("forbid_layer_pass", []))
    forbidden_passing: list[str] = []

    for candidate in result.candidates:
        report = reports.get(candidate.candidate_id)
        if report is None:
            continue
        kinds = {
            kind_by_record[record_id]
            for record_id in candidate.source_record_ids
            if record_id in kind_by_record
        }
        # A merged candidate counts as owning data if any of its records does.
        is_primary = "primary_deposition" in kinds
        decisions[report.decision.value] += 1
        gate = report.dimensions["layer_relevance"].status
        layer_gate[gate.value] += 1
        if gate == GateStatus.PASS:
            layer_pass_total += 1
            layer_pass_primary += int(is_primary)
            if not is_primary:
                methods_papers_passing.append(candidate.title[:70])
            forbidden_passing.extend(
                sorted(forbidden.intersection(candidate.source_record_ids))
            )

    recalled = set()
    for candidate in result.candidates:
        recalled.update(candidate.source_record_ids)
    expected_recall = spec.get("expect_recalled", [])

    return {
        "id": spec["id"],
        "question": spec["question"].strip(),
        "requirement": {
            "required_omics_layers": [
                item.value for item in result.requirement.required_omics_layers
            ],
            "minimum_pairing_level": (
                result.requirement.minimum_pairing_level.value
                if result.requirement.minimum_pairing_level
                else None
            ),
            "compiler": result.requirement.compiler,
        },
        "candidates": len(result.candidates),
        "decisions": {
            decision.value: decisions.get(decision.value, 0)
            for decision in CurationDecision
        },
        "layer_gate": {
            status.value: layer_gate.get(status.value, 0) for status in GateStatus
        },
        # The precision number P0-2 has to move: among candidates a topic filter
        # admits, the share that actually announce data of their own.
        "deposition_precision": (
            round(layer_pass_primary / layer_pass_total, 4)
            if layer_pass_total
            else None
        ),
        "non_primary_passing_layer_gate": sorted(methods_papers_passing),
        # Must stay empty. Anything here is a record being admitted to a
        # question its data cannot answer.
        "forbidden_passing_layer_gate": sorted(set(forbidden_passing)),
        "expected_recall_missing": sorted(
            set(expected_recall) - recalled
        ),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    golden = yaml.safe_load(args.golden.read_text(encoding="utf-8"))
    records = load_records(args.records)
    labels = golden["records"]

    per_record, inference = score_inference(records, labels)
    questions = [
        await score_question(spec, records, labels) for spec in golden["questions"]
    ]

    scorecard = {
        "records": len(records),
        "inference": inference,
        "questions": questions,
        "by_kind": dict(Counter(item.kind for item in per_record)),
    }

    text = json.dumps(scorecard, indent=2, ensure_ascii=False, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
