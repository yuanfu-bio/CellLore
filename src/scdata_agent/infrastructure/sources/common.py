"""Shared source parsing helpers for scATAC and paired multi-omics assays."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from scdata_agent.domain.models import (
    AssayProfile,
    Modality,
    OmicsLayer,
    PairingLevel,
)

ATAC_ALIASES = (
    "scatac", "snatac", "single cell atac", "single-cell atac",
    "single nucleus atac", "single-nucleus atac",
    "single cell chromatin accessibility", "chromatin accessibility",
)
# Single-nucleus spellings are listed explicitly, mirroring ATAC_ALIASES. They
# used to be reached only by `rna-seq` matching inside `snRNA-seq`, which a
# bounded matcher correctly refuses -- the alias table has to name the assay
# rather than rely on a substring collision.
RNA_ALIASES = (
    "scrna", "snrna", "single cell rna", "single-cell rna",
    "single nucleus rna", "single-nucleus rna",
    "transcriptome", "transcriptomic", "gene expression",
    "rna-seq", "rna sequencing",
)
PROTEIN_ALIASES = (
    "surface protein", "protein abundance", "antibody-derived tag",
    "antibody derived tag", "adt", "epitope", "protein",
)


TECHNOLOGY_PROFILES: tuple[dict, ...] = (
    {"label": "10x_multiome_rna_atac", "aliases": ("10x multiome", "chromium single cell multiome", "multiome atac + gene expression", "multiome atac gene expression"), "modalities": (Modality.MULTIOME_RNA_ATAC,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_NUCLEUS},
    {"label": "snare_seq", "aliases": ("snare-seq", "snare seq"), "modalities": (Modality.MULTIOME_RNA_ATAC,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_NUCLEUS},
    {"label": "share_seq", "aliases": ("share-seq", "share seq"), "modalities": (Modality.MULTIOME_RNA_ATAC,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "sci_car", "aliases": ("sci-car", "sci car"), "modalities": (Modality.MULTIOME_RNA_ATAC,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "paired_seq", "aliases": ("paired-seq", "paired seq"), "modalities": (Modality.MULTIOME_RNA_ATAC,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME), "pairing": PairingLevel.SAME_CELL},
    {"label": "asap_seq", "aliases": ("asap-seq", "asap seq", "issaac-seq", "issaac seq"), "modalities": (Modality.MULTIOME_ATAC_PROTEIN,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
    {"label": "tea_seq", "aliases": ("tea-seq", "tea seq"), "modalities": (Modality.MULTIOME_RNA_ATAC_PROTEIN,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
    {"label": "dogma_seq", "aliases": ("dogma-seq", "dogma seq"), "modalities": (Modality.MULTIOME_RNA_ATAC_PROTEIN,), "layers": (OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN), "pairing": PairingLevel.SAME_CELL},
)


# An archive's "description" is not always prose. NCBI's SRA ESummary returns
# the raw Experiment XML, whose markup carries submitter boilerplate and
# protocol URLs that describe the *deposition process*, not the assay. Matching
# assay aliases against that made a transcriptome layer and a same-nucleus
# pairing claim appear out of `contact_name="Gene Expression Omnibus (GEO),
# NCBI, NLM, NIH"` and a protocol file named `Chromium_Multiome_ATAC_GEX.pdf`.
# Only what a human would read as prose may support a claim about the assay.
_MARKUP_RE = re.compile(r"<[^<>]*>")
_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.IGNORECASE)
_PATH_RE = re.compile(r"\S+\.(?:pdf|html?|xlsx?|docx?|csv|txt|zip|gz|tar)\b", re.IGNORECASE)


def prose_text(texts: Iterable[str | None]) -> str:
    """Join source texts, dropping the parts no human would read as prose.

    Markup is removed tag-and-attributes together, because the attribute values
    are precisely where the submitter boilerplate lives. URLs and document
    filenames go too: a protocol PDF names the method used to *prepare* a
    library, and citing one is not a claim about what this record contains.
    """
    joined = " ".join(text for text in texts if text)
    without_markup = _MARKUP_RE.sub(" ", joined)
    without_urls = _URL_RE.sub(" ", without_markup)
    return _PATH_RE.sub(" ", without_urls).casefold()


def infer_assay_profiles(texts: Iterable[str | None]) -> list[AssayProfile]:
    content = prose_text(texts)
    if not content.strip():
        return []
    profiles: list[AssayProfile] = []
    for spec in TECHNOLOGY_PROFILES:
        matched = [alias for alias in spec["aliases"] if _matches(alias, content)]
        if matched:
            profiles.append(AssayProfile(
                label=spec["label"],
                modalities=list(spec["modalities"]),
                omics_layers=list(spec["layers"]),
                pairing_level=spec["pairing"],
                technology_terms=matched,
                confidence=0.95,
                rationale="Named co-assay technology was observed in repository or publication text.",
            ))
    if profiles:
        return _unique_profiles(profiles)

    has_atac = _has_any(content, ATAC_ALIASES)
    if not has_atac:
        return []
    has_rna = _has_any(content, RNA_ALIASES)
    has_protein = _has_any(content, PROTEIN_ALIASES)
    pairing = _infer_pairing_level(content)

    if has_rna and has_protein:
        modality = Modality.MULTIOME_RNA_ATAC_PROTEIN
        label = "atac_rna_protein_multiomics"
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN]
    elif has_rna:
        if pairing in {PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS}:
            modality = Modality.MULTIOME_RNA_ATAC
            label = "rna_atac_coassay"
        elif pairing in {PairingLevel.SAME_SAMPLE, PairingLevel.SAME_DONOR, PairingLevel.UNVERIFIED}:
            modality = Modality.MATCHED_SCRNA_SCATAC
            label = "matched_scrna_scatac"
        else:
            modalities = [Modality.SCATAC, Modality.SCRNA]
            return [AssayProfile(
                label="scrna_and_scatac_co_reported",
                modalities=modalities,
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
                pairing_level=PairingLevel.STUDY_LEVEL,
                confidence=0.55,
                rationale="RNA and ATAC are co-reported, but no pairing relationship is stated.",
            )]
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME]
    elif has_protein:
        modality = Modality.MULTIOME_ATAC_PROTEIN
        label = "atac_protein_multiomics"
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN]
    else:
        modality = Modality.SNATAC if _has_any(content, ("snatac", "single nucleus atac", "single-nucleus atac")) else Modality.SCATAC
        return [AssayProfile(
            label="standalone_single_cell_atac",
            modalities=[modality],
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
            pairing_level=PairingLevel.NOT_APPLICABLE,
            confidence=0.9,
            rationale="Single-cell or single-nucleus ATAC terminology was observed without another omics layer.",
        )]

    return [AssayProfile(
        label=label,
        modalities=[modality],
        omics_layers=layers,
        pairing_level=pairing,
        confidence=0.85 if pairing not in {PairingLevel.UNVERIFIED, PairingLevel.STUDY_LEVEL} else 0.65,
        rationale="Assay layers and their stated pairing relationship were inferred from source text.",
    )]


def infer_modalities(texts: Iterable[str | None]) -> list[Modality]:
    return list(dict.fromkeys(
        modality
        for profile in infer_assay_profiles(texts)
        for modality in profile.modalities
    ))


def _infer_pairing_level(content: str) -> PairingLevel:
    if _has_any(content, ("same nucleus", "same nuclei", "single nucleus multiome", "single-nucleus multiome")):
        return PairingLevel.SAME_NUCLEUS
    if _has_any(content, ("same cell", "same-cell", "single cell multiome", "single-cell multiome", "joint profiling", "simultaneous profiling", "co-assay")):
        return PairingLevel.SAME_CELL
    if _has_any(content, ("same sample", "matched sample", "matched samples", "sample-matched")):
        return PairingLevel.SAME_SAMPLE
    if _has_any(content, ("same donor", "matched donor", "donor-matched", "paired donor")):
        return PairingLevel.SAME_DONOR
    if _has_any(content, ("paired scrna", "matched scrna", "paired rna and atac", "matched rna and atac")):
        return PairingLevel.UNVERIFIED
    # Deliberately only the exact word `multiome`, which names the 10x assay.
    # `multiomics` and `multi-omics` describe a study design, not a measurement
    # unit: "single cell multiomics revealed ..." says two layers were profiled
    # and says nothing about whether one cell yielded both. Accepting those here
    # reproduces the overclaim this function was fixed to stop, one word wider.
    if _matches("multiome", content):
        return PairingLevel.SAME_NUCLEUS
    return PairingLevel.STUDY_LEVEL


@lru_cache(maxsize=512)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    # Bounded so a short alias cannot match inside a longer word: `adt` must not
    # fire on `adta`, and `protein` must not fire on `proteins`' neighbours in
    # an accession. Lookarounds rather than `\b` because several aliases begin
    # or end with punctuation (`snare-seq`, `multiome atac + gene expression`).
    return re.compile(rf"(?<!\w){re.escape(alias.casefold())}(?!\w)")


def _matches(alias: str, content: str) -> bool:
    return _alias_pattern(alias).search(content) is not None


def _has_any(content: str, aliases: tuple[str, ...]) -> bool:
    return any(_matches(alias, content) for alias in aliases)


def _unique_profiles(values: list[AssayProfile]) -> list[AssayProfile]:
    return list({profile.model_dump_json(): profile for profile in values}.values())
