"""Research-question compilation and source-specific query planning."""

from __future__ import annotations

import re

from scdata_agent.domain.models import (
    BundleSchema,
    DataRole,
    Modality,
    PairingLevel,
    QueryPlan,
    QuerySpec,
    ResearchRequirement,
    pairing_rank,
)


# An AnnData/MuData object carries the matrix and the cell axis together, so it
# satisfies any group it appears in. Naming it once keeps that intent explicit.
_ACCESSIBILITY = [DataRole.FRAGMENTS, DataRole.PEAK_MATRIX, DataRole.STANDARD_OBJECT]
# A cell-by-feature matrix cannot exist without cell identifiers, so it defines
# the cell axis on its own; a separate metadata table is a bonus, not a gate.
_CELL_AXIS = [
    DataRole.CELL_METADATA,
    DataRole.STANDARD_OBJECT,
    DataRole.PEAK_MATRIX,
    DataRole.RNA_MATRIX,
]

BUNDLE_SCHEMAS: tuple[BundleSchema, ...] = (
    BundleSchema(
        schema_id="scatac_standalone",
        label="Standalone single-cell ATAC",
        modalities=[Modality.SCATAC, Modality.SNATAC],
        # Deliberately does NOT require an RNA matrix or barcode linkage:
        # a standalone scATAC study has neither and is still training-ready.
        required_role_groups=[_ACCESSIBILITY, _CELL_AXIS],
        preferred_roles=[
            DataRole.FRAGMENT_INDEX, DataRole.PEAKS,
            DataRole.QC_METRICS, DataRole.SAMPLE_METADATA,
        ],
    ),
    BundleSchema(
        schema_id="multiome_same_cell",
        label="Joint RNA + ATAC measured in the same cell or nucleus",
        modalities=[Modality.MULTIOME_RNA_ATAC],
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT],
            # Same-cell assays link modalities by construction; an explicit map
            # or a joint object both count as evidence of that linkage.
            [DataRole.BARCODE_LINKAGE, DataRole.CELL_METADATA, DataRole.STANDARD_OBJECT],
        ],
        preferred_roles=[
            DataRole.FRAGMENT_INDEX, DataRole.PEAKS,
            DataRole.QC_METRICS, DataRole.SAMPLE_METADATA,
        ],
    ),
    BundleSchema(
        schema_id="multiome_trimodal",
        label="RNA + ATAC + surface protein in the same cell (TEA-seq, DOGMA-seq)",
        modalities=[Modality.MULTIOME_RNA_ATAC_PROTEIN],
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT],
            # The third layer is the point of a trimodal assay: without it the
            # bundle is a two-layer multiome mislabelled as trimodal.
            [DataRole.PROTEIN_MATRIX, DataRole.STANDARD_OBJECT],
            [DataRole.BARCODE_LINKAGE, DataRole.CELL_METADATA, DataRole.STANDARD_OBJECT],
        ],
        preferred_roles=[
            DataRole.FRAGMENT_INDEX, DataRole.PEAKS,
            DataRole.QC_METRICS, DataRole.SAMPLE_METADATA,
        ],
    ),
    BundleSchema(
        schema_id="matched_scrna_scatac",
        # Separate assays on shared material. Kept apart from the same-cell
        # schema because sharing a sample is a much weaker claim than sharing
        # a cell, and conflating them let matched studies pass as multiome.
        label="Separate scRNA and scATAC on matched samples or donors",
        modalities=[Modality.MATCHED_SCRNA_SCATAC],
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT],
            _CELL_AXIS,
        ],
        preferred_roles=[
            DataRole.FRAGMENT_INDEX, DataRole.PEAKS,
            DataRole.QC_METRICS, DataRole.SAMPLE_METADATA,
        ],
    ),
    BundleSchema(
        schema_id="atac_protein",
        label="ATAC + surface protein",
        modalities=[Modality.MULTIOME_ATAC_PROTEIN],
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.PROTEIN_MATRIX, DataRole.STANDARD_OBJECT],
            _CELL_AXIS,
        ],
        preferred_roles=[DataRole.QC_METRICS, DataRole.SAMPLE_METADATA],
    ),
    BundleSchema(
        schema_id="raw_reads",
        label="Raw sequencing reads requiring reprocessing",
        # Applies to any modality: FASTQ is a legitimate, if expensive, input.
        modalities=[],
        required_role_groups=[[DataRole.RAW_READS]],
        preferred_roles=[DataRole.SAMPLE_METADATA],
    ),
)


