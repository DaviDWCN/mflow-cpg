"""Tests for the LSIFPlugin."""

from omnicpg.plugins.lsif_plugin.plugin import LSIFPlugin


def test_lsif_supported_extensions() -> None:
    """Test that the plugin handles the correct extensions."""
    plugin = LSIFPlugin()
    exts = plugin.supported_extensions
    assert ".lsif" in exts


def test_lsif_empty_methods() -> None:
    """Test that the plugin returns empty lists for now."""
    plugin = LSIFPlugin()

    nodes, edges = plugin.parse_to_ast("test.lsif", "")
    assert len(nodes) == 0
    assert len(edges) == 0

    assert plugin.build_cfg([]) == []
    assert plugin.build_dfg([], []) == []
    assert plugin.build_call_graph([]) == []
