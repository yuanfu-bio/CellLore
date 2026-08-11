"""LLM-authored round-0 search queries, replacing fixed technology-term tables.

``domain.requirements.QueryPlanner`` still exists and still owns the one part
that must stay deterministic and guaranteed: the organism/biological AND
filter, derived from typed ``ResearchRequirement`` fields. What it should not
own any longer is the technology/assay OR clause -- ``MODALITY_QUERY_TERMS``/
``SAME_CELL_QUERY_TERMS`` are a closed vocabulary that a real GEO record
regularly falls outside of (a study can be genuine 10x Multiome data and
never use that phrase, see GSE228428's own record: "10x Genomics snRNA-seq
and snATAC-seq"). ``LLMQueryPlanner`` asks the model to author that clause
itself, using the selected profile's ``search_hints`` as a vocabulary/seed --
never as a template string-formatted back into a fixed shape, which would
just relocate the same closed-vocabulary problem (see ``SearchHints``' own
docstring).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from celllore.domain.curation.profile import CurationProfile
from celllore.domain.curation.profiles import profile_by_id
from celllore.domain.models import QueryPlan, ResearchRequirement
from celllore.domain.requirements import QueryPlanner
from celllore.infrastructure.llm.research_planner import (
    ChatCompletionClient,
    _structured_call,
)
from celllore.infrastructure.llm.router import ModelRouter, ReasoningProfile

_CONTROL_OR_QUOTE = re.compile(r'[\x00-\x1f"\']')
_MAX_TERMS = 12
_MAX_TERM_LENGTH = 60


class _TechnologyQueryTerms(BaseModel):
    terms: list[str] = Field(
        default_factory=list,
        max_length=_MAX_TERMS,
        description=(
            "Alternative phrasings a real record naming this assay might use: "
            "named co-assay technologies, bare/generic terms, and any "
            "wording a paper or GEO record plausibly uses -- not just the "
            "seed vocabulary given, which is illustrative, not exhaustive."
        ),
    )
    rationale: str = Field(default="", max_length=300)


class LLMQueryPlanner:
    """Drop-in replacement for ``QueryPlanner`` for the ``use_llm=True`` path.

    Same ``.plan()`` shape as ``domain.requirements.QueryPlanner`` except
    async (it makes one real LLM call) and requires no ``modality_terms_
    override`` from the caller -- it computes that override itself and
    delegates the rest to the deterministic planner.
    """

    def __init__(self, client: ChatCompletionClient, deterministic: QueryPlanner | None = None) -> None:
        self._client = client
        self._deterministic = deterministic or QueryPlanner()

    async def plan(self, requirement: ResearchRequirement, max_results: int = 20) -> QueryPlan:
        profile = _resolve_profile(requirement.profile_id)
        override = await self._technology_clause(requirement, profile)
        return self._deterministic.plan(
            requirement, max_results, modality_terms_override=override
        )

    async def _technology_clause(
        self, requirement: ResearchRequirement, profile: CurationProfile | None
    ) -> str | None:
        seeds = profile.search_hints if profile else None
        try:
            draft = await _structured_call(
                self._client,
                _TechnologyQueryTerms,
                (
                    "You are authoring a boolean search query clause for a public "
                    "repository search (Europe PMC / NCBI GEO / NCBI SRA), naming "
                    "the specific assay/technology this research question needs. "
                    "The seed vocabulary given is illustrative examples from a "
                    "curated profile, never an exhaustive or fixed list -- if the "
                    "assay is a technology not in the seeds, name it in your own "
                    "words the way a real paper or GEO record would. Prefer short, "
                    "high-precision terms (named technologies, and generic bare "
                    "words like the assay category) over long descriptive phrases, "
                    "which rarely match real record text verbatim. Do not include "
                    "organism, tissue, disease, or cell-type terms -- those are "
                    "handled separately. Return JSON only."
                ),
                {
                    "research_question": requirement.question,
                    "required_omics_layers": [
                        str(layer) for layer in requirement.required_omics_layers
                    ],
                    "pairing_requirement": (
                        [str(level) for level in requirement.pairing_requirement.acceptable_levels]
                        if requirement.pairing_requirement
                        else None
                    ),
                    "seed_technology_vocabulary": list(seeds.technology_seeds) if seeds else [],
                    "seed_pairing_phrases": list(seeds.pairing_phrase_seeds) if seeds else [],
                },
                max_tokens=1_200,
            )
        except Exception:
            # Unlike research planning or requirement compilation, this is
            # deliberately fail-open rather than fail-fast: it enriches one
            # clause of a query the deterministic tables can already build on
            # their own, and the bounded supplementary-search loop downstream
            # (ResourceDiscoveryAgent._review_results) gets its own
            # independent chance to compensate for a narrow round-0 query
            # regardless of whether this particular call succeeded. A
            # transient failure here should narrow recall, not abort the run.
            return None
        terms = _sanitize_terms(
            draft.terms,
            forbidden_terms=profile.search_hints.forbidden_query_terms if profile else (),
        )
        if not terms:
            return None
        return " OR ".join(f'"{term}"' for term in terms)


def _resolve_profile(profile_id: str | None) -> CurationProfile | None:
    if not profile_id:
        return None
    try:
        return profile_by_id(profile_id)
    except KeyError:
        return None


def configured_query_planner(use_llm: bool = True) -> QueryPlanner | LLMQueryPlanner:
    """Return the LLM-backed query planner, or the deterministic one when
    explicitly requested via ``use_llm=False`` (tests/offline debugging only).

    Round-0 query authoring is a core dependency of this agent, same as
    requirement compilation and research planning -- if the caller asked for
    LLM planning and no model is actually configured, that is a deployment
    error and must fail fast (``ModelRouter.from_environment()``'s
    ``RuntimeError`` propagates) rather than silently downgrading to the
    closed technology-term tables nobody asked for and no downstream
    reviewer will know happened.
    """
    if not use_llm:
        return QueryPlanner()
    client = ModelRouter.from_environment().client(ReasoningProfile.DISCOVERY)
    return LLMQueryPlanner(client)


def _sanitize_terms(
    terms: list[str], *, forbidden_terms: tuple[str, ...] = ()
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    forbidden = {term.casefold().strip() for term in forbidden_terms}
    for term in terms:
        candidate = re.sub(r"\s+", " ", _CONTROL_OR_QUOTE.sub(" ", str(term))).strip()
        if not candidate or len(candidate) > _MAX_TERM_LENGTH:
            continue
        key = candidate.casefold()
        if key in forbidden:
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(candidate)
        if len(cleaned) >= _MAX_TERMS:
            break
    return cleaned
