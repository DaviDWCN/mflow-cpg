"""Tests for the OpenAPIPlugin."""

import json

from omnicpg.plugins.openapi_plugin.plugin import OpenAPIPlugin


def test_openapi_supported_extensions() -> None:
    """Test that the plugin handles the correct extensions."""
    plugin = OpenAPIPlugin()
    exts = plugin.supported_extensions
    assert ".json" in exts
    assert ".yaml" in exts
    assert ".yml" in exts


def test_openapi_plugin_parses_endpoints() -> None:
    """Test parsing an OpenAPI JSON and extracting endpoints."""
    plugin = OpenAPIPlugin()
    source_code = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Sample API", "version": "1.0"},
            "paths": {
                "/users": {
                    "get": {"operationId": "getUsers", "description": "Returns a list of users"},
                    "post": {"operationId": "createUser"},
                },
                "/ping": {"get": {}},
            },
        }
    )

    nodes, edges = plugin.parse_to_ast("swagger.json", source_code)

    assert len(nodes) == 4  # 1 File node + 3 Endpoint nodes
    assert len(edges) == 3  # File -> Endpoints

    file_nodes = [n for n in nodes if n.properties.get("type") == "module"]
    assert len(file_nodes) == 1
    assert file_nodes[0].properties.get("name") == "swagger.json"

    endpoint_nodes = [n for n in nodes if n.properties.get("type") == "api_endpoint"]
    assert len(endpoint_nodes) == 3

    get_users = next((n for n in endpoint_nodes if n.properties.get("name") == "getUsers"), None)
    assert get_users is not None
    assert get_users.properties.get("http_method") == "GET"
    assert get_users.properties.get("route") == "/users"

    post_user = next((n for n in endpoint_nodes if n.properties.get("name") == "createUser"), None)
    assert post_user is not None
    assert post_user.properties.get("http_method") == "POST"

    ping = next((n for n in endpoint_nodes if n.properties.get("name") == "GET /ping"), None)
    assert ping is not None
    assert ping.properties.get("http_method") == "GET"
    assert ping.properties.get("route") == "/ping"


def test_openapi_plugin_ignores_non_json_or_invalid() -> None:
    """Test handling of invalid files or missing OpenAPI structure."""
    plugin = OpenAPIPlugin()

    # Non json file
    nodes, _edges = plugin.parse_to_ast("api.txt", '{"openapi": "3.0.0"}')
    assert len(nodes) == 0

    # Invalid json
    nodes, _edges = plugin.parse_to_ast("swagger.json", '{"openapi": "3.0.0"')
    assert len(nodes) == 0

    # Valid json but not openapi/swagger
    nodes, _edges = plugin.parse_to_ast("config.json", '{"name": "test"}')
    assert len(nodes) == 0


def test_openapi_empty_flow_methods() -> None:
    """Test that the data and control flow methods return empty lists."""
    plugin = OpenAPIPlugin()
    assert plugin.build_cfg([]) == []
    assert plugin.build_dfg([], []) == []
    assert plugin.build_call_graph([]) == []
