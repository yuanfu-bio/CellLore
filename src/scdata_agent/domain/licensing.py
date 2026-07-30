"""Repository-level distribution terms for archives that publish no per-record licence.

GEO, SRA and ENA expose no structured licence field. Treating that silence as
"unknown" is literally correct but leaves essentially every public dataset
un-recommendable, because the field will never appear. Treating it as an
approved licence would be worse: it would assert a fact nobody observed.

The resolution here is a third, honestly-named state. When an asset carries no
licence of its own but is served by an archive whose own terms of distribution
cover its holdings, the bundle is marked ``REPOSITORY_POLICY`` and carries the
policy it relies on, so a reviewer can audit the basis rather than a bare verdict.

Two rules keep this from becoming a rubber stamp:

* An explicit licence on the asset always wins. Repository policy only fills a
  genuine gap; it never overrides or upgrades a licence that was actually read.
* Policy applies to openly distributed holdings only. Controlled-access data
  (dbGaP, EGA) is gated earlier by access level and never reaches this code.
"""

from __future__ import annotations

from urllib.parse import urlparse

from scdata_agent.domain.models import StrictModel


class RepositoryLicensePolicy(StrictModel):
    """The distribution terms an archive publishes for its own holdings."""

    policy_id: str
    repository: str
    policy_url: str
    summary: str
    caveat: str

    def as_basis(self) -> str:
        """A one-line, auditable statement of what is being relied on."""
        return f"{self.repository} terms of use ({self.policy_url}): {self.summary}"


# Host suffixes are matched against the asset's observed download URL, so the
# claim is grounded in where the bytes actually come from.
_NCBI = RepositoryLicensePolicy(
    policy_id="ncbi_public_domain_policy",
    repository="NCBI",
    policy_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
    summary=(
        "NCBI places no restrictions on the use or distribution of the data it "
        "holds, and does not accept submissions where the depositor requests "
        "restrictions on reuse or redistribution."
    ),
    caveat=(
        "Individual records may still be encumbered by third-party rights such "
        "as patents, and derived publications carry their own copyright."
    ),
)

_EBI = RepositoryLicensePolicy(
    policy_id="embl_ebi_terms_of_use",
    repository="EMBL-EBI",
    policy_url="https://www.ebi.ac.uk/about/terms-of-use",
    summary=(
        "EMBL-EBI makes its public data resources freely available to all "
        "users without restriction on reuse."
    ),
    caveat=(
        "Freely available does not waive attribution norms; cite the "
        "originating study and archive."
    ),
)

REPOSITORY_LICENSE_POLICIES: tuple[tuple[tuple[str, ...], RepositoryLicensePolicy], ...] = (
    (("ncbi.nlm.nih.gov", "nih.gov"), _NCBI),
    (("ebi.ac.uk", "ensembl.org"), _EBI),
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold()


def policy_for_url(url: str) -> RepositoryLicensePolicy | None:
    """Return the archive policy covering one download URL, if any."""
    host = _host(url)
    if not host:
        return None
    for suffixes, policy in REPOSITORY_LICENSE_POLICIES:
        if any(host == item or host.endswith(f".{item}") for item in suffixes):
            return policy
    return None


def policies_for_urls(urls: list[str]) -> list[RepositoryLicensePolicy]:
    """Return every policy needed to cover the URLs, or nothing if any is uncovered.

    Coverage must be total, but it need not come from a single archive. Splitting
    a deposition across archives is the norm rather than the exception — processed
    matrices on GEO, raw reads on ENA — and both publish open terms, so such a
    bundle rests on two policies rather than none. A single file from an
    unrecognised host still disqualifies the whole bundle, which is the case the
    strictness was actually for.
    """
    if not urls:
        return []
    covering: list[RepositoryLicensePolicy] = []
    for url in urls:
        policy = policy_for_url(url)
        if policy is None:
            return []
        if not any(item.policy_id == policy.policy_id for item in covering):
            covering.append(policy)
    return sorted(covering, key=lambda item: item.policy_id)
