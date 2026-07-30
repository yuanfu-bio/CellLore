def test_studio_export_loads_supervisor_graph():
    from scdata_agent.studio import graph

    assert "initialize_run" in graph.nodes
    assert "download_approval" in graph.nodes