def applicable_bundle_schemas(
    modalities: list[Modality] | set[Modality],
) -> list[BundleSchema]:
    """Select the schemas a candidate could plausibly satisfy.

    A schema with no declared modality (``raw_reads``) always applies. When the
    candidate declares no modality at all, every schema is offered rather than
    none, so an unlabelled deposition is judged on what it actually contains.
    """
    observed = set(modalities)
    if not observed:
        return list(BUNDLE_SCHEMAS)
    return [
        schema
        for schema in BUNDLE_SCHEMAS
        if not schema.modalities or observed & set(schema.modalities)
    ]


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
    Modality.SCRNA: (
        "scrna", "single cell rna", "single-cell rna", "单细胞rna", "单细胞转录组",
    ),
    Modality.ATAC_OTHER_MULTIOMICS: (
        "atac other multiomics", "atac multiomics", "atac 多组学",
    ),
}

# Phrasings that ask for the two omics layers to come from one physical cell,
# as opposed to merely appearing in the same study. Sample- and donor-level
# matching is deliberately absent: it is a weaker claim and gets its own tier.
SAME_CELL_PAIRING_ALIASES: tuple[str, ...] = (
    "same cell", "same-cell", "same nucleus", "same-nucleus",
    "single cell multiome", "single-cell multiome", "joint profiling",
    "simultaneous profiling", "co-assay", "coassay", "co-profiling",
    "paired multiome", "paired rna and atac", "paired scatac",
    "10x multiome", "snare-seq", "share-seq", "sci-car", "paired-seq",
    "tea-seq", "dogma-seq",
    "配对", "同细胞", "同一细胞", "同核", "同一细胞核", "共测", "联合检测", "同时测量",
)

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


# The same-cell tier: these are the only modalities whose own name asserts that
# RNA and ATAC came from one physical cell or nucleus. MATCHED_SCRNA_SCATAC is
# deliberately excluded -- it names assays run separately on matched material,
# a materially weaker claim (see BUNDLE_SCHEMAS' matched_scrna_scatac schema).
_SAME_CELL_MODALITIES: tuple[Modality, ...] = (
    Modality.MULTIOME_RNA_ATAC,
    Modality.MULTIOME_RNA_ATAC_PROTEIN,
)


def restrict_modalities_to_pairing(
    modalities: list[Modality], minimum_pairing_level: PairingLevel | None
) -> list[Modality]:
    """Narrow a modality list to what a same-cell pairing requirement allows.

    Both the rule-based compiler and the LLM-derived planner path assemble a
    modality list on their own terms; without this shared step, whichever path
    happened to also mention standalone scATAC or study-level co-reporting
    would recall it alongside a question that explicitly asked for pairing.
    Applying the same rule regardless of which compiler produced the list is
    what keeps a same-cell requirement from being silently bypassed by taking
    the LLM route.
    """
    if pairing_rank(minimum_pairing_level or PairingLevel.NOT_APPLICABLE) < pairing_rank(
        PairingLevel.SAME_CELL
    ):
        return modalities
    observed = [item for item in modalities if item in _SAME_CELL_MODALITIES]
    return observed or list(_SAME_CELL_MODALITIES)


