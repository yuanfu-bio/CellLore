"""Research-question compilation and source-specific query planning."""

from __future__ import annotations

import re

from scdata_agent.domain.models import (
    DataRole,
    Modality,
    QueryPlan,
    QuerySpec,
    ResearchRequirement,
)


ORGANISM_ALIASES: dict[str, tuple[str, ...]] = {
    "Homo sapiens": ("human", "humans", "人类", "人体", "患者"),
    "Mus musculus": ("mouse", "mice", "murine", "小鼠", "鼠"),
}

TISSUE_ALIASES: dict[str, tuple[str, ...]] = {
    "lung": ("lung", "pulmonary", "肺"),
    "blood": ("blood", "pbmc", "peripheral blood", "外周血", "血液"),
    "brain": ("brain", "cortex", "cerebral", "脑", "皮层"),
    "bone marrow": ("bone marrow", "骨髓"),
    "liver": ("liver", "hepatic", "肝"),
    "kidney": ("kidney", "renal", "肾"),
    "heart": ("heart", "cardiac", "心脏", "心肌"),
    "tumor": ("tumor", "tumour", "cancer tissue", "肿瘤组织"),
}

DISEASE_ALIASES: dict[str, tuple[str, ...]] = {
    "lung cancer": ("lung cancer", "nsclc", "non-small cell lung cancer", "luad", "lusc", "肺癌", "非小细胞肺癌"),
    "cancer": ("cancer", "tumor", "tumour", "carcinoma", "癌症", "肿瘤"),
    "leukemia": ("leukemia", "leukaemia", "白血病"),
    "healthy": ("healthy", "normal", "control", "健康", "正常", "对照"),
}

CELL_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "immune cell": ("immune cell", "immune cells", "免疫细胞"),
    "T cell": ("t cell", "t-cell", "t lymphocyte", "T细胞"),
    "CD8 T cell": ("cd8 t", "cd8+ t", "cd8 t cell", "cd8阳性t", "cd8 t细胞"),
    "macrophage": ("macrophage", "巨噬细胞"),
    "tumor cell": ("tumor cell", "cancer cell", "肿瘤细胞"),
}

MODALITY_ALIASES: dict[Modality, tuple[str, ...]] = {
    Modality.SNATAC: ("snatac", "single nucleus atac", "single-nucleus atac"),
    Modality.SCATAC: (
        "scatac",
        "single cell atac",
        "single-cell atac",
        "单细胞atac",
        "单细胞染色质",
        "chromatin accessibility",
    ),
    Modality.MULTIOME_RNA_ATAC: (
        "multiome",
        "rna+atac",
        "rna atac",
        "joint rna and atac",
        "多组学",
    ),
    Modality.MATCHED_SCRNA_SCATAC: (
        "matched scrna scatac", "paired scrna scatac", "sample-matched rna atac",
        "配对scrna和scatac", "配对rna和atac",
    ),
    Modality.MULTIOME_ATAC_PROTEIN: (
        "asap-seq", "issaac-seq", "atac protein", "atac+protein",
    ),
    Modality.MULTIOME_RNA_ATAC_PROTEIN: (
        "tea-seq", "dogma-seq", "rna atac protein", "trimodal", "tri-modal",
    ),
}

TASK_ALIASES: dict[str, tuple[str, ...]] = {
    "cell_type_annotation": ("annotation", "cell type", "细胞类型注释", "细胞注释"),
    "cross_dataset_generalization": (
        "generalization",
        "cross-dataset",
        "跨数据集",
        "泛化",
    ),
    "clustering": ("clustering", "cluster", "聚类", "visualization", "可视化"),
    "sparse_signal_recovery": ("imputation", "recovery", "稀疏", "恢复"),
    "representation_learning": (
        "pretrain",
        "pre-training",
        "foundation model",
        "representation",
        "预训练",
        "基础模型",
        "表征",
    ),
}


