"""ASTBuilder — converts Java source to CPG AST nodes and edges via Tree-sitter.

Handles three file types:

* ``.java`` — parsed directly with tree-sitter-java.
* ``.jsp``  — Java code extracted from scriptlet blocks (``<% %>``,
  ``<%= %>``, ``<%! %>``) and then parsed with tree-sitter-java.
* ``.xml``  — framework configuration files (Spring, Struts, Hibernate)
  parsed with Python's ``xml.etree.ElementTree`` and converted to CPG nodes
  that capture bean definitions, action mappings, and entity mappings.

Additionally emits a **macro-skeleton** overlay for ``.java`` files:

* A ``:File`` node per file.
* ``CONTAINS`` edges (``File → Class → Method``).
* ``DEPENDS_ON`` edges for ``import`` declarations.
* Enriched properties (``signature``, ``docstring``, ``source_code``) on
  ``:Method`` and ``:Class`` nodes.

Enterprise Java enhancements:

* **Hibernate HBM** — ``<id>``, ``<many-to-one>``, ``<one-to-many>``,
  ``<set>``, ``<bag>``, ``<map>``, ``<composite-id>`` element parsing.
* **Spring XML** — ``<property ref="...">`` / ``<constructor-arg ref="...">``
  dependency injection, ``<import resource="...">`` tracking.
* **Struts 1.x** — ``<forward>`` navigation, ``<message-resources>`` i18n.
* **Acegi Security** — filter chain and URL patterns.
* **DWR** — remote AJAX method exposure.
* **Business module** and **architecture layer** tagging from file paths
  and class annotations.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge, EdgeType
from omnicpg.models.node import CPGNode
from omnicpg.plugins.java_plugin.security_rules import classify_invocation
from omnicpg.utils.id_gen import generate_deterministic_id

logger = logging.getLogger(__name__)

# Max length of each raw argument-expression string stored on call sites.
# Argument texts feed type-based overload disambiguation; capping keeps the
# node payload small while retaining enough prefix for type inference.
_MAX_ARG_EXPR_LENGTH = 120

# Tree-sitter node types that carry a meaningful ``name`` child (via field name "name").
_NAMED_NODE_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "method_declaration",
        "constructor_declaration",
        "annotation_type_declaration",
    }
)

# Annotation-related helpers for Spring / Struts / Hibernate framework detection.
_SPRING_ANNOTATIONS = frozenset(
    {
        "Controller",
        "RestController",
        "Service",
        "Repository",
        "Component",
        "Configuration",
        "Bean",
        "RequestMapping",
        "GetMapping",
        "PostMapping",
        "PutMapping",
        "DeleteMapping",
        "Autowired",
        "Value",
        "Transactional",
    }
)

_HIBERNATE_ANNOTATIONS = frozenset(
    {
        "Entity",
        "Table",
        "Column",
        "Id",
        "GeneratedValue",
        "OneToMany",
        "ManyToOne",
        "ManyToMany",
        "OneToOne",
        "JoinColumn",
        "NamedQuery",
        "NamedQueries",
    }
)

_STRUTS_BASE_CLASSES = frozenset(
    {
        "Action",
        "DispatchAction",
        "MappingDispatchAction",
        "LookupDispatchAction",
        "ActionForm",
        "DynaActionForm",
        "ValidatorForm",
        "DynaValidatorForm",
    }
)

# Regex to extract Java code from JSP scriptlet blocks.
_JSP_SCRIPTLET_RE = re.compile(
    r"<%[!=]?\s*(.*?)\s*%>",
    re.DOTALL,
)

# Regex to extract Struts HTML tag references in JSP files.
_JSP_STRUTS_FORM_RE = re.compile(
    r'<html:form\s[^>]*action\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_JSP_STRUTS_LINK_RE = re.compile(
    r'<html:link\s[^>]*page\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)

# File-name patterns that identify framework configuration XML files.
# XML files that don't match any of these are classified as ``xml_data``
# and receive only a lightweight root node (no deep element parsing).
_FRAMEWORK_XML_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"struts-.*\.xml$", re.IGNORECASE),
    re.compile(r"spring-.*\.xml$", re.IGNORECASE),
    re.compile(r"applicationContext.*\.xml$", re.IGNORECASE),
    re.compile(r"hibernate\.cfg\.xml$", re.IGNORECASE),
    re.compile(r".*\.hbm\.xml$", re.IGNORECASE),
    re.compile(r"web\.xml$", re.IGNORECASE),
    re.compile(r"dwr\.xml$", re.IGNORECASE),
    re.compile(r"acegi-.*\.xml$", re.IGNORECASE),
    re.compile(r"security-.*\.xml$", re.IGNORECASE),
    re.compile(r"beans.*\.xml$", re.IGNORECASE),
    re.compile(r"dispatcher-.*\.xml$", re.IGNORECASE),
)

# Path segments that indicate the XML file lives in a framework config
# directory (even if the file name itself is generic).
_FRAMEWORK_XML_PATH_SEGMENTS = frozenset(
    {
        "WEB-INF",
        "config",
        "resources",
        "META-INF",
    }
)

# ── Business module detection ────────────────────────────────────────────
# Map path segments to business module names.
_BUSINESS_MODULE_MAP: dict[str, str] = {
    "bizinfo": "bizinfo",
    "claim": "claim",
    "payment": "payment",
    "prpall": "policy_service",
    "undwrt": "underwriting",
    "reins": "reinsurance",
    "sales": "sales",
    "platform": "platform",
    "workflow": "workflow",
    "print": "print",
    "common": "common",
}

# ── Architecture layer detection ─────────────────────────────────────────
_PRESENTATION_ANNOTATIONS = frozenset({"Controller", "RestController"})
_SERVICE_ANNOTATIONS = frozenset({"Service"})
_PERSISTENCE_ANNOTATIONS = frozenset({"Repository"})
_DOMAIN_ANNOTATIONS = frozenset({"Entity", "Table"})
_PRESENTATION_SUPERCLASSES = frozenset(
    {
        "Action",
        "DispatchAction",
        "MappingDispatchAction",
        "LookupDispatchAction",
    }
)
_PERSISTENCE_SUPERCLASSES = frozenset(
    {
        "HibernateDaoSupport",
        "JdbcDaoSupport",
    }
)

# Tree-sitter node types considered *skeleton* nodes in ARCHITECTURAL mode.
_SKELETON_TYPES = frozenset(
    {
        "program",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "method_declaration",
        "constructor_declaration",
        "field_declaration",
        "annotation_type_declaration",
        "class_body",
        "interface_body",
        "enum_body",
        "package_declaration",
        "import_declaration",
    }
)

# Node types to preserve inside method bodies for call graph construction.
# In ARCHITECTURAL mode, we still need call-site nodes to build CALLS edges.
_CALL_GRAPH_TYPES = frozenset(
    {
        "method_invocation",
        "object_creation_expression",
        "method_reference",
    }
)

# Minimal method-body metadata retained in ARCHITECTURAL mode so call-graph
# resolution can infer receiver types without expanding the full method body.
_TYPE_CONTEXT_TYPES = frozenset(
    {
        "formal_parameter",
        "local_variable_declaration",
        "enhanced_for_statement",
        "catch_formal_parameter",
    }
)

# Statement-level node types retained in STRUCTURAL mode.
_STRUCTURAL_TYPES = _SKELETON_TYPES | frozenset(
    {
        "expression_statement",
        "return_statement",
        "local_variable_declaration",
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "try_statement",
        "try_with_resources_statement",
        "switch_expression",
        "throw_statement",
        "break_statement",
        "continue_statement",
        "assert_statement",
        "block",
        "catch_clause",
        "finally_clause",
    }
)


class ASTBuilder:
    """Build CPG AST sub-graph from Java source using Tree-sitter.

    Also handles JSP files (scriptlet extraction) and framework XML
    configuration files (Spring, Struts 1.x, Hibernate).

    Usage::

        builder = ASTBuilder()
        nodes, edges = builder.build("UserService.java", source_text)
    """

    def __init__(self) -> None:
        """Initialise tree-sitter parser with the Java grammar."""
        self._language = Language(tsjava.language())
        self._parser = Parser(self._language)

    # ── Public API ────────────────────────────────────────────────────────

    def build(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse *source_code* and return ``(nodes, edges)``.

        Args:
            file_path: Used to annotate nodes with file-level metadata.
            source_code: The full source text.
            analysis_level: Desired analysis granularity.

        Returns:
            A tuple of all AST nodes and edges.
        """
        if file_path.endswith(".jsp"):
            return self._build_jsp(file_path, source_code, analysis_level)
        if file_path.endswith(".xml"):
            return self._build_xml(file_path, source_code)
        if file_path.endswith(".properties"):
            return self._build_properties(file_path, source_code)
        return self._build_java(file_path, source_code, analysis_level)

    # ── Java parsing ─────────────────────────────────────────────────────

    def _build_java(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse a ``.java`` file with tree-sitter."""
        tree = self._parser.parse(source_code.encode("utf-8"))
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []
        package = _extract_java_package(tree.root_node)
        self._walk(
            tree.root_node,
            file_path,
            nodes,
            edges,
            parent_id=None,
            analysis_level=analysis_level,
            package=package,
            scope_fqn=package,
        )

        # Macro-skeleton overlay.
        skeleton_nodes, skeleton_edges = _build_java_skeleton(nodes, file_path)
        nodes.extend(skeleton_nodes)
        edges.extend(skeleton_edges)

        logger.info("AST for %s: %d nodes, %d edges", file_path, len(nodes), len(edges))
        return nodes, edges

    # ── JSP parsing ──────────────────────────────────────────────────────

    def _build_jsp(
        self,
        file_path: str,
        source_code: str,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Extract Java code from JSP scriptlets and parse each block.

        JSP scriptlet forms handled:

        * ``<% ... %>``  — statements
        * ``<%= ... %>`` — expressions
        * ``<%! ... %>`` — declarations
        """
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []

        # Choose edge type for the synthetic root linkage.
        root_edge_type = (
            EdgeType.CONTAINS
            if analysis_level == AnalysisLevel.ARCHITECTURAL
            else EdgeType.PARENT_OF
        )

        # Create a synthetic JSP module node as the root.
        root_id = generate_deterministic_id(file_path, "jsp_page", file_path, 1)
        root_node = CPGNode(
            id=root_id,
            labels=("Node", "Module"),
            properties=MappingProxyType(
                {
                    "type": "jsp_page",
                    "code": "",
                    "line_start": 1,
                    "line_end": source_code.count("\n") + 1,
                    "file_path": file_path,
                }
            ),
        )
        nodes.append(root_node)

        # Extract and parse each scriptlet block.
        for match in _JSP_SCRIPTLET_RE.finditer(source_code):
            snippet = match.group(1).strip()
            if not snippet:
                continue
            # Wrap bare expressions in a statement context.
            if source_code[match.start() : match.start() + 3] == "<%=":
                snippet = f"Object __expr = {snippet};"
            # Wrap snippet in a method body so tree-sitter can parse it.
            wrapped = f"class __JSP {{ void __scriptlet() {{ {snippet} }} }}"
            tree = self._parser.parse(wrapped.encode("utf-8"))
            block_nodes: list[CPGNode] = []
            block_edges: list[CPGEdge] = []
            self._walk(
                tree.root_node,
                file_path,
                block_nodes,
                block_edges,
                parent_id=None,
                analysis_level=analysis_level,
            )
            # Re-parent top-level node under the JSP root.
            if block_nodes:
                edges.append(
                    CPGEdge(
                        source_id=root_id,
                        target_id=block_nodes[0].id,
                        edge_type=root_edge_type,
                    )
                )
            nodes.extend(block_nodes)
            edges.extend(block_edges)

        logger.info("JSP AST for %s: %d nodes, %d edges", file_path, len(nodes), len(edges))

        # ── Struts HTML tag references (JSP → Action path) ───────────
        struts_refs = _extract_jsp_struts_refs(source_code)
        for action_path in struts_refs:
            ref_id = generate_deterministic_id(file_path, "struts_action_ref", action_path, 1)
            ref_node = CPGNode(
                id=ref_id,
                labels=("Node", "StrutsActionRef"),
                properties=MappingProxyType(
                    {
                        "type": "struts_action_ref",
                        "code": action_path,
                        "line_start": 1,
                        "line_end": 1,
                        "file_path": file_path,
                        "action_path": action_path,
                    }
                ),
            )
            nodes.append(ref_node)
            edges.append(
                CPGEdge(
                    source_id=root_id,
                    target_id=ref_id,
                    edge_type=EdgeType.CALLS,
                    properties=MappingProxyType({"action_path": action_path}),
                )
            )

        return nodes, edges

    # ── XML config parsing ───────────────────────────────────────────────

    def _build_xml(self, file_path: str, source_code: str) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse a framework XML configuration file.

        Recognised configuration patterns:

        * **Spring** ``<bean>`` definitions, ``<property ref>`` /
          ``<constructor-arg ref>`` injection, ``<import resource>``.
        * **Struts 1.x** ``<action>`` mappings, ``<forward>`` navigation,
          ``<form-bean>`` declarations, ``<message-resources>`` i18n.
        * **Hibernate** ``<class>`` / ``<hibernate-mapping>`` elements and
          rich HBM mapping (``<id>``, ``<many-to-one>``, ``<one-to-many>``,
          ``<set>``, ``<bag>``, ``<map>``, ``<composite-id>``).
        * **Acegi Security** ``<filter-chain-map>`` / URL patterns.
        * **DWR** ``<create>`` remote method exposure.

        Non-framework XML files (those whose file name and path do not
        match any known pattern) receive only a lightweight root node
        with ``type="xml_data"`` and no deep element parsing.
        """
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []

        is_framework = _is_framework_xml(file_path)
        root_type = "xml_config" if is_framework else "xml_data"

        root_id = generate_deterministic_id(file_path, root_type, file_path, 1)
        root_node = CPGNode(
            id=root_id,
            labels=("Node", "Module"),
            properties=MappingProxyType(
                {
                    "type": root_type,
                    "code": "",
                    "line_start": 1,
                    "line_end": source_code.count("\n") + 1,
                    "file_path": file_path,
                }
            ),
        )
        nodes.append(root_node)

        # Non-framework XML: return early with only the lightweight root.
        if not is_framework:
            logger.debug("Skipping deep XML parse (non-framework): %s", file_path)
            return nodes, edges

        try:
            root_el = ET.fromstring(source_code)
        except ET.ParseError:
            logger.warning("Failed to parse XML: %s", file_path)
            return nodes, edges

        self._walk_xml(root_el, file_path, nodes, edges, parent_id=root_id)
        logger.info("XML AST for %s: %d nodes, %d edges", file_path, len(nodes), len(edges))
        return nodes, edges

    # ── Java properties parsing ─────────────────────────────────────────

    def _build_properties(
        self, file_path: str, source_code: str
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Parse ``.properties`` files into lightweight configuration nodes."""
        nodes: list[CPGNode] = []
        edges: list[CPGEdge] = []

        root_id = generate_deterministic_id(file_path, "properties_config", file_path, 1)
        root_node = CPGNode(
            id=root_id,
            labels=("Node", "Module"),
            properties=MappingProxyType(
                {
                    "type": "properties_config",
                    "code": "",
                    "line_start": 1,
                    "line_end": source_code.count("\n") + 1,
                    "file_path": file_path,
                }
            ),
        )
        nodes.append(root_node)

        for line_number, raw_line in enumerate(source_code.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith(("#", "!")):
                continue
            separator_index = self._find_properties_separator(line)
            if separator_index is None:
                key = line
                value = ""
            else:
                key = line[:separator_index].strip()
                value = line[separator_index + 1 :].strip()
            if not key:
                continue

            node_id = generate_deterministic_id(
                file_path,
                "property_entry",
                key,
                line_number,
            )
            nodes.append(
                CPGNode(
                    id=node_id,
                    labels=("Node", "Config", "Property"),
                    properties=MappingProxyType(
                        {
                            "type": "property_entry",
                            "name": key,
                            "key": key,
                            "value": value,
                            "code": raw_line,
                            "line_start": line_number,
                            "line_end": line_number,
                            "file_path": file_path,
                        }
                    ),
                )
            )
            edges.append(
                CPGEdge(
                    source_id=root_id,
                    target_id=node_id,
                    edge_type=EdgeType.PARENT_OF,
                )
            )

        logger.info("Properties AST for %s: %d nodes, %d edges", file_path, len(nodes), len(edges))
        return nodes, edges

    @staticmethod
    def _find_properties_separator(line: str) -> int | None:
        """Return the first unescaped ``=`` or ``:`` separator in a properties line."""
        escaped = False
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char in {"=", ":"}:
                return index
        return None

    def _walk_xml(
        self,
        element: ET.Element,
        file_path: str,
        nodes: list[CPGNode],
        edges: list[CPGEdge],
        parent_id: str,
    ) -> None:
        """Recursively convert XML elements into CPG nodes.

        Only elements that represent framework configuration (beans, actions,
        entity mappings) are promoted to full CPG nodes; other elements are
        walked but not emitted.
        """
        tag = _strip_ns(element.tag)
        props = self._xml_element_props(element, tag, file_path)

        if props is not None:
            xml_name = str(props.get("name", props.get("code", tag)))
            xml_line = int(props.get("line_start", 0))
            node_id = generate_deterministic_id(file_path, tag, xml_name, xml_line)
            labels = self._xml_labels_resolved(tag, props)
            cpg_node = CPGNode(
                id=node_id,
                labels=labels,
                properties=MappingProxyType(props),
            )
            nodes.append(cpg_node)
            edges.append(
                CPGEdge(
                    source_id=parent_id,
                    target_id=node_id,
                    edge_type=EdgeType.PARENT_OF,
                )
            )
            child_parent = node_id
        else:
            child_parent = parent_id

        for child_el in element:
            self._walk_xml(child_el, file_path, nodes, edges, parent_id=child_parent)

    @staticmethod
    def _xml_element_props(element: ET.Element, tag: str, file_path: str) -> dict[str, Any] | None:
        """Extract CPG properties from a framework XML element.

        Returns ``None`` for elements that should not become CPG nodes.
        """
        attribs = dict(element.attrib)

        # ── Spring ───────────────────────────────────────────────────
        if tag == "bean":
            return {
                "type": "spring_bean",
                "code": f'<bean id="{attribs.get("id", "")}" class="{attribs.get("class", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "spring",
                "bean_id": attribs.get("id", ""),
                "bean_class": attribs.get("class", ""),
            }
        # Spring <property ref="..."> — dependency injection reference.
        if tag == "property" and "ref" in attribs:
            return {
                "type": "spring_injection",
                "code": (
                    f'<property name="{attribs.get("name", "")}" ref="{attribs.get("ref", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "spring",
                "property_name": attribs.get("name", ""),
                "ref_bean": attribs.get("ref", ""),
            }
        # Spring <constructor-arg ref="..."> — constructor injection.
        if tag == "constructor-arg" and "ref" in attribs:
            return {
                "type": "spring_injection",
                "code": f'<constructor-arg ref="{attribs.get("ref", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "spring",
                "ref_bean": attribs.get("ref", ""),
            }
        # Spring <import resource="..."> — config file import.
        if tag == "import" and "resource" in attribs:
            return {
                "type": "spring_import",
                "code": f'<import resource="{attribs.get("resource", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "spring",
                "resource": attribs.get("resource", ""),
            }
        # Spring AOP / TX markers — lightweight capture.
        if tag in {"annotation-driven", "component-scan"}:
            return {
                "type": f"spring_{tag.replace('-', '_')}",
                "code": f"<{tag}>",
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "spring",
                "base_package": attribs.get("base-package", ""),
            }

        # ── Struts 1.x ──────────────────────────────────────────────
        if tag == "action":
            return {
                "type": "struts_action",
                "code": (
                    f'<action path="{attribs.get("path", "")}" type="{attribs.get("type", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "struts",
                "action_path": attribs.get("path", ""),
                "action_type": attribs.get("type", ""),
                "action_name": attribs.get("name", ""),
            }
        if tag == "form-bean":
            return {
                "type": "struts_form_bean",
                "code": (
                    f'<form-bean name="{attribs.get("name", "")}"'
                    f' type="{attribs.get("type", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "struts",
                "form_name": attribs.get("name", ""),
                "form_type": attribs.get("type", ""),
            }
        # Struts <forward> — action navigation path.
        if tag == "forward":
            return {
                "type": "struts_forward",
                "code": (
                    f'<forward name="{attribs.get("name", "")}" path="{attribs.get("path", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "struts",
                "forward_name": attribs.get("name", ""),
                "forward_path": attribs.get("path", ""),
            }
        # Struts <message-resources> — i18n resource bundles.
        if tag == "message-resources":
            return {
                "type": "struts_message_resources",
                "code": f'<message-resources parameter="{attribs.get("parameter", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "struts",
                "parameter": attribs.get("parameter", ""),
            }

        # ── Hibernate ────────────────────────────────────────────────
        if tag == "class" and ("name" in attribs or "table" in attribs):
            return {
                "type": "hibernate_entity",
                "code": (
                    f'<class name="{attribs.get("name", "")}" table="{attribs.get("table", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "entity_class": attribs.get("name", ""),
                "table_name": attribs.get("table", ""),
            }
        # Hibernate <property> — column mapping (only when name+column present,
        # to avoid colliding with Spring <property ref>).
        if tag == "property" and ("name" in attribs and "column" in attribs):
            return {
                "type": "hibernate_property",
                "code": (
                    f'<property name="{attribs.get("name", "")}"'
                    f' column="{attribs.get("column", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "property_name": attribs.get("name", ""),
                "column_name": attribs.get("column", ""),
            }
        # Hibernate <id> — primary key mapping.
        if tag == "id":
            return {
                "type": "hibernate_id",
                "code": (
                    f'<id name="{attribs.get("name", "")}" column="{attribs.get("column", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "property_name": attribs.get("name", ""),
                "column_name": attribs.get("column", ""),
            }
        # Hibernate <composite-id> — composite primary key.
        if tag == "composite-id":
            return {
                "type": "hibernate_composite_id",
                "code": f'<composite-id class="{attribs.get("class", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "id_class": attribs.get("class", ""),
            }
        # Hibernate relationship mappings.
        if tag == "many-to-one":
            return {
                "type": "hibernate_many_to_one",
                "code": (
                    f'<many-to-one name="{attribs.get("name", "")}"'
                    f' class="{attribs.get("class", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "property_name": attribs.get("name", ""),
                "target_class": attribs.get("class", ""),
                "column_name": attribs.get("column", ""),
            }
        if tag == "one-to-many":
            return {
                "type": "hibernate_one_to_many",
                "code": f'<one-to-many class="{attribs.get("class", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "target_class": attribs.get("class", ""),
            }
        if tag in {"set", "bag", "map"}:
            name = attribs.get("name", "")
            tbl = attribs.get("table", "")
            return {
                "type": f"hibernate_{tag}",
                "code": f'<{tag} name="{name}" table="{tbl}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "hibernate",
                "property_name": name,
                "table_name": tbl,
            }

        # ── Acegi Security ───────────────────────────────────────────
        if tag == "filter-chain-map":
            return {
                "type": "acegi_filter_chain",
                "code": "<filter-chain-map>",
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "acegi",
            }
        if tag == "filter-chain":
            return {
                "type": "acegi_filter_chain_entry",
                "code": (
                    f'<filter-chain pattern="{attribs.get("pattern", "")}"'
                    f' filters="{attribs.get("filters", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "acegi",
                "url_pattern": attribs.get("pattern", ""),
                "filters": attribs.get("filters", ""),
            }
        if tag == "intercept-url":
            return {
                "type": "acegi_intercept_url",
                "code": (
                    f'<intercept-url pattern="{attribs.get("pattern", "")}"'
                    f' access="{attribs.get("access", "")}">'
                ),
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "acegi",
                "url_pattern": attribs.get("pattern", ""),
                "access": attribs.get("access", ""),
            }

        # ── DWR (Direct Web Remoting) ────────────────────────────────
        if tag == "create":
            creator = attribs.get("creator", "")
            javascript = attribs.get("javascript", "")
            return {
                "type": "dwr_create",
                "code": f'<create creator="{creator}" javascript="{javascript}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "dwr",
                "creator": creator,
                "javascript": javascript,
            }
        if tag == "include" and "method" in attribs:
            return {
                "type": "dwr_include",
                "code": f'<include method="{attribs.get("method", "")}">',
                "line_start": 1,
                "line_end": 1,
                "file_path": file_path,
                "framework": "dwr",
                "method_name": attribs.get("method", ""),
            }

        return None

    @staticmethod
    def _xml_labels(tag: str) -> tuple[str, ...]:
        """Derive CPG labels for a framework XML element.

        .. note::

            For ``<property>`` elements that can belong to either Spring
            (injection) or Hibernate (column mapping), the caller resolves
            the ambiguity in :meth:`_xml_element_props` by returning
            different ``type`` values.  This method only handles tags that
            are unambiguous.
        """
        base = ("Node",)
        # Spring
        if tag == "bean":
            return (*base, "SpringBean")
        if tag == "constructor-arg":
            return (*base, "SpringInjection")
        if tag == "import":
            return (*base, "SpringImport")
        if tag in {"annotation-driven", "component-scan"}:
            return (*base, "SpringConfig")
        # Struts
        if tag == "action":
            return (*base, "StrutsAction")
        if tag == "form-bean":
            return (*base, "StrutsFormBean")
        if tag == "forward":
            return (*base, "StrutsForward")
        if tag == "message-resources":
            return (*base, "StrutsMessageResources")
        # Hibernate
        if tag == "class":
            return (*base, "HibernateEntity")
        if tag in {"id", "composite-id"}:
            return (*base, "HibernateId")
        if tag in {"many-to-one", "one-to-many"}:
            return (*base, "HibernateRelation")
        if tag in {"set", "bag", "map"}:
            return (*base, "HibernateCollection")
        # Acegi / Security
        if tag in {"filter-chain-map", "filter-chain", "intercept-url"}:
            return (*base, "SecurityConfig")
        # DWR
        if tag == "create":
            return (*base, "DWRRemote")
        if tag == "include":
            return (*base, "DWRMethod")
        return base

    @staticmethod
    def _xml_labels_resolved(tag: str, props: dict[str, Any]) -> tuple[str, ...]:
        """Derive CPG labels using both the XML tag and the resolved props.

        This method disambiguates the ``<property>`` tag which can be
        either a Spring injection (``ref`` attribute) or a Hibernate column
        mapping (``column`` attribute).
        """
        base = ("Node",)
        # Ambiguous tags — use 'type' from resolved props.
        node_type = props.get("type", "")
        if node_type == "spring_injection":
            return (*base, "SpringInjection")
        if node_type == "hibernate_property":
            return (*base, "HibernateProperty")
        # Fall through to tag-based resolution.
        return ASTBuilder._xml_labels(tag)

    # ── Tree-sitter walk ─────────────────────────────────────────────────

    def _walk(
        self,
        ts_node: Node,
        file_path: str,
        nodes: list[CPGNode],
        edges: list[CPGEdge],
        parent_id: str | None,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
        package: str = "",
        scope_fqn: str = "",
    ) -> str:
        """Recursively convert a tree-sitter node into CPGNode + edges.

        The recursion depth is controlled by *analysis_level*:

        * ``FULL`` — emit every named child (original behaviour).
        * ``ARCHITECTURAL`` — stop at Method/Field level; store body as
          ``source_code`` property on Method nodes.
        * ``STRUCTURAL`` — keep statement-level nodes but prune
          expression and literal children.

        *package* / *scope_fqn* thread the enclosing package and fully-qualified
        scope name down the tree so ``Class`` / ``Method`` nodes can be
        annotated with ``package`` / ``fqn`` / ``signature``.

        Returns:
            The CPGNode ``id`` created for *ts_node*.
        """
        props = self._extract_properties(ts_node, file_path)
        labels = self._compute_labels(ts_node, props)

        # ── FQN / structured-field enrichment (Class / Method) ────────────
        child_scope = scope_fqn
        if ts_node.type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }:
            simple_name = str(props.get("name", ""))
            if simple_name:
                base = scope_fqn or package
                class_fqn = f"{base}.{simple_name}" if base else simple_name
                props["package"] = package
                props["fqn"] = class_fqn
                child_scope = class_fqn
            props["modifiers"] = list(_extract_java_modifiers(ts_node))
        elif ts_node.type in {"method_declaration", "constructor_declaration"}:
            method_name = str(props.get("name", ""))
            props["package"] = package
            if method_name:
                props["fqn"] = f"{scope_fqn}.{method_name}" if scope_fqn else method_name
            param_types = _extract_java_param_types(ts_node)
            props["param_types"] = list(param_types)
            props["modifiers"] = list(_extract_java_modifiers(ts_node))
            props["mccabe"] = props.get("complexity", _compute_java_mccabe_complexity(ts_node))
            # Structured REST routing metadata.
            http_method, route = _extract_java_route(ts_node)
            if http_method:
                props["http_method"] = http_method
            if route:
                props["route"] = route
            # Structured @Transactional propagation metadata.
            tx_propagation = _extract_java_tx_propagation(ts_node)
            if tx_propagation:
                props["tx_propagation"] = tx_propagation

        # Build a deterministic ID from stable attributes.
        # For named entities (class/method) use the name; for unnamed nodes
        # fall back to the code snippet for extra disambiguation.
        node_name = str(props.get("name") or props.get("code", "")[:80])
        line_start = int(props.get("line_start", 0))
        col_start = ts_node.start_point[1]
        node_id = generate_deterministic_id(
            file_path,
            ts_node.type,
            node_name,
            line_start,
            col_start,
        )

        # In ARCHITECTURAL mode, attach source_code to Method nodes.
        if analysis_level == AnalysisLevel.ARCHITECTURAL and ts_node.type in {
            "method_declaration",
            "constructor_declaration",
        }:
            body_code = ts_node.text.decode("utf-8") if ts_node.text is not None else ""
            props["source_code"] = body_code

        cpg_node = CPGNode(
            id=node_id,
            labels=labels,
            properties=MappingProxyType(props),
        )
        nodes.append(cpg_node)

        # Choose edge type based on analysis level.
        edge_type = (
            EdgeType.CONTAINS
            if analysis_level == AnalysisLevel.ARCHITECTURAL
            else EdgeType.PARENT_OF
        )

        if parent_id is not None:
            edges.append(
                CPGEdge(
                    source_id=parent_id,
                    target_id=node_id,
                    edge_type=edge_type,
                )
            )

        # Decide whether and how to recurse into children.
        if analysis_level == AnalysisLevel.ARCHITECTURAL:
            self._walk_architectural_children(
                ts_node,
                file_path,
                nodes,
                edges,
                parent_id=node_id,
                package=package,
                scope_fqn=child_scope,
            )
        elif analysis_level == AnalysisLevel.STRUCTURAL:
            # Recurse into structural-level children only.
            for child in ts_node.children:
                if child.is_named and child.type in _STRUCTURAL_TYPES:
                    self._walk(
                        child,
                        file_path,
                        nodes,
                        edges,
                        parent_id=node_id,
                        analysis_level=analysis_level,
                        package=package,
                        scope_fqn=child_scope,
                    )
        else:
            # FULL mode — recurse into all named children.
            for child in ts_node.children:
                if child.is_named:
                    self._walk(
                        child,
                        file_path,
                        nodes,
                        edges,
                        parent_id=node_id,
                        analysis_level=analysis_level,
                        package=package,
                        scope_fqn=child_scope,
                    )

        return node_id

    def _walk_architectural_children(
        self,
        ts_node: Node,
        file_path: str,
        nodes: list[CPGNode],
        edges: list[CPGEdge],
        parent_id: str,
        package: str,
        scope_fqn: str,
    ) -> None:
        """Traverse ARCHITECTURAL children, treating noisy containers as transparent."""
        retained_types = _SKELETON_TYPES | _CALL_GRAPH_TYPES | _TYPE_CONTEXT_TYPES
        for child in ts_node.children:
            if not child.is_named:
                continue
            if child.type in retained_types:
                self._walk(
                    child,
                    file_path,
                    nodes,
                    edges,
                    parent_id=parent_id,
                    analysis_level=AnalysisLevel.ARCHITECTURAL,
                    package=package,
                    scope_fqn=scope_fqn,
                )
            else:
                self._walk_architectural_children(
                    child,
                    file_path,
                    nodes,
                    edges,
                    parent_id=parent_id,
                    package=package,
                    scope_fqn=scope_fqn,
                )

    @staticmethod
    def _extract_properties(ts_node: Node, file_path: str) -> dict[str, Any]:
        """Build the ``properties`` dict for a single tree-sitter node."""
        props: dict[str, Any] = {
            "type": ts_node.type,
            "code": (ts_node.text.decode("utf-8") if ts_node.text is not None else ""),
            "line_start": ts_node.start_point[0] + 1,  # 1-indexed
            "line_end": ts_node.end_point[0] + 1,
            "file_path": file_path,
        }

        # Extract identifier name for well-known compound nodes.
        if ts_node.type in _NAMED_NODE_TYPES:
            name_child = ts_node.child_by_field_name("name")
            if name_child is not None and name_child.text is not None:
                props["name"] = name_child.text.decode("utf-8")

        # Extract method name + receiver for method_invocation nodes using
        # tree-sitter fields (robust against chained / generic / nested calls).
        if ts_node.type == "method_invocation":
            name_node = ts_node.child_by_field_name("name")
            if name_node is not None and name_node.text is not None:
                method_name = name_node.text.decode("utf-8")
                if method_name.isidentifier():
                    props["name"] = method_name
            else:
                # Fallback to string parsing for unusual grammars.
                code = props.get("code", "")
                if "(" in code:
                    prefix = code.split("(", maxsplit=1)[0].strip()
                    method_name = prefix.rsplit(".", maxsplit=1)[-1] if "." in prefix else prefix
                    if method_name.isidentifier():
                        props["name"] = method_name
            obj_node = ts_node.child_by_field_name("object")
            if obj_node is not None and obj_node.text is not None:
                props["receiver"] = obj_node.text.decode("utf-8")

        # Extract object creation type for ``new Foo(...)`` call sites.
        if ts_node.type == "object_creation_expression":
            type_node = ts_node.child_by_field_name("type")
            if type_node is not None and type_node.text is not None:
                props["name"] = type_node.text.decode("utf-8")

        if ts_node.type in {"method_invocation", "object_creation_expression"}:
            args_node = ts_node.child_by_field_name("arguments")
            if args_node is not None:
                arg_children = [c for c in args_node.children if c.is_named]
                props["arg_count"] = len(arg_children)
                # Raw argument expression texts, used downstream for type-based
                # overload disambiguation.  Capped to keep node payload small.
                exprs: list[str] = []
                for c in arg_children:
                    if c.text is not None:
                        exprs.append(c.text.decode("utf-8")[:_MAX_ARG_EXPR_LENGTH])
                if exprs:
                    props["arg_exprs"] = tuple(exprs)

        # Tag well-known taint sources / sinks / sanitizers on call sites so
        # downstream analysers can locate candidate flow endpoints quickly.
        # This is additive metadata only; it does not alter REACHES semantics.
        if ts_node.type in {"method_invocation", "object_creation_expression"}:
            match_name = props.get("name")
            if ts_node.type == "object_creation_expression" and match_name:
                # ``new java.io.File<...>(...)`` → simple type name ``File``.
                match_name = match_name.split("<", maxsplit=1)[0].rsplit(".", maxsplit=1)[-1]
            rule = classify_invocation(match_name, props.get("receiver"))
            if rule is not None:
                props["security_role"] = rule.role
                props["security_category"] = rule.category

        # Extract method references (``Type::method`` / ``obj::method`` /
        # ``this::method``).  These behave like deferred call sites: the
        # referenced method may be invoked through the functional interface,
        # so we expose ``name`` + ``receiver`` for call-graph resolution.
        # Constructor references (``Type::new``) are left unresolved.
        if ts_node.type == "method_reference":
            named = [c for c in ts_node.children if c.is_named]
            if named:
                last = named[-1]
                if last.type == "identifier" and last.text is not None:
                    method_name = last.text.decode("utf-8")
                    if method_name.isidentifier():
                        props["name"] = method_name
                    if len(named) >= 2 and named[0].text is not None:
                        props["receiver"] = named[0].text.decode("utf-8")

        # Extract declared variable names + type for local declarations.
        if ts_node.type == "local_variable_declaration":
            var_names = _extract_java_declarator_names(ts_node)
            if var_names:
                props["var_names"] = var_names
            declared_type = _extract_java_declared_type(ts_node)
            if declared_type:
                props["declared_type"] = declared_type

        # Extract the loop variable name + element type for enhanced ``for``
        # statements (``for (Foo x : coll) { x.bar(); }``).  The loop variable
        # is declared on the statement node itself rather than in a nested
        # ``local_variable_declaration``, so it must be captured here to make
        # ``x``'s type available for receiver-class resolution.
        if ts_node.type == "enhanced_for_statement":
            name_node = ts_node.child_by_field_name("name")
            type_node = ts_node.child_by_field_name("type")
            if name_node is not None and name_node.text is not None:
                props["var_names"] = (name_node.text.decode("utf-8"),)
            if type_node is not None and type_node.text is not None:
                props["declared_type"] = type_node.text.decode("utf-8")

        # Extract the caught-exception variable name + type for catch clauses
        # (``catch (SQLException e) { e.getMessage(); }``).  Exception types are
        # almost always outside the analysis scope, so capturing the type lets
        # receiver resolution suppress name-only false edges to project methods.
        if ts_node.type == "catch_formal_parameter":
            name_node = ts_node.child_by_field_name("name")
            catch_type = next((c for c in ts_node.children if c.type == "catch_type"), None)
            if name_node is not None and name_node.text is not None:
                props["var_names"] = (name_node.text.decode("utf-8"),)
            if catch_type is not None:
                first_type = next(
                    (c for c in catch_type.children if c.is_named and c.text is not None),
                    None,
                )
                if first_type is not None and first_type.text is not None:
                    props["declared_type"] = first_type.text.decode("utf-8")

        # Extract assignment target + whether it writes a field vs. a variable.
        if ts_node.type == "assignment_expression":
            target, is_field = _extract_java_assignment_target(ts_node)
            if target:
                props["assign_target"] = target
                if is_field:
                    props["assign_kind"] = "field"

        # Extract declared field names + type for field declarations.
        if ts_node.type == "field_declaration":
            var_names = _extract_java_declarator_names(ts_node)
            if var_names:
                props["var_names"] = var_names
            declared_type = _extract_java_declared_type(ts_node)
            if declared_type:
                props["declared_type"] = declared_type

        # Extract framework annotations from modifier nodes.
        if ts_node.type in {
            "class_declaration",
            "interface_declaration",
            "record_declaration",
            "method_declaration",
        }:
            annotations = _extract_annotations(ts_node)
            if annotations:
                props["annotations"] = tuple(annotations)
                framework = _detect_framework(annotations)
                if framework:
                    props["framework"] = framework

        # Detect Struts Action subclass via superclass.
        if ts_node.type == "class_declaration":
            superclass = _extract_superclass(ts_node)
            if superclass:
                props["superclass"] = superclass
                if superclass in _STRUTS_BASE_CLASSES:
                    props["framework"] = "struts"

        # ── Enriched properties for Method / Class nodes ──────────────
        if ts_node.type in {"method_declaration", "constructor_declaration"}:
            _enrich_java_method(ts_node, props)
        elif ts_node.type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        }:
            _enrich_java_class(ts_node, props)

        # ── Business module + architecture layer (Class-level only) ───
        if ts_node.type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
        }:
            biz_module = _detect_business_module(file_path)
            if biz_module:
                props["business_module"] = biz_module
            layer = _detect_architecture_layer(props)
            if layer:
                props["architecture_layer"] = layer

        return props

    @staticmethod
    def _compute_labels(ts_node: Node, props: dict[str, Any]) -> tuple[str, ...]:
        """Derive node labels from the tree-sitter node type."""
        base = ("Node",)
        ts_type = ts_node.type

        if ts_type == "class_declaration":
            extra: tuple[str, ...] = ("Class",)
            framework = props.get("framework")
            if framework == "spring":
                extra = (*extra, "SpringComponent")
            elif framework == "hibernate":
                extra = (*extra, "HibernateEntity")
            elif framework == "struts":
                extra = (*extra, "StrutsAction")
            return (*base, *extra)
        if ts_type == "interface_declaration":
            return (*base, "Interface")
        if ts_type == "enum_declaration":
            return (*base, "Enum")
        if ts_type == "record_declaration":
            return (*base, "Class", "Record")
        if ts_type in {"method_declaration", "constructor_declaration"}:
            method_extra: tuple[str, ...] = ("Method",)
            annotations = props.get("annotations", ())
            mapping_annotations = {
                "RequestMapping",
                "GetMapping",
                "PostMapping",
                "PutMapping",
                "DeleteMapping",
            }
            if mapping_annotations & set(annotations):
                method_extra = (*method_extra, "RequestHandler")
            return (*base, *method_extra)
        if ts_type in {"identifier", "field_access"}:
            return (*base, "Variable")
        if ts_type in {"formal_parameter", "spread_parameter"}:
            return (*base, "Parameter")
        if ts_type in {"method_invocation", "object_creation_expression", "method_reference"}:
            return (*base, "CallSite")
        if ts_type == "return_statement":
            return (*base, "Return")
        if ts_type == "program":
            return (*base, "Module")
        if ts_type == "field_declaration":
            return (*base, "Field")
        if ts_type == "package_declaration":
            return (*base, "Package")
        if ts_type in {"import_declaration"}:
            return (*base, "Import")
        if ts_type == "annotation_type_declaration":
            return (*base, "Annotation")
        if ts_type in {"marker_annotation", "annotation"}:
            return (*base, "AnnotationUsage")
        return base


# ── Module-level helpers ─────────────────────────────────────────────────


def _extract_java_package(root: Node) -> str:
    """Return the package name from a ``program`` root, or ``""`` if default."""
    for child in root.children:
        if child.type == "package_declaration":
            for grandchild in child.children:
                if grandchild.is_named and grandchild.text is not None:
                    return grandchild.text.decode("utf-8")
    return ""


def _extract_java_modifiers(ts_node: Node) -> tuple[str, ...]:
    """Return declared modifier keywords (``public``/``static``/...), excluding annotations."""
    modifiers: list[str] = []
    for child in ts_node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type in {"marker_annotation", "annotation"}:
                    continue
                if mod.is_named is False and mod.text is not None:
                    modifiers.append(mod.text.decode("utf-8"))
            break
    return tuple(modifiers)


def _extract_java_param_types(ts_node: Node) -> tuple[str, ...]:
    """Return declared parameter types (preserving generics) for a method/constructor."""
    params_node = ts_node.child_by_field_name("parameters")
    if params_node is None:
        return ()
    types: list[str] = []
    for child in params_node.children:
        if child.type in {"formal_parameter", "spread_parameter"}:
            type_node = child.child_by_field_name("type")
            if type_node is not None and type_node.text is not None:
                type_text = type_node.text.decode("utf-8")
                if child.type == "spread_parameter":
                    type_text += "..."
                types.append(type_text)
    return tuple(types)


# Spring mapping annotation → HTTP verb.
_MAPPING_HTTP_METHODS: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def _extract_java_route(ts_node: Node) -> tuple[str | None, str | None]:
    """Return ``(http_method, route)`` for a Spring request-handler method.

    Reads ``@GetMapping`` / ``@PostMapping`` / ``@RequestMapping(...)`` arguments
    from the method modifiers.  Returns ``(None, None)`` when no mapping
    annotation is present.
    """
    modifiers = None
    for child in ts_node.children:
        if child.type == "modifiers":
            modifiers = child
            break
    if modifiers is None:
        return None, None

    for ann in modifiers.children:
        if ann.type not in {"annotation", "marker_annotation"}:
            continue
        name_node = ann.child_by_field_name("name")
        if name_node is None or name_node.text is None:
            continue
        ann_name = name_node.text.decode("utf-8")
        if ann_name in _MAPPING_HTTP_METHODS:
            return _MAPPING_HTTP_METHODS[ann_name], _extract_annotation_route(ann)
        if ann_name == "RequestMapping":
            return _extract_annotation_http_method(ann), _extract_annotation_route(ann)
    return None, None


def _extract_annotation_route(ann: Node) -> str | None:
    """Extract the URL route (``value`` / ``path`` attribute or positional) from an annotation."""
    args = ann.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        if arg.type == "string_literal":
            return _string_literal_text(arg)
        if arg.type == "element_value_pair":
            key = arg.child_by_field_name("key")
            value = arg.child_by_field_name("value")
            if key is not None and key.text is not None:
                key_text = key.text.decode("utf-8")
                if key_text in {"value", "path"} and value is not None:
                    return _string_literal_text(value)
    return None


def _extract_annotation_http_method(ann: Node) -> str | None:
    """Extract the HTTP verb from a ``@RequestMapping(method=RequestMethod.X)`` annotation."""
    args = ann.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.children:
        if arg.type != "element_value_pair":
            continue
        key = arg.child_by_field_name("key")
        value = arg.child_by_field_name("value")
        if key is None or key.text is None or value is None or value.text is None:
            continue
        if key.text.decode("utf-8") == "method":
            verb = value.text.decode("utf-8")
            return verb.rsplit(".", maxsplit=1)[-1]  # RequestMethod.POST → POST
    return None


def _string_literal_text(node: Node) -> str | None:
    """Return the inner text of a ``string_literal`` node (without quotes)."""
    if node.type == "string_literal":
        for child in node.children:
            if child.type == "string_fragment" and child.text is not None:
                return child.text.decode("utf-8")
        # Empty string literal "".
        if node.text is not None:
            return node.text.decode("utf-8").strip('"')
    if node.text is not None:
        return node.text.decode("utf-8")
    return None


def _extract_java_tx_propagation(ts_node: Node) -> str | None:
    """Return the ``@Transactional`` propagation level for a method, if declared.

    Reads ``@Transactional(propagation = Propagation.X)``; returns ``"DEFAULT"``
    for a bare ``@Transactional`` annotation and ``None`` when absent.
    """
    modifiers = None
    for child in ts_node.children:
        if child.type == "modifiers":
            modifiers = child
            break
    if modifiers is None:
        return None

    for ann in modifiers.children:
        if ann.type not in {"annotation", "marker_annotation"}:
            continue
        name_node = ann.child_by_field_name("name")
        if name_node is None or name_node.text is None:
            continue
        if name_node.text.decode("utf-8") != "Transactional":
            continue
        args = ann.child_by_field_name("arguments")
        if args is None:
            return "DEFAULT"
        for arg in args.children:
            if arg.type != "element_value_pair":
                continue
            key = arg.child_by_field_name("key")
            value = arg.child_by_field_name("value")
            if key is None or key.text is None or value is None or value.text is None:
                continue
            if key.text.decode("utf-8") == "propagation":
                return value.text.decode("utf-8").rsplit(".", maxsplit=1)[-1]
        return "DEFAULT"
    return None


def _extract_annotations(ts_node: Node) -> list[str]:
    """Extract annotation names from a class/method declaration's modifiers."""
    annotations: list[str] = []
    modifiers = ts_node.child_by_field_name("modifiers") if ts_node.child_count > 0 else None
    if modifiers is None:
        # Try finding modifiers as a direct child.
        for child in ts_node.children:
            if child.type == "modifiers":
                modifiers = child
                break
    if modifiers is None:
        return annotations

    for child in modifiers.children:
        if child.type in {"marker_annotation", "annotation"}:
            name_node = child.child_by_field_name("name")
            if name_node is None:
                # Marker annotations have the name as first named child.
                for grandchild in child.children:
                    if grandchild.is_named and grandchild.type == "identifier":
                        name_node = grandchild
                        break
            if name_node is not None and name_node.text is not None:
                annotations.append(name_node.text.decode("utf-8"))
    return annotations


def _extract_superclass(ts_node: Node) -> str | None:
    """Extract the superclass name from a ``class_declaration`` node."""
    for child in ts_node.children:
        if child.type == "superclass":
            for grandchild in child.children:
                if grandchild.is_named and grandchild.text is not None:
                    return grandchild.text.decode("utf-8")
    return None


def _detect_framework(annotations: list[str]) -> str | None:
    """Detect framework from a list of annotation names."""
    ann_set = frozenset(annotations)
    if ann_set & _SPRING_ANNOTATIONS:
        return "spring"
    if ann_set & _HIBERNATE_ANNOTATIONS:
        return "hibernate"
    return None


def _strip_ns(tag: str) -> str:
    """Strip XML namespace prefix from an element tag.

    E.g. ``'{http://example.com}bean'`` → ``'bean'``.
    """
    if tag.startswith("{"):
        return tag.split("}", maxsplit=1)[-1]
    return tag


# ── Macro-skeleton helpers ───────────────────────────────────────────────


def _build_java_skeleton(
    ast_nodes: list[CPGNode],
    file_path: str,
) -> tuple[list[CPGNode], list[CPGEdge]]:
    """Build the macro-skeleton overlay for one Java file.

    Creates:

    * A ``:File`` node.
    * ``CONTAINS`` edges (``File → Class → Method``).
    * ``DEPENDS_ON`` edges for ``import`` declarations.

    Returns:
        ``(extra_nodes, extra_edges)``
    """
    extra_nodes: list[CPGNode] = []
    extra_edges: list[CPGEdge] = []

    file_name = file_path.rsplit("/", maxsplit=1)[-1]
    file_node_id = generate_deterministic_id(file_path, "file", file_name, 1)
    file_node = CPGNode(
        id=file_node_id,
        labels=("Node", "File"),
        properties=MappingProxyType(
            {
                "type": "file",
                "name": file_path.rsplit("/", maxsplit=1)[-1],
                "file_path": file_path,
                "line_start": 1,
                "line_end": 0,
                "code": "",
                "layer": _detect_java_layer(file_path),
            }
        ),
    )
    extra_nodes.append(file_node)

    class_nodes = [n for n in ast_nodes if n.has_label("Class") or n.has_label("Interface")]
    method_nodes = [n for n in ast_nodes if n.has_label("Method")]

    for cls in class_nodes:
        extra_edges.append(
            CPGEdge(source_id=file_node_id, target_id=cls.id, edge_type=EdgeType.CONTAINS)
        )

    claimed: set[str] = set()
    for cls in class_nodes:
        cls_start = int(cls.properties.get("line_start", 0))
        cls_end = int(cls.properties.get("line_end", 0))
        for method in method_nodes:
            m_start = int(method.properties.get("line_start", 0))
            m_end = int(method.properties.get("line_end", 0))
            if cls_start <= m_start and m_end <= cls_end:
                extra_edges.append(
                    CPGEdge(source_id=cls.id, target_id=method.id, edge_type=EdgeType.CONTAINS)
                )
                claimed.add(method.id)

    for method in method_nodes:
        if method.id not in claimed:
            extra_edges.append(
                CPGEdge(source_id=file_node_id, target_id=method.id, edge_type=EdgeType.CONTAINS)
            )

    # DEPENDS_ON for imports.
    for node in ast_nodes:
        if node.has_label("Import"):
            code = str(node.properties.get("code", ""))
            module_name = _extract_java_import(code)
            if module_name:
                extra_edges.append(
                    CPGEdge(
                        source_id=file_node_id,
                        target_id=file_node_id,
                        edge_type=EdgeType.DEPENDS_ON,
                        properties=MappingProxyType({"module": module_name}),
                    )
                )

    # ── IMPLEMENTS edges: Class → superclass / interface ──────────────
    # FQN-aware resolution: a base type name is resolved by (1) exact FQN match,
    # (2) FQN suffix match (qualified names / inner classes), then (3) simple
    # name.  When a simple name is ambiguous (several classes share it, e.g. an
    # inner class and a top-level class), the candidate sharing the longest FQN
    # prefix with the referencing class is preferred so inner/duplicate classes
    # resolve to the nearest enclosing scope instead of an arbitrary last match.
    fqn_index: dict[str, str] = {}
    simple_index: dict[str, list[str]] = {}
    for cls in class_nodes:
        cfqn = str(cls.properties.get("fqn", ""))
        cname = str(cls.properties.get("name", ""))
        if cfqn:
            fqn_index[cfqn] = cls.id
        if cname:
            simple_index.setdefault(cname, []).append(cls.id)

    def _resolve_base(base_name: str, referrer: CPGNode) -> str | None:
        name = base_name.strip()
        if not name:
            return None
        # 1. exact FQN.
        if name in fqn_index:
            return fqn_index[name]
        # 2. qualified name → match by FQN suffix.
        if "." in name:
            matches = [cid for fqn, cid in fqn_index.items() if fqn.endswith("." + name)]
            if len(matches) == 1:
                return matches[0]
            simple = name.rsplit(".", 1)[-1]
        else:
            simple = name
        candidates = simple_index.get(simple, [])
        candidates = [cid for cid in candidates if cid != referrer.id]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # 3. ambiguous simple name → prefer the nearest enclosing scope by
        # longest shared FQN prefix with the referencing class.
        referrer_fqn = str(referrer.properties.get("fqn", ""))
        node_by_id = {c.id: c for c in class_nodes}

        def shared_prefix_len(cid: str) -> int:
            cand_fqn = str(node_by_id[cid].properties.get("fqn", ""))
            ref_parts = referrer_fqn.split(".")
            cand_parts = cand_fqn.split(".")
            shared = 0
            for a, b in zip(ref_parts, cand_parts, strict=False):
                if a != b:
                    break
                shared += 1
            return shared

        best = max(candidates, key=shared_prefix_len)
        # Only accept if the best is unambiguously closest (avoid arbitrary pick).
        best_score = shared_prefix_len(best)
        if sum(1 for cid in candidates if shared_prefix_len(cid) == best_score) == 1:
            return best
        return None

    for cls in class_nodes:
        # ``superclass`` is set by _extract_superclass() during property extraction.
        superclass = str(cls.properties.get("superclass", ""))
        if superclass:
            base_id = _resolve_base(superclass, cls)
            if base_id is not None and base_id != cls.id:
                extra_edges.append(
                    CPGEdge(
                        source_id=cls.id,
                        target_id=base_id,
                        edge_type=EdgeType.IMPLEMENTS,
                        properties=MappingProxyType({"base_class": superclass}),
                    )
                )
        # Java ``implements`` interfaces are in ``interfaces`` field of the class node.
        base_classes = cls.properties.get("base_classes", ())
        if base_classes:
            for base_name in base_classes:
                base_id = _resolve_base(str(base_name), cls)
                if base_id is not None and base_id != cls.id:
                    extra_edges.append(
                        CPGEdge(
                            source_id=cls.id,
                            target_id=base_id,
                            edge_type=EdgeType.IMPLEMENTS,
                            properties=MappingProxyType({"base_class": str(base_name)}),
                        )
                    )

    return extra_nodes, extra_edges


def _enrich_java_method(ts_node: Node, props: dict[str, Any]) -> None:
    """Add structured metadata to a Java method/constructor node.

    Properties added:

    * ``signature`` — everything before the opening brace.
    * ``docstring`` — Javadoc from preceding block_comment.
    * ``source_code`` — full method text.
    * ``param_names`` — tuple of parameter names.
    * ``return_type`` — return-type text (if present).
    * ``is_async`` — always ``False`` for Java (no native async).
    * ``complexity`` — McCabe cyclomatic complexity (branch count + 1).
    """
    if ts_node.text is not None:
        full_text = ts_node.text.decode("utf-8")
        props["source_code"] = full_text
        # Signature: everything before the opening brace.
        brace_idx = full_text.find("{")
        if brace_idx != -1:
            props["signature"] = full_text[:brace_idx].strip()

    # Javadoc: look for a block_comment immediately preceding the method
    # within its parent's children.
    if ts_node.parent is not None:
        idx = _child_index(ts_node)
        if idx is not None and idx > 0:
            prev = ts_node.parent.children[idx - 1]
            if prev.type == "block_comment" and prev.text is not None:
                raw = prev.text.decode("utf-8")
                props["docstring"] = _strip_javadoc(raw)

    # ── Structured properties ────────────────────────────────────────
    props["param_names"] = _extract_java_param_names(ts_node)
    props["return_type"] = _extract_java_return_type(ts_node)
    props["is_async"] = False  # Java has no native async/await
    props["complexity"] = _compute_java_mccabe_complexity(ts_node)


def _enrich_java_class(ts_node: Node, props: dict[str, Any]) -> None:
    """Add ``docstring``, ``source_code``, and ``base_classes`` to a class node."""
    if ts_node.text is not None:
        props["source_code"] = ts_node.text.decode("utf-8")

    if ts_node.parent is not None:
        idx = _child_index(ts_node)
        if idx is not None and idx > 0:
            prev = ts_node.parent.children[idx - 1]
            if prev.type == "block_comment" and prev.text is not None:
                raw = prev.text.decode("utf-8")
                props["docstring"] = _strip_javadoc(raw)

    # ── Base classes / interfaces ────────────────────────────────────
    props["base_classes"] = _extract_java_interfaces(ts_node)


def _child_index(ts_node: Node) -> int | None:
    """Return the index of *ts_node* among its parent's children."""
    if ts_node.parent is None:
        return None
    for i, child in enumerate(ts_node.parent.children):
        if child.id == ts_node.id:
            return i
    return None  # pragma: no cover


def _strip_javadoc(raw: str) -> str:
    """Strip ``/** … */`` delimiters and leading ``*`` from a Javadoc comment."""
    text = raw.strip()
    if text.startswith("/**"):
        text = text[3:]
    elif text.startswith("/*"):
        text = text[2:]
    if text.endswith("*/"):
        text = text[:-2]
    # Remove leading asterisks on each line.
    lines = text.split("\n")
    cleaned = [line.lstrip().lstrip("*").lstrip() for line in lines]
    return "\n".join(cleaned).strip()


def _extract_java_import(code: str) -> str | None:
    """Extract the fully-qualified class name from a Java import statement.

    Handles both normal (``import java.util.List;``) and static
    (``import static java.util.List;``) forms by always returning the
    last whitespace-delimited token.
    """
    code = code.strip().rstrip(";")
    if code.startswith("import "):
        parts = code.split()
        if len(parts) >= 2:
            return parts[-1]
    return None


# ── XML classification helpers ───────────────────────────────────────────


def _is_framework_xml(file_path: str) -> bool:
    """Return ``True`` if the XML file is a recognised framework config.

    Recognition rules (any match is sufficient):

    1. The file name matches a known framework pattern (e.g.
       ``struts-config.xml``, ``*.hbm.xml``).
    2. The file resides under a framework config directory (e.g.
       ``WEB-INF/``, ``config/``, ``resources/``).
    """
    if not file_path:
        return False
    name = PurePosixPath(file_path).name
    for pat in _FRAMEWORK_XML_PATTERNS:
        if pat.search(name):
            return True
    # Check path segments.
    parts = set(PurePosixPath(file_path).parts)
    return bool(parts & _FRAMEWORK_XML_PATH_SEGMENTS)


# ── JSP Struts tag extraction ────────────────────────────────────────────


def _extract_jsp_struts_refs(source_code: str) -> list[str]:
    """Extract Struts action paths from JSP HTML tags.

    Recognises ``<html:form action="...">`` and
    ``<html:link page="...">`` patterns.
    """
    if not source_code:
        return []
    refs: list[str] = []
    for m in _JSP_STRUTS_FORM_RE.finditer(source_code):
        refs.append(m.group(1))
    for m in _JSP_STRUTS_LINK_RE.finditer(source_code):
        refs.append(m.group(1))
    return refs


# ── Business module detection ────────────────────────────────────────────


def _detect_business_module(file_path: str) -> str | None:
    """Infer the business module from the file path.

    Scans path segments for well-known module names (e.g.
    ``claim``, ``undwrt``, ``payment``) and returns the first match.
    """
    if not file_path:
        return None
    parts = PurePosixPath(file_path).parts
    for part in parts:
        lower = part.lower()
        if lower in _BUSINESS_MODULE_MAP:
            return _BUSINESS_MODULE_MAP[lower]
    return None


# ── Architecture layer detection ─────────────────────────────────────────


def _detect_architecture_layer(props: dict[str, Any]) -> str | None:
    """Infer the architecture layer from class annotations / superclass.

    Returns one of ``"presentation"``, ``"service"``, ``"persistence"``,
    ``"domain"``, or ``None`` if not determinable.
    """
    annotations = frozenset(props.get("annotations", ()))
    superclass = props.get("superclass", "")

    if annotations & _PRESENTATION_ANNOTATIONS or superclass in _PRESENTATION_SUPERCLASSES:
        return "presentation"
    if annotations & _SERVICE_ANNOTATIONS:
        return "service"
    if annotations & _PERSISTENCE_ANNOTATIONS or superclass in _PERSISTENCE_SUPERCLASSES:
        return "persistence"
    if annotations & _DOMAIN_ANNOTATIONS:
        return "domain"
    return None


# ── Structured-property helpers (Java methods) ───────────────────────────

# Tree-sitter node types that increment McCabe cyclomatic complexity in Java.
_JAVA_BRANCH_TYPES = frozenset(
    {
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "catch_clause",
        "switch_expression",
        "ternary_expression",
        "binary_expression",  # will filter for ``&&`` / ``||`` only
    }
)


def _extract_java_declarator_names(ts_node: Node) -> tuple[str, ...]:
    """Extract declared names from a declaration node.

    Supports multi-declaration statements such as ``int a, b, c;`` by reading
    every ``variable_declarator`` child's ``name`` field.
    """
    names: list[str] = []
    for child in ts_node.children:
        if child.type == "variable_declarator":
            name_child = child.child_by_field_name("name")
            if name_child is not None and name_child.text is not None:
                names.append(name_child.text.decode("utf-8"))
    return tuple(names)


def _extract_java_declared_type(ts_node: Node) -> str | None:
    """Extract the declared type text (preserving generics) from a declaration.

    When the declared type is the ``var`` keyword (Java 10+ local variable type
    inference), the initializer is inspected so the variable's effective type
    can still feed type-aware call resolution.  Supported initializer forms:

    * ``var x = new Foo(...)``     → ``Foo``
    * ``var x = Foo.create(...)``  → unknown (left as ``var``)
    * ``var x = (Foo) y``          → ``Foo``
    """
    type_node = ts_node.child_by_field_name("type")
    if type_node is None or type_node.text is None:
        return None
    declared = type_node.text.decode("utf-8")
    if declared != "var":
        return declared
    inferred = _infer_var_initializer_type(ts_node)
    return inferred or declared


def _infer_var_initializer_type(ts_node: Node) -> str | None:
    """Infer the type of a ``var`` local from its declarator initializer."""
    for child in ts_node.children:
        if child.type != "variable_declarator":
            continue
        value = child.child_by_field_name("value")
        if value is None:
            continue
        if value.type in {"object_creation_expression", "cast_expression"}:
            type_node = value.child_by_field_name("type")
            if type_node is not None and type_node.text is not None:
                return type_node.text.decode("utf-8")
    return None


def _extract_java_assignment_target(ts_node: Node) -> tuple[str | None, bool]:
    """Return ``(target_name, is_field)`` for an ``assignment_expression``.

    Uses the tree-sitter ``left`` field rather than string splitting so that
    ``a[i] = x`` / ``obj.f = x`` / generic RHS are handled correctly.

    * ``x = ...``          -> ``("x", False)``
    * ``this.field = ...`` -> ``("field", True)``
    * ``obj.field = ...``  -> ``("field", True)``
    * complex targets (subscript, etc.) -> ``(None, False)``
    """
    left = ts_node.child_by_field_name("left")
    if left is None:
        return None, False
    if left.type == "identifier" and left.text is not None:
        return left.text.decode("utf-8"), False
    if left.type == "field_access":
        field_node = left.child_by_field_name("field")
        if field_node is not None and field_node.text is not None:
            return field_node.text.decode("utf-8"), True
    return None, False


def _extract_java_param_names(ts_node: Node) -> tuple[str, ...]:
    """Extract parameter names from a Java method or constructor."""
    params_node = ts_node.child_by_field_name("parameters")
    if params_node is None:
        return ()
    names: list[str] = []
    for child in params_node.children:
        if child.type == "formal_parameter":
            name_child = child.child_by_field_name("name")
            if name_child is not None and name_child.text is not None:
                names.append(name_child.text.decode("utf-8"))
        elif child.type == "spread_parameter":
            # ``String... args``
            for sub in child.children:
                if sub.type == "variable_declarator":
                    name_sub = sub.child_by_field_name("name")
                    if name_sub is not None and name_sub.text is not None:
                        names.append(name_sub.text.decode("utf-8"))
                elif sub.type == "identifier" and sub.text is not None:
                    names.append(sub.text.decode("utf-8"))
    return tuple(names)


def _extract_java_return_type(ts_node: Node) -> str | None:
    """Extract the return type from a Java ``method_declaration``."""
    type_node = ts_node.child_by_field_name("type")
    if type_node is not None and type_node.text is not None:
        return type_node.text.decode("utf-8")
    return None


def _compute_java_mccabe_complexity(ts_node: Node) -> int:
    """Compute McCabe cyclomatic complexity for a Java method node.

    Counts branch nodes within the entire method sub-tree.  For
    ``binary_expression`` nodes, only ``&&`` and ``||`` operators are
    counted.  The result is ``branch_count + 1``.
    """
    count = 0
    visited: set[int] = set()
    stack: list[Node] = [ts_node]
    while stack:
        node = stack.pop()
        node_id = id(node)
        if node_id in visited:
            continue
        visited.add(node_id)
        if node.type in _JAVA_BRANCH_TYPES:
            if node.type == "binary_expression":
                # Only count short-circuit operators.
                op = node.child_by_field_name("operator")
                if op is not None and op.text is not None:
                    op_text = op.text.decode("utf-8")
                    if op_text in ("&&", "||"):
                        count += 1
            else:
                count += 1
        for child in node.children:
            if child.is_named:
                stack.append(child)
    return count + 1


def _extract_java_interfaces(ts_node: Node) -> tuple[str, ...]:
    """Extract implemented interface names from a Java class declaration.

    Reads the ``super_interfaces`` field's ``type_list`` children.
    """
    names: list[str] = []
    for child in ts_node.children:
        if child.type == "super_interfaces":
            for grandchild in child.children:
                if grandchild.type == "type_list":
                    for type_child in grandchild.children:
                        if type_child.type == "type_identifier" and type_child.text is not None:
                            names.append(type_child.text.decode("utf-8"))
    return tuple(names)


# ── Layer detection (Java) ───────────────────────────────────────────────

# Maps path segments to architecture layer names.
_JAVA_LAYER_PATH_MAP: dict[str, str] = {
    "interfaces": "interface",
    "adapters": "adapter",
    "adapter": "adapter",
    "plugins": "plugin",
    "plugin": "plugin",
    "orchestrator": "engine",
    "engine": "engine",
    "models": "model",
    "model": "model",
    "domain": "model",
    "entity": "model",
    "service": "service",
    "services": "service",
    "controller": "presentation",
    "controllers": "presentation",
    "web": "presentation",
    "dao": "persistence",
    "repository": "persistence",
    "repositories": "persistence",
    "persistence": "persistence",
    "mapper": "persistence",
    "config": "config",
    "configuration": "config",
    "util": "utility",
    "utils": "utility",
    "helper": "utility",
    "test": "test",
    "tests": "test",
}


def _detect_java_layer(file_path: str) -> str:
    """Infer the architecture layer from the Java file path.

    Scans path segments against :data:`_JAVA_LAYER_PATH_MAP` and returns
    the first match, or ``"other"`` if none matches.
    """
    if not file_path:
        return "other"
    normalised = file_path.replace("\\", "/")
    parts = normalised.split("/")
    for part in parts:
        layer = _JAVA_LAYER_PATH_MAP.get(part.lower())
        if layer is not None:
            return layer
    return "other"
