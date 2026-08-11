"""Research-question compilation and source-specific query planning."""

from __future__ import annotations

import re

from celllore.domain.assay_knowledge import LAYER_ALIASES
from celllore.domain.curation.profile import CurationProfile
from celllore.domain.models import (
    BundleSchema,
    DataRole,
    OmicsLayer,
    PairingLevel,
    PairingRequirement,
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

_PHYSICAL_PAIRING = PairingRequirement(
    acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
)
# Provenance match only: same sample/donor, not the same physical measurement
# unit. Deliberately excludes SAME_CELL/SAME_NUCLEUS so a genuinely same-cell
# candidate resolves to multiome_same_cell instead of colliding with this one.
_PROVENANCE_PAIRING = PairingRequirement(
    acceptable_levels=[PairingLevel.SAME_SAMPLE, PairingLevel.SAME_DONOR]
)

BUNDLE_SCHEMAS: tuple[BundleSchema, ...] = (
    BundleSchema(
        schema_id="scatac_standalone",
        label="Standalone single-cell ATAC",
        omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
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
        omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        required_relationship=_PHYSICAL_PAIRING,
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT],
            # Same as _CELL_AXIS's own reasoning: the RNA/peak matrix already
            # carries its own barcode axis, and a standard 10x Multiome kit
            # links GEX and ATAC barcodes through a fixed, kit-published
            # translation whitelist -- not a per-sample fact this bundle would
            # need to supply evidence for. An explicit barcode map or a joint
            # object is still accepted when present, just no longer required.
            _CELL_AXIS,
        ],
        preferred_roles=[
            DataRole.FRAGMENT_INDEX, DataRole.PEAKS,
            DataRole.QC_METRICS, DataRole.SAMPLE_METADATA,
        ],
    ),
    BundleSchema(
        schema_id="multiome_trimodal",
        label="RNA + ATAC + surface protein in the same cell (TEA-seq, DOGMA-seq)",
        omics_layers=[
            OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN,
        ],
        required_relationship=_PHYSICAL_PAIRING,
        required_role_groups=[
            _ACCESSIBILITY,
            [DataRole.RNA_MATRIX, DataRole.STANDARD_OBJECT],
            # The third layer is the point of a trimodal assay: without it the
            # bundle is a two-layer multiome mislabelled as trimodal.
            [DataRole.PROTEIN_MATRIX, DataRole.STANDARD_OBJECT],
            # See multiome_same_cell's identical group above for why RNA/peak
            # matrices count here too.
            _CELL_AXIS,
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
        omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
        required_relationship=_PROVENANCE_PAIRING,
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
        omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN],
        required_relationship=_PHYSICAL_PAIRING,
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
        # Applies to any layer: FASTQ is a legitimate, if expensive, input.
        omics_layers=[],
        required_role_groups=[[DataRole.RAW_READS]],
        preferred_roles=[DataRole.SAMPLE_METADATA],
    ),
)


