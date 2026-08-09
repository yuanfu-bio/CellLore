"""infer_genome_build: deterministic assembly text inference.

GEO per-sample supplementary listings almost never state a genome build, so
this is read from series/sample prose when it is stated at all -- never
required, since paired_atac_rna_human's resolved_assembly evidence is
ungated at acquisition precisely because this often finds nothing (see
tests/test_curation_decision.py).
"""

from scdata_agent.infrastructure.sources.common import infer_genome_build


def test_named_build_is_recognized():
    assert infer_genome_build(["Reads were aligned to GRCh38."]) == "GRCh38"


def test_common_alias_is_recognized():
    assert infer_genome_build(["Reads were aligned to the hg38 reference."]) == "GRCh38"


def test_mouse_build_is_recognized():
    assert infer_genome_build(["Aligned against mm10."]) == "GRCm38"


def test_no_build_mentioned_returns_none():
    assert infer_genome_build(["A study of endothelial cell heterogeneity."]) is None


def test_none_and_empty_texts_are_skipped():
    assert infer_genome_build([None, "", None]) is None


def test_two_disagreeing_builds_is_left_unresolved():
    """A real ambiguity (e.g. citing a prior study's build alongside this
    study's own) must not be silently resolved by picking one."""
    assert infer_genome_build(["Compared against GRCh37; realigned to GRCh38."]) is None


def test_build_name_inside_a_url_is_not_matched():
    """Mirrors the overclaim fix for assay/pairing inference: a URL's path
    segments are not prose, even when they happen to contain a build name."""
    assert infer_genome_build([
        "Supplementary files are listed at "
        "https://ftp.ncbi.nlm.nih.gov/geo/GRCh38_reference_bundle/readme.txt "
        "for this deposition."
    ]) is None


def test_build_name_inside_a_document_filename_is_not_matched():
    assert infer_genome_build(["See protocol_GRCh38_pipeline.pdf for details."]) is None
