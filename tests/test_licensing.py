"""Repository-level licence policy and its precedence rules.

The relaxation here is deliberate but narrow: an archive's published terms may
stand in for a missing per-record licence, and never for anything else. These
tests pin the boundaries, because loosening them silently is how a curation
tool ends up asserting reuse rights nobody granted.
"""

import pytest

from scdata_agent.domain.licensing import (
    REPOSITORY_LICENSE_POLICIES,
    policies_for_urls,
    policy_for_url,
)
from scdata_agent.domain.lifecycle import LicenseStatus


class TestPolicyMatching:
    @pytest.mark.parametrize(
        ("url", "repository"),
        [
            ("https://ftp.ncbi.nlm.nih.gov/geo/series/GSE1nnn/file.tar", "NCBI"),
            ("https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE1", "NCBI"),
            ("ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/SRR1_1.fastq.gz", "EMBL-EBI"),
            ("https://www.ebi.ac.uk/ena/browser/api/xml/PRJNA1", "EMBL-EBI"),
        ],
    )
    def test_known_archives_are_recognised(self, url, repository):
        policy = policy_for_url(url)
        assert policy is not None and policy.repository == repository

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/data.tar",
            "https://zenodo.org/api/records/1/files/x.h5ad",
            "https://ncbi.nlm.nih.gov.evil.test/data.tar",  # suffix must be a real boundary
            "not-a-url",
            "",
        ],
    )
    def test_unknown_or_spoofed_hosts_get_no_policy(self, url):
        assert policy_for_url(url) is None

    def test_every_policy_carries_an_auditable_basis(self):
        for _, policy in REPOSITORY_LICENSE_POLICIES:
            assert policy.policy_url.startswith("https://")
            assert policy.summary and policy.caveat
            assert policy.policy_url in policy.as_basis()


class TestTotalCoverage:
    def test_a_bundle_within_one_archive_cites_one_policy(self):
        policies = policies_for_urls([
            "https://ftp.ncbi.nlm.nih.gov/geo/a.tar",
            "https://ftp.ncbi.nlm.nih.gov/geo/b.tar",
        ])
        assert [item.repository for item in policies] == ["NCBI"]

    def test_a_deposition_split_across_archives_cites_both(self):
        """GEO-processed plus ENA-raw is the normal shape, not an anomaly."""
        policies = policies_for_urls([
            "https://ftp.ncbi.nlm.nih.gov/geo/a.tar",
            "ftp://ftp.sra.ebi.ac.uk/vol1/b.fastq.gz",
        ])
        assert {item.repository for item in policies} == {"NCBI", "EMBL-EBI"}

    def test_one_uncovered_file_disqualifies_the_whole_bundle(self):
        """Coverage must be total; a single unknown host voids the basis."""
        assert policies_for_urls([
            "https://ftp.ncbi.nlm.nih.gov/geo/a.tar",
            "https://example.com/b.tar",
        ]) == []

    def test_no_urls_means_no_policy(self):
        assert policies_for_urls([]) == []


def test_repository_policy_is_a_distinct_status_from_approved():
    """The two must never be conflated: one was read, the other inferred."""
    assert LicenseStatus.REPOSITORY_POLICY != LicenseStatus.APPROVED
    assert LicenseStatus.REPOSITORY_POLICY.value == "repository_policy"
