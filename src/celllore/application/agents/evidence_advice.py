"""Bounded LLM advice used by the progressive Evidence Curator."""

from __future__ import annotations

import json

from langchain.tools import tool
from pydantic import BaseModel, Field

from celllore.application.agents.shared import AgentContext
from celllore.infrastructure.llm.agent_runtime import run_bounded_agent
from celllore.infrastructure.llm.router import ReasoningProfile


class EvidenceAdvice(BaseModel):
    summary: str
    next_action: str
    priority_candidate_ids: list[str] = Field(default_factory=list)


async def evidence_advice(context: AgentContext, candidates: list[dict]) -> EvidenceAdvice:
    fallback = EvidenceAdvice(
        summary="Candidate relevance is provisional until repository metadata and file manifests are verified.",
        next_action="resolve repository file manifests for shortlisted candidates",
    )
    if not context.policy.get("use_llm", True):
        return fallback

    @tool
    def list_candidates() -> list[dict]:
        """Read candidate IDs, titles, identifiers and existing evidence IDs."""
        return candidates

    @tool
    def read_candidate_evidence(candidate_id: str) -> dict:
        """Read already-collected evidence identifiers for one supplied candidate ID."""
        for candidate in candidates:
            if candidate["candidate_id"] == candidate_id:
                return {"candidate_id": candidate_id, "evidence_ids": candidate["evidence_ids"], "source_record_ids": candidate["source_record_ids"]}
        return {"error": "unknown candidate_id"}

    try:
        response = await run_bounded_agent(
            profile=ReasoningProfile.REVIEW, tools=[list_candidates, read_candidate_evidence],
            system_prompt="You are an evidence reviewer for public single-cell epigenomics data. Use only read-only supplied tools. Return only JSON with summary, next_action, priority_candidate_ids. Never invent identifiers or claim file availability.",
            user_prompt=json.dumps({"question": context.question}, ensure_ascii=False),
        )
        data = json.loads(response[response.find("{"): response.rfind("}") + 1])
        advice = EvidenceAdvice.model_validate(data)
        valid_ids = {item["candidate_id"] for item in candidates}
        return advice.model_copy(update={"priority_candidate_ids": [item for item in advice.priority_candidate_ids if item in valid_ids]})
    except Exception:
        return fallback
