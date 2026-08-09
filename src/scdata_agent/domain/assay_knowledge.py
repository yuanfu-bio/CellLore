"""Shared, provider-neutral knowledge about named assays and omics layers.

This is the single table both requirement compilation (what a research
question is asking for) and candidate classification (what a discovered
record actually contains) read from. Before this module existed, the two
sides kept separate, drifting copies -- a modality alias table for compilation
and the sources layer's own copy for classification -- so a term recognised on
one side could be invisible on the other. Named technologies are a fast path
over a smaller, closed vocabulary of omics layers, not the vocabulary itself:
an assay this table has never heard of still resolves through
``LAYER_ALIASES`` (or, upstream of this module, through LLM+evidence
inference), it just resolves with lower confidence.

Lives in ``domain`` rather than the sources layer because the sources layer
depends on ``domain``, never the reverse -- see the architecture layering
test that asserts this.
"""

from __future__ import annotations

from scdata_agent.domain.models import OmicsLayer, PairingLevel

#: Bare terminology for one omics layer, independent of any named technology.
#: The fallback path when a record mentions a layer without naming an assay.
LAYER_ALIASES: dict[OmicsLayer, tuple[str, ...]] = {
    OmicsLayer.CHROMATIN_ACCESSIBILITY: (
        "scatac", "snatac", "single cell atac", "single-cell atac",
        "single nucleus atac", "single-nucleus atac",
        "single cell chromatin accessibility", "chromatin accessibility",
        "单细胞atac", "单细胞染色质",
    ),
    # Single-nucleus spellings are listed explicitly, mirroring the ATAC
    # aliases. They used to be reached only by `rna-seq` matching inside
    # `snRNA-seq`, which a bounded matcher correctly refuses -- the alias
    # table has to name the assay rather than rely on a substring collision.
    OmicsLayer.TRANSCRIPTOME: (
        "scrna", "snrna", "single cell rna", "single-cell rna",
        "single nucleus rna", "single-nucleus rna",
        "transcriptome", "transcriptomic", "gene expression",
        "rna-seq", "rna sequencing", "单细胞rna", "单细胞转录组",
    ),
    OmicsLayer.PROTEIN: (
        "surface protein", "protein abundance", "antibody-derived tag",
        "antibody derived tag", "adt", "epitope", "protein",
        "cite-seq", "cite seq", "totalseq",
    ),
}

#: Named co-assay technologies: the fast path. Each row states its layers and
#: pairing relationship *by definition* (confidence 0.95 when matched), which
#: is a materially stronger claim than assembling the same reading from
#: separate layer mentions -- see ``ProfileBasis`` in ``domain.models``.
TECHNOLOGY_PROFILES: tuple[dict, ...] = (
    {"label": "10x_multiome_rna_atac", "aliases": ("10x multiome", "chromium single cell multiome", "multiome atac + gene expression", "multiome atac gene expression"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_NUCLEUS},
    {"label": "snare_seq", "aliases": ("snare-seq", "snare seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_NUCLEUS},
    {"label": "share_seq", "aliases": ("share-seq", "share seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "sci_car", "aliases": ("sci-car", "sci car"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "paired_seq", "aliases": ("paired-seq", "paired seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "asap_seq", "aliases": ("asap-seq", "asap seq", "issaac-seq", "issaac seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
    {"label": "tea_seq", "aliases": ("tea-seq", "tea seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
    {"label": "dogma_seq", "aliases": ("dogma-seq", "dogma seq"), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
    # RNA + surface protein, no chromatin accessibility layer. Absent before
    # this table existed, which is what made a CITE-seq question silently
    # fall back to the ATAC-scoped default (domain.requirements) and made
    # infer_assay_profiles refuse to classify a CITE-seq record at all
    # (its old ATAC gate lived in infrastructure.sources.common).
    {"label": "cite_seq", "aliases": ("cite-seq", "cite seq"), "layers": (OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
)
