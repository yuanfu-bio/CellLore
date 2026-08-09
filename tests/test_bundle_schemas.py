"""Per-modality bundle schemas.

Regression cover for the defect that made every bundle in a live run report
`bundle:missing_roles`: a question mentioning both scATAC and multiome produced
one unioned role list, which was then applied to standalone scATAC depositions
that legitimately ship no RNA matrix and no barcode linkage.
"""

from scdata_agent.domain.models import BundleSchema, DataRole
from scdata_agent.domain.requirements import BUNDLE_SCHEMAS


def _schema(schema_id: str) -> BundleSchema:
    return next(item for item in BUNDLE_SCHEMAS if item.schema_id == schema_id)


def test_standalone_scatac_is_complete_without_rna_or_barcode_linkage():
    """The core regression: scATAC must not be judged against multiome."""
    observed = {DataRole.FRAGMENTS, DataRole.CELL_METADATA}
    assert _schema("scatac_standalone").is_satisfied_by(observed)
    # The same bundle correctly fails the multiome schema.
    assert not _schema("multiome_same_cell").is_satisfied_by(observed)


def test_multiome_still_requires_rna_and_a_cell_linkage():
    schema = _schema("multiome_same_cell")
    assert not schema.is_satisfied_by({DataRole.FRAGMENTS, DataRole.CELL_METADATA})
    assert schema.is_satisfied_by(
        {DataRole.FRAGMENTS, DataRole.RNA_MATRIX, DataRole.CELL_METADATA}
    )


def test_accessibility_may_be_fragments_or_a_peak_matrix():
    """Role groups are disjunctions: either form of the signal is usable."""
    schema = _schema("scatac_standalone")
    assert schema.is_satisfied_by({DataRole.FRAGMENTS, DataRole.CELL_METADATA})
    assert schema.is_satisfied_by({DataRole.PEAK_MATRIX, DataRole.CELL_METADATA})
    # Neither form present: the group is genuinely unmet.
    assert not schema.is_satisfied_by({DataRole.CELL_METADATA})


def test_a_joint_object_satisfies_matrix_and_cell_axis_at_once():
    """An AnnData/MuData carries the matrix and the cell axis together."""
    assert _schema("multiome_same_cell").is_satisfied_by({DataRole.STANDARD_OBJECT})


def test_multiome_same_cell_accepts_a_native_10x_package_with_no_separate_barcode_file():
    """A raw/filtered_feature_bc_matrix.h5 carries its own barcode axis, and a
    10x Multiome kit's GEX<->ATAC barcode correspondence is a fixed,
    kit-published translation whitelist, not a per-sample fact the bundle
    needs to supply evidence for. Requiring a standalone barcode-linkage file
    here blocked every ordinary 10x Multiome deposition (fragments.tsv.gz +
    filtered_feature_bc_matrix.h5, no extra sidecar) from ever satisfying this
    schema -- confirmed against a real live-discovered GEO series (liver
    snMultiome) whose eight wells were verified same_cell and had every
    required file, and still could not clear this schema before this fix."""
    schema = _schema("multiome_same_cell")
    assert schema.is_satisfied_by(
        {DataRole.FRAGMENTS, DataRole.FRAGMENT_INDEX, DataRole.RNA_MATRIX}
    )


def test_unmet_groups_name_what_is_actually_missing():
    unmet = _schema("multiome_same_cell").unmet_groups(
        {DataRole.FRAGMENTS, DataRole.CELL_METADATA}
    )
    assert len(unmet) == 1
    assert DataRole.RNA_MATRIX in unmet[0]
