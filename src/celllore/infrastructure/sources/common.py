"""Shared source parsing helpers for scATAC and paired multi-omics assays."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

from celllore.domain.assay_knowledge import LAYER_ALIASES, TECHNOLOGY_PROFILES
from celllore.domain.models import (
    AssayProfile,
    OmicsLayer,
    PairingLevel,
    ProfileBasis,
)

ATAC_ALIASES = LAYER_ALIASES[OmicsLayer.CHROMATIN_ACCESSIBILITY]
RNA_ALIASES = LAYER_ALIASES[OmicsLayer.TRANSCRIPTOME]
PROTEIN_ALIASES = LAYER_ALIASES[OmicsLayer.PROTEIN]


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
                omics_layers=list(spec["layers"]),
                pairing_level=spec["pairing"],
                technology_terms=matched,
                confidence=0.95,
                rationale="Named co-assay technology was observed in repository or publication text.",
                inferred_from=ProfileBasis.NAMED_TECHNOLOGY,
            ))
    if profiles:
        return _unique_profiles(profiles)

    has_atac = _has_any(content, ATAC_ALIASES)
    has_rna = _has_any(content, RNA_ALIASES)
    has_protein = _has_any(content, PROTEIN_ALIASES)
    if not (has_atac or has_rna or has_protein):
        return []
    pairing = _infer_pairing_level(content)

    if has_atac and has_rna and has_protein:
        label = "atac_rna_protein_multiomics"
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN]
    elif has_atac and has_rna:
        if pairing in {PairingLevel.SAME_CELL, PairingLevel.SAME_NUCLEUS}:
            label = "rna_atac_coassay"
        elif pairing in {PairingLevel.SAME_SAMPLE, PairingLevel.SAME_DONOR, PairingLevel.UNVERIFIED}:
            label = "matched_scrna_scatac"
        else:
            return [AssayProfile(
                label="scrna_and_scatac_co_reported",
                omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME],
                pairing_level=PairingLevel.STUDY_LEVEL,
                confidence=0.55,
                rationale="RNA and ATAC are co-reported, but no pairing relationship is stated.",
            )]
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.TRANSCRIPTOME]
    elif has_atac and has_protein:
        label = "atac_protein_multiomics"
        layers = [OmicsLayer.CHROMATIN_ACCESSIBILITY, OmicsLayer.PROTEIN]
    elif has_atac:
        return [AssayProfile(
            label="standalone_single_cell_atac",
            omics_layers=[OmicsLayer.CHROMATIN_ACCESSIBILITY],
            pairing_level=PairingLevel.NOT_APPLICABLE,
            confidence=0.9,
            rationale="Single-cell or single-nucleus ATAC terminology was observed without another omics layer.",
        )]
    elif has_rna and has_protein:
        # RNA + surface protein with no chromatin-accessibility layer and no
        # named-technology match above (e.g. an assay text calls "CITE-seq"
        # would already have matched TECHNOLOGY_PROFILES): report the layers
        # actually observed rather than refusing to classify the record just
        # because this tool's current corpus target is ATAC-scoped. Whether a
        # profile like this is in scope for a given research question is a
        # requirement-level decision, not something this classifier should
        # decide by silently returning nothing.
        return [AssayProfile(
            label="rna_protein_coassay",
            omics_layers=[OmicsLayer.TRANSCRIPTOME, OmicsLayer.PROTEIN],
            pairing_level=pairing,
            confidence=0.65,
            rationale="RNA and surface-protein layers were observed without a chromatin-accessibility layer.",
        )]
    else:
        layer = OmicsLayer.TRANSCRIPTOME if has_rna else OmicsLayer.PROTEIN
        return [AssayProfile(
            label="single_omics_layer",
            omics_layers=[layer],
            pairing_level=PairingLevel.NOT_APPLICABLE,
            confidence=0.75,
            rationale=f"Only a {layer.value} layer was observed in source text.",
        )]

    return [AssayProfile(
        label=label,
        omics_layers=layers,
        pairing_level=pairing,
        confidence=0.85 if pairing not in {PairingLevel.UNVERIFIED, PairingLevel.STUDY_LEVEL} else 0.65,
        rationale="Assay layers and their stated pairing relationship were inferred from source text.",
    )]


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


GENOME_BUILD_ALIASES: dict[str, tuple[str, ...]] = {
    "GRCh38": ("grch38", "hg38"),
    "GRCh37": ("grch37", "hg19"),
    "GRCm39": ("grcm39", "mm39"),
    "GRCm38": ("grcm38", "mm10"),
    "T2T-CHM13": ("chm13", "t2t-chm13"),
}


def infer_genome_build(texts: Iterable[str | None]) -> str | None:
    """The genome build this text states, or None if no known build name
    appears -- read the same way pairing/layer claims are, from prose with
    markup/URLs/filenames stripped, never from a bare substring.

    Deliberately returns at most one build: a study's own text naming two
    different ones (e.g. citing a prior study's build alongside its own) is a
    real ambiguity worth leaving unresolved, not something to silently
    resolve by picking one. Whether an assembly claim from this is *required*
    before acquisition is a profile policy question (see
    ``domain.curation.decision``), not this function's -- a study that
    states no build anywhere in its own text is common, and the real
    determination can wait for the downloaded file's own header.
    """
    content = prose_text(texts)
    matches = {
        canonical
        for canonical, aliases in GENOME_BUILD_ALIASES.items()
        if _has_any(content, aliases)
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


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
