"""Graph analysis tools for CPG data.

Provides tools for high-level graph analysis:
- get_call_graph: Get function call graph
- get_dependencies: Get dependency relationships
- analyze_function: Analyze function details
- get_file_structure: Get file structure
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_server_omnicpg.neo4j_adapter import get_adapter

logger = logging.getLogger(__name__)

FUNCTION_NODE_TYPES: tuple[str, ...] = (
    "function_definition",
    "method_definition",
    "method_declaration",
    "constructor_declaration",
)


def _function_node_filter(alias: str) -> str:
    """Return Cypher filter for function/method nodes across languages."""
    type_list = ", ".join(f"'{node_type}'" for node_type in FUNCTION_NODE_TYPES)
    return f"({alias}.type IN [{type_list}] OR {alias}:Method OR {alias}:Function)"


def get_call_graph(
    function_name: str | None = None,
    depth: int = 2,
    include_callers: bool = True,
    include_callees: bool = True,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Get function call graph.

    Retrieves the call graph for functions, showing which functions call
    which other functions.

    Args:
        function_name: Specific function name to analyze. If None, returns
            call graph for all functions.
        depth: How many levels of the call graph to traverse. Default is 2.
        include_callers: Whether to include functions that call the target.
            Default is True.
        include_callees: Whether to include functions called by the target.
            Default is True.
        project_id: Project ID to scope the query to. If None, the call graph
            spans all projects.

    Returns:
        A dictionary representing the call graph:
        - function: Function name (if specified)
        - depth: Query depth
        - callers: List of functions that call the target (if include_callers)
        - callees: List of functions called by the target (if include_callees)
        - total_edges: Total number of call relationships found

    Raises:
        ValueError: If depth is less than 1.
        ConnectionError: If Neo4j connection fails.
    """
    if depth < 1:
        raise ValueError("depth must be at least 1")

    adapter = get_adapter()
    adapter.ensure_connected()

    # Scope to the configured project when provided (REQ-SCHEMA-006).
    proj_caller = " AND caller.project_id = $project_id" if project_id else ""
    proj_callee = " AND callee.project_id = $project_id" if project_id else ""

    # Build match clause based on function_name
    function_filter = _function_node_filter("f")
    if function_name:
        match_clause = f"MATCH (f:Node {{name: $function_name}}) WHERE {function_filter}"
        params: dict[str, Any] = {"function_name": function_name}
    else:
        match_clause = f"MATCH (f:Node) WHERE {function_filter}"
        params = {}
    if project_id:
        params["project_id"] = project_id

    callers: list[dict[str, Any]] = []
    callees: list[dict[str, Any]] = []

    # Query callers (functions that call the target)
    if include_callers:
        if function_name:
            if depth == 1:
                # Direct callers only
                callers_query = f"""
                    MATCH (caller:Node)-[:CALLS]->(target:Node {{name: $function_name}})
                    WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR caller:Method OR caller:Function)
                      AND (target.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR target:Method OR target:Function){proj_caller}
                    RETURN caller.name AS name,
                           caller.id AS id,
                           caller.file_path AS file_path,
                           1 AS distance
                    LIMIT 50
                """
            else:
                # Multi-level callers - use recursive pattern
                callers_query = f"""
                    MATCH (caller:Node)-[:CALLS*1..{depth - 1}]-(target:Node {{name: $function_name}})
                    WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR caller:Method OR caller:Function)
                      AND (target.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR target:Method OR target:Function)
                      AND NOT (target)-[:CALLS]->(caller){proj_caller}
                    WITH caller, count(*) AS path_count
                    WHERE path_count > 0
                    RETURN caller.name AS name,
                           caller.id AS id,
                           caller.file_path AS file_path,
                           path_count AS distance
                    LIMIT 50
                """
        else:
            if depth == 1:
                # Direct callers for all functions
                callers_query = (
                    match_clause
                    + """
                    MATCH (caller:Node)-[:CALLS]->(f)
                    WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR caller:Method OR caller:Function)
                    RETURN caller.name AS name,
                           caller.id AS id,
                           caller.file_path AS file_path,
                           1 AS distance
                    LIMIT 50
                """
                )
            else:
                # Multi-level callers for all functions - simplified version
                callers_query = f"""
                    MATCH (f:Node)
                    WHERE {function_filter}
                    MATCH (caller:Node)-[:CALLS*1..{depth - 1}]-(f)
                    WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR caller:Method OR caller:Function)
                      AND NOT (f)-[:CALLS]->(caller)
                    WITH f, caller, count(*) AS path_count
                    WHERE path_count > 0
                    RETURN caller.name AS name,
                           caller.id AS id,
                           caller.file_path AS file_path,
                           path_count AS distance
                    LIMIT 50
                """

        callers_results = adapter.query(callers_query, **params)
        callers = [
            {
                "name": r.get("name"),
                "id": r.get("id"),
                "file_path": r.get("file_path"),
                "distance": r.get("distance", 1),
            }
            for r in callers_results
        ]
        if function_name and not callers:
            callers_query = """
                MATCH (cs:Node {name: $function_name})
                WHERE cs:CallSite OR cs.type IN ['call', 'method_invocation']
                MATCH (caller:Node)-[:PARENT_OF*1..15]->(cs)
                WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                    OR caller:Method OR caller:Function)
                RETURN DISTINCT caller.name AS name,
                       caller.id AS id,
                       caller.file_path AS file_path,
                       1 AS distance
                LIMIT 50
            """
            callers_results = adapter.query(callers_query, **params)
            callers = [
                {
                    "name": r.get("name"),
                    "id": r.get("id"),
                    "file_path": r.get("file_path"),
                    "distance": r.get("distance", 1),
                }
                for r in callers_results
            ]

    # Query callees (functions called by the target)
    if include_callees:
        if function_name:
            if depth == 1:
                # Direct callees only
                callees_query = f"""
                    MATCH (source:Node {{name: $function_name}})-[:CALLS]->(callee:Node)
                    WHERE (source.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR source:Method OR source:Function)
                      AND (callee.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR callee:Method OR callee:Function){proj_callee}
                    RETURN callee.name AS name,
                           callee.id AS id,
                           callee.file_path AS file_path,
                           1 AS distance
                    LIMIT 50
                """
            else:
                # Multi-level callees - use recursive pattern
                callees_query = f"""
                    MATCH (source:Node {{name: $function_name}})-[:CALLS*1..{depth - 1}]-(callee:Node)
                    WHERE (source.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR source:Method OR source:Function)
                      AND (callee.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR callee:Method OR callee:Function)
                      AND NOT (callee)-[:CALLS]->(source){proj_callee}
                    WITH source, callee, count(*) AS path_count
                    WHERE path_count > 0
                    RETURN callee.name AS name,
                           callee.id AS id,
                           callee.file_path AS file_path,
                           path_count AS distance
                    LIMIT 50
                """
        else:
            if depth == 1:
                # Direct callees for all functions
                callees_query = (
                    match_clause
                    + """
                    MATCH (f)-[:CALLS]->(callee:Node)
                    WHERE (callee.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR callee:Method OR callee:Function)
                    RETURN callee.name AS name,
                           callee.id AS id,
                           callee.file_path AS file_path,
                           1 AS distance
                    LIMIT 50
                """
                )
            else:
                # Multi-level callees for all functions - simplified version
                callees_query = f"""
                    MATCH (f:Node)
                    WHERE {function_filter}
                    MATCH (f)-[:CALLS*1..{depth - 1}]-(callee:Node)
                    WHERE (callee.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                        OR callee:Method OR callee:Function)
                      AND NOT (callee)-[:CALLS]->(f)
                    WITH f, callee, count(*) AS path_count
                    WHERE path_count > 0
                    RETURN callee.name AS name,
                           callee.id AS id,
                           callee.file_path AS file_path,
                           path_count AS distance
                    LIMIT 50
                """

        callees_results = adapter.query(callees_query, **params)
        callees = [
            {
                "name": r.get("name"),
                "id": r.get("id"),
                "file_path": r.get("file_path"),
                "distance": r.get("distance", 1),
            }
            for r in callees_results
        ]
        if function_name and not callees:
            callees_query = """
                MATCH (f:Node {name: $function_name})
                WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                    OR f:Method OR f:Function)
                MATCH (f)-[:PARENT_OF*1..15]->(cs:Node)
                WHERE cs:CallSite OR cs.type IN ['call', 'method_invocation']
                RETURN DISTINCT cs.name AS name,
                       cs.id AS id,
                       cs.file_path AS file_path,
                       1 AS distance
                LIMIT 50
            """
            callees_results = adapter.query(callees_query, **params)
            callees = [
                {
                    "name": r.get("name"),
                    "id": r.get("id"),
                    "file_path": r.get("file_path"),
                    "distance": r.get("distance", 1),
                }
                for r in callees_results
            ]

    result: dict[str, Any] = {
        "function": function_name,
        "depth": depth,
        "callers": callers,
        "callees": callees,
        "total_edges": len(callers) + len(callees),
    }

    logger.info(
        "Retrieved call graph for %s (depth=%d, callers=%d, callees=%d)",
        function_name or "all functions",
        depth,
        len(callers),
        len(callees),
    )

    return result


