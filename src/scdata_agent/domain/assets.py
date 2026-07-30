"""Deterministic file-role classification and bundle completeness checks."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from scdata_agent.domain.identifiers import stable_id
from scdata_agent.domain.models import AssetBundle, DataAsset, DataRole


ROLE_PATTERNS: tuple[tuple[DataRole, re.Pattern[str]], ...] = (
    (DataRole.STANDARD_OBJECT, re.compile(r"\.(?:h5ad|h5mu)$", re.I)),
    (DataRole.BARCODE_LINKAGE, re.compile(r"barcode.*(?:link|pair|mapping)", re.I)),
    (DataRole.FRAGMENT_INDEX, re.compile(r"fragments?.*(?:tbi|csi)$", re.I)),
    (DataRole.FRAGMENTS, re.compile(r"fragments?.*(?:tsv|bed)(?:\.gz)?$", re.I)),
    (DataRole.PEAK_MATRIX, re.compile(r"(?:peak|atac).*(?:matrix|counts?)", re.I)),
    (DataRole.RNA_MATRIX, re.compile(r"(?:rna|gene).*(?:matrix|counts?)", re.I)),
    (DataRole.PROTEIN_MATRIX, re.compile(r"(?:protein|adt|antibody|epitope).*(?:matrix|counts?)", re.I)),
    (DataRole.CELL_METADATA, re.compile(r"(?:cell|barcode).*(?:meta|annot)", re.I)),
    (DataRole.SAMPLE_METADATA, re.compile(r"sample.*(?:meta|sheet)", re.I)),
    (DataRole.PEAKS, re.compile(r"(?:peak|narrowpeak|broadpeak)", re.I)),
    (DataRole.ALIGNED_READS, re.compile(r"\.(?:bam|cram)(?:\..*)?$", re.I)),
    (DataRole.RAW_READS, re.compile(r"\.(?:fastq|fq)(?:\.gz)?$", re.I)),
)


def infer_data_role(file_name: str) -> DataRole | None:
    """Infer a conservative semantic role from a repository file name."""
    normalized = PurePosixPath(file_name).name
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
