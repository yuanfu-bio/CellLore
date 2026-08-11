"""Deterministic file-role classification and bundle completeness checks."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import AssetBundle, DataAsset, DataRole

# Order is significant: the first match wins, so narrower patterns come first.
# A fragment index name contains the fragment name, and a peak matrix name
# contains "peak", so those must precede their broader counterparts.
ROLE_PATTERNS: tuple[tuple[DataRole, re.Pattern[str]], ...] = (
    # Self-describing single-file objects are unambiguous.
    (DataRole.STANDARD_OBJECT, re.compile(r"\.(?:h5ad|h5mu)(?:\.gz)?$", re.I)),
    (DataRole.BARCODE_LINKAGE, re.compile(r"barcode.*(?:link|pair|mapping|translat)", re.I)),
    (DataRole.FRAGMENT_INDEX, re.compile(r"fragments?.*\.(?:tbi|csi)(?:\.gz)?$", re.I)),
    (DataRole.FRAGMENTS, re.compile(r"fragments?.*\.(?:tsv|bed)(?:\.gz)?$", re.I)),
    # Per-cell QC tables emitted by Cell Ranger and Cell Ranger ARC.
    (
        DataRole.QC_METRICS,
        re.compile(r"per_barcode_metrics|singlecell\.csv|metrics_summary|qc[_-]?metrics", re.I),
    ),
    # Basename-anchored sidecars must precede the matrix patterns below. The
    # matrix patterns read modality from the enclosing directory, so without
    # this ordering ``filtered_peak_bc_matrix/barcodes.tsv.gz`` would be
    # classified as a peak matrix by its own parent directory.
    (
        DataRole.CELL_METADATA,
        re.compile(
            r"barcodes\.tsv(?:\.gz)?$|(?:cell|barcode).*(?:meta|annot)|"
            r"(?:^|[/_-])metadata\.(?:csv|tsv|txt)(?:\.gz)?$",
            re.I,
        ),
    ),
    (DataRole.PEAKS, re.compile(r"narrowpeak|broadpeak|peaks?\.(?:bed|tsv|csv|txt)(?:\.gz)?$", re.I)),
    # Modality-specific matrices. 10x encodes the modality in the enclosing
    # directory (``filtered_peak_bc_matrix/matrix.mtx.gz``), which is why the
    # whole path is searched rather than the basename alone. "chromatin" and
    # "cDNA" are the SNARE-seq/SHARE-seq spellings of the same two modalities.
    (
        DataRole.PEAK_MATRIX,
        re.compile(r"(?:peak|atac|tile|chromatin).*(?:matrix|counts?|bc_matrix)", re.I),
    ),
    (
        DataRole.RNA_MATRIX,
        re.compile(r"(?:rna|gene|gex|feature|cdna).*(?:matrix|counts?|bc_matrix)", re.I),
    ),
    (DataRole.PROTEIN_MATRIX, re.compile(r"(?:protein|adt|antibody|epitope).*(?:matrix|counts?)", re.I)),
    (DataRole.SAMPLE_METADATA, re.compile(r"sample.*(?:meta|sheet)", re.I)),
    (DataRole.ALIGNED_READS, re.compile(r"\.(?:bam|cram)(?:\..*)?$", re.I)),
    (DataRole.RAW_READS, re.compile(r"\.(?:fastq|fq)(?:\.gz)?$", re.I)),
    # Archives last: anything more specific should have matched by now. Their
    # payload is unknowable without downloading, so they get their own role
    # rather than being folded into OTHER.
    (DataRole.ARCHIVE, re.compile(r"\.(?:tar(?:\.(?:gz|bz2|xz))?|tgz|zip)$", re.I)),
)


def infer_data_role(file_name: str) -> DataRole | None:
    """Infer a conservative semantic role from a repository file path.

    The full path is searched, not just the basename: 10x layouts put the
    modality in the directory while the file itself is generically named, so
    ``filtered_peak_bc_matrix/matrix.mtx.gz`` is only classifiable in context.
    """
    normalized = PurePosixPath(file_name).as_posix()
    for role, pattern in ROLE_PATTERNS:
        if pattern.search(normalized):
            return role
    return None


def build_asset_bundle(
    candidate_id: str,
    assets: list[DataAsset],
    *,
    bundle_type: str,
    required_roles: list[DataRole],
    optional_roles: list[DataRole] | None = None,
) -> AssetBundle:
    """Build an auditable bundle; completeness depends on roles, not filenames."""
    observed = {asset.role for asset in assets}
    missing = [role for role in required_roles if role not in observed]
    identity = ":".join(
        [
            candidate_id,
            bundle_type,
            ",".join(sorted(asset.asset_id for asset in assets)),
        ]
    )
    return AssetBundle(
        bundle_id=stable_id("bundle", identity),
        candidate_id=candidate_id,
        bundle_type=bundle_type,
        required_roles=required_roles,
        optional_roles=optional_roles or [],
        asset_ids=[asset.asset_id for asset in assets],
        missing_roles=missing,
        complete=not missing,
    )