def get_dependencies(
    node_id: str,
    dependency_type: str = "both",
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Get dependency relationships for a node.

    Retrieves nodes that depend on the target or that the target depends on.

    Args:
        node_id: The node ID to analyze.
        dependency_type: Type of dependencies to retrieve:
            - "inbound": Nodes that depend on the target
            - "outbound": Nodes that the target depends on
            - "both": Both inbound and outbound dependencies (default)
        project_id: Project ID to scope the query to. If None, dependencies
            span all projects.

    Returns:
        A list of dependency dictionaries, each containing:
        - dependent_id: ID of the dependent node
        - dependency_type: Type of dependency (inbound/outbound)
        - relationship_types: List of relationship types between nodes
        - node_info: Basic information about the dependent node

    Raises:
        ValueError: If dependency_type is invalid.
        ConnectionError: If Neo4j connection fails.
    """
    if dependency_type not in ["inbound", "outbound", "both"]:
        raise ValueError("dependency_type must be 'inbound', 'outbound', or 'both'")

    adapter = get_adapter()
    adapter.ensure_connected()

    dep_params: dict[str, Any] = {"node_id": node_id}
    if project_id:
        dep_params["project_id"] = project_id
    proj_target = " AND target.project_id = $project_id" if project_id else ""
    proj_source = " AND source.project_id = $project_id" if project_id else ""

    dependencies: list[dict[str, Any]] = []

    # Query outbound dependencies (nodes that the target depends on)
    if dependency_type in ["outbound", "both"]:
        outbound_query = f"""
            MATCH (source:Node {{id: $node_id}})-[r]->(target:Node)
            WHERE true{proj_target}
            RETURN target.id AS dependent_id,
                   type(r) AS relationship_type,
                   target.name AS name,
                   labels(target) AS labels
            LIMIT 50
        """
        outbound_results = adapter.query(outbound_query, **dep_params)

        for result in outbound_results:
            labels = result.get("labels", [])
            node_type = next((lbl for lbl in labels if lbl != "Node"), None)

            dependencies.append(
                {
                    "dependent_id": result.get("dependent_id"),
                    "dependency_type": "outbound",
                    "relationship_types": [result.get("relationship_type")],
                    "node_info": {
                        "name": result.get("name"),
                        "type": node_type,
                    },
                }
            )

    # Query inbound dependencies (nodes that depend on the target)
    if dependency_type in ["inbound", "both"]:
        inbound_query = f"""
            MATCH (source:Node)-[r]->(target:Node {{id: $node_id}})
            WHERE true{proj_source}
            RETURN source.id AS dependent_id,
                   type(r) AS relationship_type,
                   source.name AS name,
                   labels(source) AS labels
            LIMIT 50
        """
        inbound_results = adapter.query(inbound_query, **dep_params)

        for result in inbound_results:
            labels = result.get("labels", [])
            node_type = next((lbl for lbl in labels if lbl != "Node"), None)

            dependencies.append(
                {
                    "dependent_id": result.get("dependent_id"),
                    "dependency_type": "inbound",
                    "relationship_types": [result.get("relationship_type")],
                    "node_info": {
                        "name": result.get("name"),
                        "type": node_type,
                    },
                }
            )

    logger.info(
        "Retrieved %d dependencies for node %s (type=%s)",
        len(dependencies),
        node_id,
        dependency_type,
    )

    return dependencies


def analyze_function(function_id: str, project_id: str | None = None) -> dict[str, Any] | None:
    """Analyze function details and relationships.

    Provides comprehensive analysis of a function, including its signature,
    callers, callees, and usage of variables.

    Args:
        function_id: The function ID to analyze.
        project_id: Project ID to scope the lookup to. If None, the function is
            matched across all projects.

    Returns:
        A dictionary containing function analysis:
        - id: Function ID
        - name: Function name
        - signature: Function signature (if available)
        - file_path: File path where the function is defined
        - line_number: Line number (if available)
        - callers: Functions that call this function
        - callees: Functions called by this function
        - variables_used: Variables used by this function
        - variables_defined: Variables defined by this function
        - complexity_metrics: Complexity metrics (if available)

        Returns None if function not found.

    Raises:
        ConnectionError: If Neo4j connection fails.
    """
    if not function_id:
        return None

    adapter = get_adapter()
    adapter.ensure_connected()

    # Query function details
    function_filter = _function_node_filter("f")
    proj_f = " AND f.project_id = $project_id" if project_id else ""
    fn_params: dict[str, Any] = {"function_id": function_id}
    if project_id:
        fn_params["project_id"] = project_id
    query = f"""
        MATCH (f:Node {{id: $function_id}})
        WHERE {function_filter}{proj_f}
        RETURN f.id AS id,
               f.name AS name,
               f.code AS signature,
               f.file_path AS file_path,
               f.line_start AS line_number,
               properties(f) AS properties
    """

    results = adapter.query(query, **fn_params)

    if not results:
        logger.warning("Function with ID %s not found", function_id)
        return None

    result = results[0]

    # Query callers
    callers_query = """
        MATCH (caller:Node)-[:CALLS]->(f:Node {id: $function_id})
        WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR caller:Method OR caller:Function)
          AND (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR f:Method OR f:Function)
        RETURN caller.name AS name,
               caller.id AS id,
               caller.file_path AS file_path
        LIMIT 50
    """
    callers_results = adapter.query(callers_query, function_id=function_id)
    callers = [
        {
            "name": r.get("name"),
            "id": r.get("id"),
            "file_path": r.get("file_path"),
        }
        for r in callers_results
    ]
    if not callers:
        callers_query = """
            MATCH (f:Node {id: $function_id})
            WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                OR f:Method OR f:Function)
            MATCH (cs:Node {name: f.name})
            WHERE cs:CallSite OR cs.type IN ['call', 'method_invocation']
            MATCH (caller:Node)-[:PARENT_OF*1..15]->(cs)
            WHERE (caller.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                OR caller:Method OR caller:Function)
            RETURN DISTINCT caller.name AS name,
                   caller.id AS id,
                   caller.file_path AS file_path
            LIMIT 50
        """
        callers_results = adapter.query(callers_query, function_id=function_id)
        callers = [
            {
                "name": r.get("name"),
                "id": r.get("id"),
                "file_path": r.get("file_path"),
            }
            for r in callers_results
        ]

    # Query callees
    callees_query = """
        MATCH (f:Node {id: $function_id})-[:CALLS]->(callee:Node)
        WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR f:Method OR f:Function)
          AND (callee.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR callee:Method OR callee:Function)
        RETURN callee.name AS name,
               callee.id AS id,
               callee.file_path AS file_path
        LIMIT 50
    """
    callees_results = adapter.query(callees_query, function_id=function_id)
    callees = [
        {
            "name": r.get("name"),
            "id": r.get("id"),
            "file_path": r.get("file_path"),
        }
        for r in callees_results
    ]
    if not callees:
        callees_query = """
            MATCH (f:Node {id: $function_id})
            WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
                OR f:Method OR f:Function)
            MATCH (f)-[:PARENT_OF*1..15]->(cs:Node)
            WHERE cs:CallSite OR cs.type IN ['call', 'method_invocation']
            RETURN DISTINCT cs.name AS name,
                   cs.id AS id,
                   cs.file_path AS file_path
            LIMIT 50
        """
        callees_results = adapter.query(callees_query, function_id=function_id)
        callees = [
            {
                "name": r.get("name"),
                "id": r.get("id"),
                "file_path": r.get("file_path"),
            }
            for r in callees_results
        ]

    # Query used variables
    # Note: USES relationship not available in current schema
    variables_used_query = """
        MATCH (f:Node {id: $function_id})-[:PARENT_OF*]->(v:Node {type: 'identifier'})
        WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR f:Method OR f:Function)
        RETURN v.name AS name,
               v.id AS id
        LIMIT 50
    """
    variables_used_results = adapter.query(variables_used_query, function_id=function_id)
    variables_used = [
        {
            "name": r.get("name"),
            "id": r.get("id"),
        }
        for r in variables_used_results
    ]

    # Query defined variables
    # Note: DEFINES relationship not available in current schema
    variables_defined_query = """
        MATCH (f:Node {id: $function_id})-[:PARENT_OF]->(assign:Node {type: 'assignment'})-[:PARENT_OF]->(v:Node {type: 'identifier'})
        WHERE (f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']
            OR f:Method OR f:Function)
        RETURN v.name AS name,
               v.id AS id
        LIMIT 50
    """
    variables_defined_results = adapter.query(variables_defined_query, function_id=function_id)
    variables_defined = [
        {
            "name": r.get("name"),
            "id": r.get("id"),
        }
        for r in variables_defined_results
    ]

    analysis: dict[str, Any] = {
        "id": result.get("id"),
        "name": result.get("name"),
        "signature": result.get("signature"),
        "file_path": result.get("file_path"),
        "line_number": result.get("line_number"),
        "callers": callers,
        "callees": callees,
        "variables_used": variables_used,
        "variables_defined": variables_defined,
        "complexity_metrics": {
            "num_callers": len(callers),
            "num_callees": len(callees),
            "num_variables_used": len(variables_used),
            "num_variables_defined": len(variables_defined),
        },
    }

    logger.info(
        "Analyzed function %s: %d callers, %d callees",
        result.get("name"),
        len(callers),
        len(callees),
    )

    return analysis


def get_file_structure(file_path: str, project_id: str | None = None) -> dict[str, Any] | None:
    """Get structure of a source code file.

    Retrieves all classes, functions, and variables defined in a file.

    Args:
        file_path: The file path to analyze.
        project_id: Project ID to scope the query to. If None, matches the file
            across all projects.

    Returns:
        A dictionary containing file structure:
        - file_path: File path
        - classes: List of classes defined in the file
        - functions: List of functions defined in the file
        - variables: List of variables defined in the file
        - total_elements: Total number of elements found

        Returns None if file not found or has no CPG data.

    Raises:
        ConnectionError: If Neo4j connection fails.
    """
    if not file_path:
        return None

    adapter = get_adapter()
    adapter.ensure_connected()

    fs_params: dict[str, Any] = {"file_path": file_path}
    if project_id:
        fs_params["project_id"] = project_id
    proj_c = " AND c.project_id = $project_id" if project_id else ""
    proj_fn = " AND f.project_id = $project_id" if project_id else ""
    proj_v = " AND v.project_id = $project_id" if project_id else ""

    # Query classes
    classes_query = f"""
        MATCH (c:Node {{file_path: $file_path}})
        WHERE c.type IN ['class_definition', 'class_declaration', 'interface_declaration', 'enum_declaration']{proj_c}
        RETURN c.id AS id,
               c.name AS name,
               c.line_start AS line_number
        ORDER BY c.line_start
    """
    classes_results = adapter.query(classes_query, **fs_params)
    classes = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "line_number": r.get("line_number"),
        }
        for r in classes_results
    ]

    # Query functions
    functions_query = f"""
        MATCH (f:Node {{file_path: $file_path}})
        WHERE f.type IN ['function_definition', 'method_definition', 'method_declaration', 'constructor_declaration']{proj_fn}
           RETURN f.id AS id,
               f.name AS name,
               f.code AS signature,
               f.line_start AS line_number
        ORDER BY f.line_start
    """
    functions_results = adapter.query(functions_query, **fs_params)
    functions = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "signature": r.get("signature"),
            "line_number": r.get("line_number"),
        }
        for r in functions_results
    ]

    # Query variables
    # Note: Variable nodes not available, using identifier nodes instead
    variables_query = f"""
        MATCH (v:Node {{file_path: $file_path, type: 'identifier'}})
        WHERE true{proj_v}
        RETURN v.id AS id,
               v.name AS name,
               v.type AS type,
               v.line_start AS line_number
        ORDER BY v.line_start
        LIMIT 100
    """
    variables_results = adapter.query(variables_query, **fs_params)
    variables = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "type": r.get("type"),
            "line_number": r.get("line_number"),
        }
        for r in variables_results
    ]

    if not classes and not functions and not variables:
        logger.warning("No CPG data found for file %s", file_path)
        return None

    structure: dict[str, Any] = {
        "file_path": file_path,
        "classes": classes,
        "functions": functions,
        "variables": variables,
        "total_elements": len(classes) + len(functions) + len(variables),
    }

    logger.info(
        "Retrieved structure for file %s: %d classes, %d functions, %d variables",
        file_path,
        len(classes),
        len(functions),
        len(variables),
    )

    return structure
