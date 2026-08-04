"""Dialect detection, size parsing, and the XLSX seam.

The fixture is 20 rows copied verbatim out of the real curated manifest,
including its BOM and its Chinese headers, so these tests fail if the reader
stops handling what a curator actually produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scdata_agent.domain.manifest import (
    PAIRED_MULTIOME_ZH_V1,
    REQUIRED_FIELDS,
    ManifestFormatError,
    detect_dialect,
    dialect_by_id,
    parse_size,
)
from scdata_agent.infrastructure.manifest_reader import (
    XLSX_EXTRA_HINT,
    read_manifest,
    read_tabular,
)

FIXTURE = Path(__file__).parent / "data" / "manifest_paired_multiome_sample.csv"


def test_detects_the_real_chinese_header_row() -> None:
    headers, rows = read_tabular(FIXTURE)
    # The BOM must not survive into the first header, or "dataset_id" becomes
    # "﻿dataset_id" and every lookup silently misses.
    assert headers[0] == "dataset_id"
    assert "GSM/文件accession" in headers
    assert len(rows) == 20
    assert detect_dialect(headers) is PAIRED_MULTIOME_ZH_V1


def test_dialect_covers_every_required_field() -> None:
    for field in REQUIRED_FIELDS:
        assert field in PAIRED_MULTIOME_ZH_V1.columns


def test_unknown_dialect_id_names_the_known_ones() -> None:
    with pytest.raises(ManifestFormatError, match="paired_multiome_zh_v1"):
        dialect_by_id("no_such_dialect")


def test_missing_required_column_is_refused_not_guessed(tmp_path: Path) -> None:
    trimmed = tmp_path / "trimmed.csv"
    text = FIXTURE.read_text(encoding="utf-8-sig").splitlines()
    header, *body = text
    columns = header.split(",")
    keep = [index for index, name in enumerate(columns) if name != "本地相对路径"]
    trimmed.write_text(
        "\n".join(
            ",".join(line.split(",")[index] for index in keep) for line in [header, *body]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ManifestFormatError, match="dialect"):
        read_manifest(trimmed)


def test_explicit_dialect_still_verifies_its_columns(tmp_path: Path) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    with pytest.raises(ManifestFormatError, match="not present"):
        read_manifest(empty, "paired_multiome_zh_v1")


def test_unsupported_suffix_is_named(tmp_path: Path) -> None:
    other = tmp_path / "manifest.parquet"
    other.write_bytes(b"")
    with pytest.raises(ManifestFormatError, match="Unsupported manifest format"):
        read_tabular(other)


def test_xlsx_without_the_extra_says_how_to_install_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workbook = tmp_path / "manifest.xlsx"
    workbook.write_bytes(b"")
    monkeypatch.setitem(__import__("sys").modules, "openpyxl", None)
    with pytest.raises(ManifestFormatError) as error:
        read_tabular(workbook)
    assert str(error.value) == XLSX_EXTRA_HINT


@pytest.mark.parametrize(
    ("value", "unit", "size_gb", "expected"),
    [
        ("86.3", "MB", "", round(86.3 * 1024**2)),
        ("519", "KB", "", 519 * 1024),
        ("1.8", "GB", "1.8", round(1.8 * 1024**3)),
        # A blank size is unknown, not zero. Treating it as zero is what routed
        # unsized multi-gigabyte files into the small-file queue.
        ("", "", "", None),
        ("", "MB", "", None),
        ("not-a-number", "MB", "", None),
        ("12", "furlongs", "", None),
    ],
)
def test_parse_size(value: str, unit: str, size_gb: str, expected: int | None) -> None:
    assert parse_size(value, unit, size_gb) == expected


def test_declared_gigabytes_win_over_value_and_unit() -> None:
    # The two columns disagree in the source manifest for rounded rows; the
    # GB column is the one the curator computed from the repository listing.
    assert parse_size("1024", "MB", "2") == 2 * 1024**3