def applicable_bundle_schemas_by_layers(
    omics_layers: list[OmicsLayer] | set[OmicsLayer],
    observed_relationship: PairingLevel | None,
) -> list[BundleSchema]:
    """Layer/relationship-driven successor to ``applicable_bundle_schemas``.

    Two different match rules for two different kinds of schema, not one rule
    for both:

    * A **relationship-gated** schema (``multiome_same_cell``,
      ``matched_scrna_scatac``, ...) matches by *subset*: its declared layers
      must all be present, but the candidate may carry more. A DOGMA-seq
      candidate {ATAC, RNA, PROTEIN} still legitimately proves the RNA+ATAC
      same-cell relationship ``multiome_same_cell`` asks for -- rejecting it
      for having one extra layer would reintroduce the exact closed-enumeration
      problem this layer system exists to fix, just one level down (now over
      layer *combinations* instead of over a coarse assay label). The
      relationship gate is what disambiguates ``multiome_same_cell`` from
      ``matched_scrna_scatac`` despite their identical layer sets -- subset
      matching does not reopen that collision.
    * A schema with **no relationship gate** (``scatac_standalone``,
      ``atac_protein`` has one, so it doesn't count here -- only truly
      ungated ones) matches by *exact* set equality. Without this, a
      candidate declared as multiome (ATAC+RNA) but still missing its RNA
      file would silently satisfy ``scatac_standalone`` on the strength of its
      ATAC layer alone -- exactly the "graded on an easier curve" bug
      ``bundle_shape_for`` was already written to avoid for the reverse case
      (see its own docstring). ``raw_reads`` (empty ``omics_layers``) is exempt
      from both rules and always applies.

    An unlabelled candidate (no layer observed at all) is offered every
    schema.
    """
    observed = set(omics_layers)
    if not observed:
        return list(BUNDLE_SCHEMAS)
    matches: list[BundleSchema] = []
    for schema in BUNDLE_SCHEMAS:
        if not schema.omics_layers:
            matches.append(schema)
            continue
        required = set(schema.omics_layers)
        layers_match = (
            required.issubset(observed)
            if schema.required_relationship is not None
            else required == observed
        )
        if not layers_match:
            continue
        if (
            schema.required_relationship is not None
            and not schema.required_relationship.is_satisfied_by(observed_relationship)
        ):
            continue
        matches.append(schema)
    return matches


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