def _contains(text: str, aliases: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return any(alias.casefold() in normalized for alias in aliases)


def _extract_aliases(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    return [label for label, aliases in mapping.items() if _contains(text, aliases)]


class RuleBasedRequirementCompiler:
    """Conservative baseline compiler that never invents domain entities."""

    def compile(self, question: str) -> ResearchRequirement:
        organisms = _extract_aliases(question, ORGANISM_ALIASES)
        tissues = _extract_aliases(question, TISSUE_ALIASES)
        diseases = _extract_aliases(question, DISEASE_ALIASES)
        cell_types = _extract_aliases(question, CELL_TYPE_ALIASES)
        modalities = [
            modality
            for modality, aliases in MODALITY_ALIASES.items()
            if _contains(question, aliases)
        ]
        downstream_tasks = _extract_aliases(question, TASK_ALIASES)

        if not modalities:
            modalities = [Modality.SCATAC, Modality.MULTIOME_RNA_ATAC]

        minimum_donors = None
        donor_match = re.search(
            r"(?:at least|minimum|至少(?:需要)?|不少于)\s*(\d+)\s*(?:个|名|位)?\s*(?:donors?|供体|患者)",
            question,
            flags=re.IGNORECASE,
        )
        if donor_match:
            minimum_donors = int(donor_match.group(1))

        required_roles = {
            DataRole.CELL_METADATA,
            DataRole.SAMPLE_METADATA,
        }
        preferred_roles = {
            DataRole.RAW_READS,
            DataRole.PEAK_MATRIX,
            DataRole.QC_METRICS,
        }
        if any(
            modality in modalities
            for modality in (
                Modality.SCATAC, Modality.SNATAC, Modality.MULTIOME_RNA_ATAC,
                Modality.MATCHED_SCRNA_SCATAC, Modality.MULTIOME_ATAC_PROTEIN,
                Modality.MULTIOME_RNA_ATAC_PROTEIN, Modality.ATAC_OTHER_MULTIOMICS,
            )
        ):
            required_roles.add(DataRole.FRAGMENTS)
            preferred_roles.add(DataRole.FRAGMENT_INDEX)
        if any(modality in modalities for modality in (
            Modality.MULTIOME_RNA_ATAC, Modality.MATCHED_SCRNA_SCATAC,
            Modality.MULTIOME_RNA_ATAC_PROTEIN,
        )):
            required_roles.add(DataRole.RNA_MATRIX)
        if any(modality in modalities for modality in (
            Modality.MULTIOME_RNA_ATAC, Modality.MULTIOME_ATAC_PROTEIN,
            Modality.MULTIOME_RNA_ATAC_PROTEIN,
        )):
            required_roles.add(DataRole.BARCODE_LINKAGE)
        if any(modality in modalities for modality in (
            Modality.MULTIOME_ATAC_PROTEIN, Modality.MULTIOME_RNA_ATAC_PROTEIN,
        )):
            required_roles.add(DataRole.PROTEIN_MATRIX)

        search_terms = _unique(
            [
                *organisms,
                *tissues,
                *diseases,
                *cell_types,
                *[item.value for item in modalities],
            ]
        )

        lowered = question.casefold()
        excluded_sources = ["cell_line"] if any(term in lowered for term in ("cell line", "cell-line", "细胞系")) and any(term in lowered for term in ("exclude", "excluding", "排除", "不接受")) else []
        exclusions = ["controlled_access"] if any(term in lowered for term in ("controlled access", "受控访问")) and any(term in lowered for term in ("exclude", "excluding", "排除", "不接受")) else []
        evaluation_policy = {"reserve_independent_studies": any(term in lowered for term in ("independent study", "independent studies", "独立研究")), "holdout_unit": "study" if any(term in lowered for term in ("independent study", "independent studies", "独立研究")) else None, "prevent_lineage_leakage": any(term in lowered for term in ("independent study", "独立研究", "泛化测试"))}
        preferred_modalities = [Modality.MULTIOME_RNA_ATAC] if Modality.MULTIOME_RNA_ATAC in modalities and any(term in lowered for term in ("prefer", "priority", "优先")) else []
        return ResearchRequirement(
            question=question,
            organisms=organisms,
            tissues=tissues,
            diseases=diseases,
            cell_types=cell_types,
            modalities=modalities,
            acceptable_modalities=modalities,
            preferred_modalities=preferred_modalities,
            required_data_roles=sorted(required_roles, key=str),
            preferred_data_roles=sorted(preferred_roles, key=str),
            minimum_donors=minimum_donors,
            excluded_biological_sources=excluded_sources,
            exclusions=exclusions,
            evaluation_policy=evaluation_policy,
            downstream_tasks=downstream_tasks,
            search_terms=search_terms,
        )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


MODALITY_QUERY_TERMS = (
    '"single-cell ATAC-seq" OR scATAC-seq OR snATAC-seq OR '
    '"single nucleus ATAC-seq" OR "10x Multiome" OR "RNA ATAC multiome"'
)


class QueryPlanner:
    def plan(
        self, requirement: ResearchRequirement, max_results: int = 20
    ) -> QueryPlan:
        biological_terms = _unique(
            [
                *requirement.tissues,
                *requirement.diseases,
                *requirement.cell_types,
            ]
        )
        biological_query = " OR ".join(f'"{term}"' for term in biological_terms)
        organism_query = " OR ".join(
            f'"{organism}"' for organism in requirement.organisms
        )

        common_parts = [f"({MODALITY_QUERY_TERMS})"]
        if biological_query:
            common_parts.append(f"({biological_query})")
        if organism_query:
            common_parts.append(f"({organism_query})")
        common_query = " AND ".join(common_parts)

        geo_parts = [f"({MODALITY_QUERY_TERMS})"]
        if biological_query:
            geo_parts.append(f"({biological_query})")
        if requirement.organisms:
            geo_parts.append(
                "("
                + " OR ".join(
                    f'"{organism}"[Organism]' for organism in requirement.organisms
                )
                + ")"
            )
        geo_query = " AND ".join(geo_parts)

        return QueryPlan(
            requirement=requirement,
            queries=[
                QuerySpec(
                    source="europe_pmc",
                    query=common_query,
                    purpose="Find publications and preprints describing relevant data.",
                    max_results=max_results,
                ),
                QuerySpec(
                    source="ncbi_geo",
                    query=geo_query,
                    purpose="Find GEO studies and processed data records.",
                    max_results=max_results,
                ),
                QuerySpec(
                    source="ncbi_sra",
                    query=common_query,
                    purpose="Find raw sequencing studies and experiments.",
                    max_results=max_results,
                ),
            ],
            uncovered_sources=[
                "encode",
                "cellxgene",
                "single_cell_portal",
                "controlled_access",
            ],
        )



CORPUS_QUERY_FAMILIES: tuple[tuple[str, str], ...] = (
    (
        "standalone_scatac",
        '("single-cell ATAC-seq" OR "single nucleus ATAC-seq" OR scATAC-seq OR snATAC-seq)',
    ),
    (
        "rna_atac_coassay",
        '("10x Multiome" OR "RNA ATAC multiome" OR SNARE-seq OR SHARE-seq OR sci-CAR OR Paired-seq)',
    ),
    (
        "matched_scrna_scatac",
        '((scATAC-seq OR snATAC-seq) AND (scRNA-seq OR "single-cell RNA-seq") AND (paired OR matched OR "same sample" OR "same donor"))',
    ),
    (
        "atac_other_multiomics",
        '(ASAP-seq OR ISSAAC-seq OR TEA-seq OR DOGMA-seq OR ((scATAC-seq OR snATAC-seq) AND (protein OR epitope OR trimodal)))',
    ),
)


class CorpusQueryPlanner:
    """Exhaustive query families for public scATAC pretraining-corpus discovery."""

    def plan(
        self, requirement: ResearchRequirement, max_results: int = 200
    ) -> QueryPlan:
        biological_terms = _unique([
            *requirement.tissues, *requirement.diseases, *requirement.cell_types
        ])
        queries: list[QuerySpec] = []
        for family, base_query in CORPUS_QUERY_FAMILIES:
            for source in ("europe_pmc", "ncbi_geo", "ncbi_sra"):
                parts = [base_query]
                if biological_terms:
                    parts.append("(" + " OR ".join(f'"{term}"' for term in biological_terms) + ")")
                if requirement.organisms:
                    if source == "ncbi_geo":
                        parts.append("(" + " OR ".join(f'"{organism}"[Organism]' for organism in requirement.organisms) + ")")
                    else:
                        parts.append("(" + " OR ".join(f'"{organism}"' for organism in requirement.organisms) + ")")
                queries.append(QuerySpec(
                    source=source,
                    query=" AND ".join(parts),
                    purpose=f"Corpus discovery family: {family}",
                    max_results=max_results,
                ))
        return QueryPlan(
            requirement=requirement,
            queries=queries,
            uncovered_sources=[
                "encode", "biostudies_arrayexpress", "single_cell_portal",
                "human_cell_atlas", "ega_dbgap_metadata",
            ],
        )
