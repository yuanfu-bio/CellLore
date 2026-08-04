"""Architecture boundaries for the layered package layout."""

from importlib import import_module
from pathlib import Path


def test_public_entrypoints_and_all_specialists_import():
    import_module("scdata_agent.interfaces.api")
    import_module("scdata_agent.interfaces.cli")
    for module in (
        "planning", "discovery", "supervisor", "shared", "evidence_advice",
        "progressive_evidence", "evidence_escalation", "progressive_asset_scout",
        "manifest_ingest", "inventory_reconcile", "progressive_preflight",
        "progressive_lineage_v2", "acquisition_v2", "transfer_execution", "curator",
        "materialized_quality", "snapshot_governance",
    ):
        import_module(f"scdata_agent.application.agents.{module}")


def test_retired_v1_modules_are_absent():
    """The v1 graph was fully superseded; keep the package free of dead paths."""
    agents = Path(__file__).parents[1] / "src" / "scdata_agent" / "application" / "agents"
    application = agents.parent
    retired = {
        agents / name for name in (
            "acquisition.py", "asset_scout.py", "curated_evidence.py", "evidence.py",
            "governance.py", "lineage.py", "preflight.py", "progressive_lineage.py",
            "quality.py", "transfer.py",
        )
    } | {application / "orchestration.py"}
    assert not [path for path in retired if path.exists()]


def test_domain_does_not_depend_on_application_or_infrastructure():
    domain = Path(__file__).parents[1] / "src" / "scdata_agent" / "domain"
    forbidden = ("scdata_agent.application", "scdata_agent.infrastructure", "scdata_agent.interfaces")
    for path in domain.glob("*.py"):
        assert not any(marker in path.read_text() for marker in forbidden), path