# Phrasings that name a specific combination of omics layers -- either
# through bare terminology (see LAYER_ALIASES for a single layer) or through
# a combinatorial phrase that doesn't name any one named technology. Keyed by
# the exact layer combination implied, not by a coarse assay label: unlike a
# Modality value, this can never conflate "which layers" with "what
# relationship" -- see BUNDLE_SCHEMAS/PairingRequirement for the relationship
# axis, which this table has nothing to do with.
_LAYER_COMBINATION_ALIASES: dict[frozenset[OmicsLayer], tuple[str, ...]] = {
    frozenset({OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME}): (
        "multiome", "rna+atac", "rna atac", "joint rna and atac", "多组学",
        "matched scrna scatac", "paired scrna scatac", "sample-matched rna atac",
        "配对scrna和scatac", "配对rna和atac",
        "atac other multiomics", "atac multiomics", "atac 多组学",
    ),
    frozenset({OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN}): (
        "asap-seq", "issaac-seq", "atac protein", "atac+protein",
    ),
    frozenset({
        OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN,
    }): (
        "tea-seq", "dogma-seq", "rna atac protein", "trimodal", "tri-modal",
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

# Independent of any named-technology or layer-combination match: an assay
# this table has never heard of can still surface the omics layers it asked
# for via the shared, open-vocabulary layer table -- see `_known_layers`.
SINGLE_CELL_ALIASES: tuple[str, ...] = (
    "single cell", "single-cell", "single nucleus", "single-nucleus",
    "单细胞", "单核",
)
BULK_ONLY_ALIASES: tuple[str, ...] = (
    "bulk rna-seq", "bulk sequencing", "bulk omics", "bulk transcriptome",
    "bulk 转录组", "bulk测序", "bulk 测序",
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


def _known_layers(text: str) -> list[OmicsLayer]:
    """Every OmicsLayer whose bare terminology, or a combinatorial phrase that
    implies it, appears in this free-text string.

    A question can name a single layer (e.g. "surface protein", via
    ``LAYER_ALIASES``) or a phrase that implies a specific combination of
    layers without naming any one technology (e.g. bare "multiome", via
    ``_LAYER_COMBINATION_ALIASES``) -- both routes are open-vocabulary and
    apply even when no named technology in ``TECHNOLOGY_PROFILES`` covers the
    assay that produced it.
    """
    layers = {layer for layer, aliases in LAYER_ALIASES.items() if _contains(text, aliases)}
    for combination, aliases in _LAYER_COMBINATION_ALIASES.items():
        if _contains(text, aliases):
            layers.update(combination)
    return sorted(layers, key=str)


def roles_for_layers(
    layers: list[OmicsLayer] | set[OmicsLayer],
) -> tuple[list[DataRole], list[DataRole]]:
    """Canonical required/preferred DataRoles implied by an omics-layer set.

    This is the single source of truth for what a coarse ResearchRequirement
    should advise a candidate needs; it previously existed as three
    independently drifting copies (this function, the compiler in
    ``infrastructure/llm/requirement_compiler.py``, and one in
    ``infrastructure/llm/research_planner.py``). The per-candidate acquisition
    gate in ``inventory_reconcile``/``BUNDLE_SCHEMAS`` is the actual admission
    decision; this only seeds the requirement's advisory fields and the
    compilers' role recommendations -- so one narrow imprecision is accepted
    here rather than threading a relationship parameter through just to avoid
    it: BARCODE_LINKAGE gets advised for any ATAC+RNA combination, including a
    matched-samples-only (not same-cell) requirement, which never actually has
    a shared barcode space to link. That distinction matters at admission time
    (``BUNDLE_SCHEMAS``' ``required_relationship``), not for this advisory seed.
    """
    observed = set(layers)
    required_roles = {DataRole.CELL_METADATA, DataRole.SAMPLE_METADATA}
    preferred_roles = {DataRole.RAW_READS, DataRole.PEAK_MATRIX, DataRole.QC_METRICS}
    if OmicsLayer.CHROMATIN_ACCESSIBILITY in observed:
        required_roles.add(DataRole.FRAGMENTS)
        preferred_roles.add(DataRole.FRAGMENT_INDEX)
    if {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME} <= observed:
        required_roles.add(DataRole.RNA_MATRIX)
        required_roles.add(DataRole.BARCODE_LINKAGE)
    if {OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN} <= observed:
        required_roles.add(DataRole.PROTEIN_MATRIX)
    return sorted(required_roles, key=str), sorted(preferred_roles, key=str)


class RuleBasedRequirementCompiler:
    """Conservative baseline compiler that never invents domain entities."""

    def compile(
        self, question: str, profile: CurationProfile | None = None
    ) -> ResearchRequirement:
        same_cell_pairing_aliases = (
            profile.search_hints.pairing_phrase_seeds
            if profile
            else SAME_CELL_PAIRING_ALIASES
        )
        organisms = _extract_aliases(question, ORGANISM_ALIASES)
        tissues = _extract_aliases(question, TISSUE_ALIASES)
        diseases = _extract_aliases(question, DISEASE_ALIASES)
        cell_types = _extract_aliases(question, CELL_TYPE_ALIASES)
        downstream_tasks = _extract_aliases(question, TASK_ALIASES)

        # Asking for paired data is a constraint, not just a topic. Without
        # this, "paired multiome" compiled to the same contract as a generic
        # scATAC question and standalone studies were recalled alongside it.
        minimum_pairing_level = (
            PairingLevel.SAME_CELL
            if _contains(question, same_cell_pairing_aliases)
            else None
        )
        # No silent default here. An earlier version of this compiler filled
        # an empty match with an ATAC-scoped guess that made an unrelated
        # question (e.g. a CITE-seq one) recall ATAC studies instead of
        # returning nothing. `_known_layers` is the open-vocabulary
        # replacement: a question naming no combination this table
        # recognises still surfaces the bare layers it asked for, without
        # inventing an assay nobody mentioned.
        required_omics_layers = _known_layers(question)

        # Same-cell/nucleus is a category, not a rank: the requirement is an
        # explicit allow-list rather than "at least this level" (see
        # PairingRequirement's docstring in domain.models).
        pairing_requirement = (
            PairingRequirement(
                acceptable_levels=[PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS]
            )
            if minimum_pairing_level is not None
            else None
        )

        single_cell_requirement = None
        if _contains(question, SINGLE_CELL_ALIASES):
            single_cell_requirement = True
        elif _contains(question, BULK_ONLY_ALIASES):
            single_cell_requirement = False

        minimum_donors = None
        donor_match = re.search(
            r"(?:at least|minimum|至少(?:需要)?|不少于)\s*(\d+)\s*(?:个|名|位)?\s*(?:donors?|供体|患者)",
            question,
            flags=re.IGNORECASE,
        )
        if donor_match:
            minimum_donors = int(donor_match.group(1))

        required_roles, preferred_roles = roles_for_layers(required_omics_layers)

        search_terms = _unique(
            [
                *organisms,
                *tissues,
                *diseases,
                *cell_types,
                *[item.value for item in required_omics_layers],
            ]
        )

        lowered = question.casefold()
        excluded_sources = ["cell_line"] if any(term in lowered for term in ("cell line", "cell-line", "细胞系")) and any(term in lowered for term in ("exclude", "excluding", "排除", "不接受")) else []
        exclusions = ["controlled_access"] if any(term in lowered for term in ("controlled access", "受控访问")) and any(term in lowered for term in ("exclude", "excluding", "排除", "不接受")) else []
        evaluation_policy = {"reserve_independent_studies": any(term in lowered for term in ("independent study", "independent studies", "独立研究")), "holdout_unit": "study" if any(term in lowered for term in ("independent study", "independent studies", "独立研究")) else None, "prevent_lineage_leakage": any(term in lowered for term in ("independent study", "独立研究", "泛化测试"))}
        return ResearchRequirement(
            question=question,
            profile_id=profile.profile_id if profile else None,
            organisms=organisms,
            tissues=tissues,
            diseases=diseases,
            cell_types=cell_types,
            required_data_roles=required_roles,
            preferred_data_roles=preferred_roles,
            minimum_donors=minimum_donors,
            minimum_pairing_level=minimum_pairing_level,
            required_omics_layers=required_omics_layers,
            pairing_requirement=pairing_requirement,
            single_cell_requirement=single_cell_requirement,
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
    # Bare "multiome"/"multiomic" are excluded from the shared
    # RNA_ATAC_COASSAY_QUERY_TERMS constant above (it also seeds
    # CorpusQueryPlanner's family bucketing, where that ambiguity matters
    # for labelling). Adding them only here is safe: a real GEO record
    # regularly describes a true 10x Multiome study in its own words
    # ("10x Genomics snRNA-seq and snATAC-seq", "multiomic profiling of...")
    # without ever using any of the named co-assay technology strings above
    # (see GSE228428 -- confirmed to have zero matches against the named-only
    # list, real ones against this widened one). Recall lost here can never
    # be recovered later; the false positives this widening risks (a study
    # that merely co-reports separate ATAC and RNA) are exactly what
    # PairingVerifier's evidence-graded relationship claim exists to reject
    # downstream, so casting a wider net here no longer trades away
    # precision the way it would have before that existed.
    f"{RNA_ATAC_COASSAY_QUERY_TERMS} OR TEA-seq OR DOGMA-seq OR multiome OR multiomic"
)


class QueryPlanner:
    def plan(
        self,
        requirement: ResearchRequirement,
        max_results: int = 20,
        modality_terms_override: str | None = None,
    ) -> QueryPlan:
        """Build per-source boolean queries.

        ``modality_terms_override`` lets a caller (see ``LLMQueryPlanner``)
        supply the technology/assay clause itself -- authored against a
        profile's open ``search_hints`` vocabulary rather than the fixed
        ``MODALITY_QUERY_TERMS``/``SAME_CELL_QUERY_TERMS`` tables below --
        while still going through this method for the one part that must
        stay deterministic and guaranteed: the organism/biological filter,
        derived from typed ``ResearchRequirement`` fields, not free text an
        LLM call could omit or hallucinate.
        """
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
        modality_terms = modality_terms_override or (
            SAME_CELL_QUERY_TERMS
            if pairing_rank(requirement.minimum_pairing_level or PairingLevel.NOT_APPLICABLE) >= 3
            else MODALITY_QUERY_TERMS
        )
        # LLM-identified technology names that no layer/combination alias
        # covers yet. OR'd alongside the deterministic terms -- an alternate
        # way to match the assay, not an additional constraint -- so a niche
        # assay can widen recall without anyone updating the alias tables
        # first. Bounded to keep the query from growing without limit;
        # already sanitised by `_safe_terms` at the point each term was
        # harvested.
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