def roles_for_modalities(
    modalities: list[Modality],
) -> tuple[list[DataRole], list[DataRole]]:
    """Canonical required/preferred DataRoles implied by a modality set.

    This is the single source of truth for what a coarse ResearchRequirement
    should advise a candidate needs; it previously existed as three
    independently drifting copies (this function, the compiler in
    ``infrastructure/llm/requirement_compiler.py``, and one in
    ``infrastructure/llm/research_planner.py``) and only this one -- the
    rule-based compiler's inline version -- had the PROTEIN_MATRIX branch.
    The per-candidate acquisition gate in ``inventory_reconcile``/
    ``BUNDLE_SCHEMAS`` is the actual admission decision; this only seeds the
    requirement's advisory fields and the compilers' role recommendations.
    """
    required_roles = {DataRole.CELL_METADATA, DataRole.SAMPLE_METADATA}
    preferred_roles = {DataRole.RAW_READS, DataRole.PEAK_MATRIX, DataRole.QC_METRICS}
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
    return sorted(required_roles, key=str), sorted(preferred_roles, key=str)


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

        # Asking for paired data is a constraint, not just a topic. Without
        # this, "paired multiome" compiled to the same contract as a generic
        # scATAC question and standalone studies were recalled alongside it.
        minimum_pairing_level = (
            PairingLevel.SAME_CELL
            if _contains(question, SAME_CELL_PAIRING_ALIASES)
            else None
        )
        if minimum_pairing_level is not None:
            modalities = restrict_modalities_to_pairing(modalities, minimum_pairing_level)
        elif not modalities:
            modalities = [Modality.SCATAC, Modality.MULTIOME_RNA_ATAC]

        minimum_donors = None
        donor_match = re.search(
            r"(?:at least|minimum|至少(?:需要)?|不少于)\s*(\d+)\s*(?:个|名|位)?\s*(?:donors?|供体|患者)",
            question,
            flags=re.IGNORECASE,
        )
        if donor_match:
            minimum_donors = int(donor_match.group(1))

        required_roles, preferred_roles = roles_for_modalities(modalities)

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
            required_data_roles=required_roles,
            preferred_data_roles=preferred_roles,
            minimum_donors=minimum_donors,
            minimum_pairing_level=minimum_pairing_level,
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

# Named co-assay technologies: the only reliable way to search for layers
# measured in one cell, since "multiome" alone also matches studies that merely
# report both assays. Shared by QueryPlanner and CorpusQueryPlanner.
RNA_ATAC_COASSAY_QUERY_TERMS = (
    '"10x Multiome" OR "RNA ATAC multiome" OR SNARE-seq OR SHARE-seq '
    'OR sci-CAR OR Paired-seq'
)
ATAC_OTHER_MULTIOMICS_QUERY_TERMS = (
    'ASAP-seq OR ISSAAC-seq OR TEA-seq OR DOGMA-seq '
    'OR ((scATAC-seq OR snATAC-seq) AND (protein OR epitope OR trimodal))'
)
SAME_CELL_QUERY_TERMS = (
    f"{RNA_ATAC_COASSAY_QUERY_TERMS} OR TEA-seq OR DOGMA-seq"
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

        # Searching for same-cell data with the generic modality blob recalls
        # standalone scATAC and study-level co-reporting alongside it, so a
        # question that asks for pairing gets the named co-assay technologies.
        modality_terms = (
            SAME_CELL_QUERY_TERMS
            if pairing_rank(requirement.minimum_pairing_level or PairingLevel.NOT_APPLICABLE) >= 3
            else MODALITY_QUERY_TERMS
        )
        # LLM-identified technology names that no Modality alias covers yet.
        # OR'd alongside the deterministic terms -- an alternate way to match
        # the modality, not an additional constraint -- so a niche assay can
        # widen recall without anyone updating MODALITY_ALIASES first. Bounded
        # to keep the query from growing without limit; already sanitised by
        # `_safe_terms` at the point each term was harvested.
        supplementary_query = " OR ".join(
            f'"{term}"' for term in requirement.supplementary_search_terms[:8]
        )
        modality_clause = (
            f"({modality_terms} OR {supplementary_query})"
            if supplementary_query
            else f"({modality_terms})"
        )

        common_parts = [modality_clause]
        if biological_query:
            common_parts.append(f"({biological_query})")
        if organism_query:
            common_parts.append(f"({organism_query})")
        common_query = " AND ".join(common_parts)

        geo_parts = [modality_clause]
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
        f"({RNA_ATAC_COASSAY_QUERY_TERMS})",
    ),
    (
        "matched_scrna_scatac",
        '((scATAC-seq OR snATAC-seq) AND (scRNA-seq OR "single-cell RNA-seq") AND (paired OR matched OR "same sample" OR "same donor"))',
    ),
    (
        "atac_other_multiomics",
        f"({ATAC_OTHER_MULTIOMICS_QUERY_TERMS})",
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
