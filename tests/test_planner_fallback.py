"""The offline planner fallback must not invent a clinical study design.

When the model's strategy branch exceeds its latency budget, a hardcoded
template takes over. That template previously assumed a disease-mechanism
study, so a purely methodological question (\"build a foundation model\") was
handed two ESSENTIAL clinical-cohort needs it never asked for, which polluted
capability_gaps and depressed readiness.
"""

from celllore.infrastructure.llm.research_planner import (
    _fallback_compact_intent,
    _fallback_compact_strategy,
    _mentions_clinical_outcomes,
)

METHODS_QUESTION_ZH = (
    "面向单细胞表观组数据的自监督生物基础模型：聚焦scATAC-seq与单细胞multiome公开数据，"
    "构建数据策展、统一表示、自监督预训练与评估基础模型原型"
)
METHODS_QUESTION_EN = (
    "Self-supervised foundation model for single-cell epigenomics: curate public "
    "scATAC-seq and multiome data, pretrain a unified representation, and evaluate it"
)
CLINICAL_QUESTION_ZH = "肺癌患者免疫微环境的scATAC-seq研究，关联治疗响应与生存预后"
CLINICAL_QUESTION_EN = (
    "scATAC-seq of the lung cancer immune microenvironment linked to "
    "treatment response and patient survival"
)


def _need_names(question: str) -> list[str]:
    intent = _fallback_compact_intent(question)
    return [item.name for item in _fallback_compact_strategy(question, intent).data_needs]


def test_methods_question_gets_no_clinical_cohort_need():
    for question in (METHODS_QUESTION_ZH, METHODS_QUESTION_EN):
        names = " ".join(_need_names(question))
        assert "临床" not in names
        assert "clinical" not in names.casefold()
        assert "空间" not in names
        assert "spatial" not in names.casefold()


def test_clinical_question_still_gets_the_clinical_cohort_need():
    """The template is not simply deleted: real clinical studies still need it."""
    zh = " ".join(_need_names(CLINICAL_QUESTION_ZH))
    assert "独立临床结局验证队列" in zh
    en = " ".join(_need_names(CLINICAL_QUESTION_EN))
    assert "clinical-outcome" in en.casefold()


def test_single_cell_discovery_need_is_always_present():
    """Removing clinical needs must not strip the core epigenomics need."""
    for question in (METHODS_QUESTION_ZH, METHODS_QUESTION_EN, CLINICAL_QUESTION_ZH):
        assert _need_names(question), "fallback produced no data needs at all"


def test_clinical_cue_detection_covers_both_languages():
    for question in (CLINICAL_QUESTION_ZH, CLINICAL_QUESTION_EN):
        assert _mentions_clinical_outcomes(question, _fallback_compact_intent(question))
    for question in (METHODS_QUESTION_ZH, METHODS_QUESTION_EN):
        assert not _mentions_clinical_outcomes(
            question, _fallback_compact_intent(question)
        )


def test_methods_fallback_records_why_clinical_needs_were_omitted():
    intent = _fallback_compact_intent(METHODS_QUESTION_EN)
    strategy = _fallback_compact_strategy(METHODS_QUESTION_EN, intent)
    assert any("clinical" in item.casefold() for item in strategy.assumptions)
