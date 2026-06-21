"""Unit tests for the Java AST builder (Tree-sitter integration)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def ast_builder() -> ASTBuilder:
    """Return a fresh ASTBuilder instance."""
    return ASTBuilder()


@pytest.fixture()
def simple_java_source() -> str:
    """Return a small Java source snippet."""
    return (
        "public class Greeter {\n"
        "    public String greet(String name) {\n"
        '        String message = "Hello, " + name;\n'
        "        return message;\n"
        "    }\n"
        "}\n"
    )


class TestJavaASTBuilder:
    """Tests for :class:`ASTBuilder` (Java)."""

    def test_basic_parse_produces_nodes_and_edges(
        self, ast_builder: ASTBuilder, simple_java_source: str
    ) -> None:
        """Parsing valid Java produces at least one node and one edge."""
        nodes, edges = ast_builder.build("Test.java", simple_java_source)
        assert len(nodes) > 0
        assert len(edges) > 0

    def test_root_node_is_module(self, ast_builder: ASTBuilder, simple_java_source: str) -> None:
        """The first node should have 'Module' label (program node)."""
        nodes, _ = ast_builder.build("Test.java", simple_java_source)
        root = nodes[0]
        assert root.has_label("Module")
        assert root.properties["type"] == "program"

    def test_class_node_has_class_label(
        self, ast_builder: ASTBuilder, simple_java_source: str
    ) -> None:
        """A ``class_declaration`` node carries the 'Class' label."""
        nodes, _ = ast_builder.build("Test.java", simple_java_source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties["name"] == "Greeter"

    def test_method_node_has_method_label(
        self, ast_builder: ASTBuilder, simple_java_source: str
    ) -> None:
        """A ``method_declaration`` node carries the 'Method' label."""
        nodes, _ = ast_builder.build("Test.java", simple_java_source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert methods[0].properties["name"] == "greet"

    def test_all_edges_are_parent_of(
        self, ast_builder: ASTBuilder, simple_java_source: str
    ) -> None:
        """AST edges are ``PARENT_OF``; skeleton edges are ``CONTAINS``."""
        _, edges = ast_builder.build("Test.java", simple_java_source)
        allowed = {EdgeType.PARENT_OF, EdgeType.CONTAINS, EdgeType.DEPENDS_ON}
        assert all(e.edge_type in allowed for e in edges)
        # The classic AST sub-graph must still contain PARENT_OF edges.
        assert any(e.edge_type == EdgeType.PARENT_OF for e in edges)

    def test_edge_source_and_target_are_valid_node_ids(
        self, ast_builder: ASTBuilder, simple_java_source: str
    ) -> None:
        """Every edge's source and target must reference an existing node id."""
        nodes, edges = ast_builder.build("Test.java", simple_java_source)
        node_ids = {n.id for n in nodes}
        for edge in edges:
            assert edge.source_id in node_ids
            assert edge.target_id in node_ids

    def test_nodes_have_line_info(self, ast_builder: ASTBuilder, simple_java_source: str) -> None:
        """Every node should have ``line_start`` and ``line_end``."""
        nodes, _ = ast_builder.build("Test.java", simple_java_source)
        for node in nodes:
            assert "line_start" in node.properties
            assert "line_end" in node.properties
            assert node.properties["line_start"] >= 1

    def test_interface_node_has_interface_label(self, ast_builder: ASTBuilder) -> None:
        """An ``interface_declaration`` node carries the 'Interface' label."""
        source = "public interface Runnable {\n    void run();\n}\n"
        nodes, _ = ast_builder.build("Test.java", source)
        interfaces = [n for n in nodes if n.has_label("Interface")]
        assert len(interfaces) == 1
        assert interfaces[0].properties["name"] == "Runnable"

    def test_spring_annotation_detected(self, ast_builder: ASTBuilder) -> None:
        """Spring ``@Service`` annotation is detected and tagged."""
        source = (
            "import org.springframework.stereotype.Service;\n\n"
            "@Service\n"
            "public class MyService {\n"
            "    public void doWork() {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ast_builder.build("MyService.java", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties.get("framework") == "spring"
        assert "Service" in classes[0].properties.get("annotations", ())
        assert classes[0].has_label("SpringComponent")

    def test_hibernate_annotation_detected(self, ast_builder: ASTBuilder) -> None:
        """Hibernate ``@Entity`` annotation is detected and tagged."""
        source = (
            "import javax.persistence.Entity;\n"
            "import javax.persistence.Table;\n\n"
            "@Entity\n"
            '@Table(name = "users")\n'
            "public class User {\n"
            "    private Long id;\n"
            "}\n"
        )
        nodes, _ = ast_builder.build("User.java", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties.get("framework") == "hibernate"
        assert classes[0].has_label("HibernateEntity")

    def test_struts_action_detected(self, ast_builder: ASTBuilder) -> None:
        """Struts ``Action`` superclass is detected and tagged."""
        source = (
            "public class LoginAction extends Action {\n"
            "    public void execute() {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ast_builder.build("LoginAction.java", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties.get("framework") == "struts"
        assert classes[0].properties.get("superclass") == "Action"
        assert classes[0].has_label("StrutsAction")

    def test_jsp_parsing_produces_nodes(self, ast_builder: ASTBuilder) -> None:
        """A JSP file with scriptlets produces AST nodes."""
        source = (
            '<%@ page contentType="text/html" %>\n'
            "<html><body>\n"
            '<%\n    String msg = "hello";\n%>\n'
            "<%= msg %>\n"
            "</body></html>\n"
        )
        nodes, _edges = ast_builder.build("page.jsp", source)
        assert len(nodes) > 0
        # The root should be a JSP module node.
        root = nodes[0]
        assert root.has_label("Module")
        assert root.properties["type"] == "jsp_page"

    def test_spring_xml_config_parsing(self, ast_builder: ASTBuilder) -> None:
        """A Spring XML config file produces SpringBean nodes."""
        source = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<beans>\n"
            '    <bean id="userService" class="com.example.UserService"/>\n'
            "</beans>\n"
        )
        nodes, _edges = ast_builder.build("applicationContext.xml", source)
        bean_nodes = [n for n in nodes if n.has_label("SpringBean")]
        assert len(bean_nodes) == 1
        assert bean_nodes[0].properties["bean_id"] == "userService"
        assert bean_nodes[0].properties["bean_class"] == "com.example.UserService"

    def test_struts_xml_config_parsing(self, ast_builder: ASTBuilder) -> None:
        """A Struts XML config file produces StrutsAction nodes."""
        source = (
            "<struts-config>\n"
            "    <action-mappings>\n"
            '        <action path="/login" type="com.example.LoginAction"/>\n'
            "    </action-mappings>\n"
            "</struts-config>\n"
        )
        nodes, _edges = ast_builder.build("struts-config.xml", source)
        action_nodes = [n for n in nodes if n.has_label("StrutsAction")]
        assert len(action_nodes) == 1
        assert action_nodes[0].properties["action_path"] == "/login"
        assert action_nodes[0].properties["action_type"] == "com.example.LoginAction"

    def test_hibernate_xml_mapping(self, ast_builder: ASTBuilder) -> None:
        """A Hibernate XML mapping file produces HibernateEntity nodes."""
        source = (
            "<hibernate-mapping>\n"
            '    <class name="com.example.User" table="users">\n'
            '        <property name="username" column="username"/>\n'
            "    </class>\n"
            "</hibernate-mapping>\n"
        )
        nodes, _edges = ast_builder.build("User.hbm.xml", source)
        entity_nodes = [n for n in nodes if n.has_label("HibernateEntity")]
        assert len(entity_nodes) == 1
        assert entity_nodes[0].properties["entity_class"] == "com.example.User"
        assert entity_nodes[0].properties["table_name"] == "users"

        prop_nodes = [n for n in nodes if n.has_label("HibernateProperty")]
        assert len(prop_nodes) == 1
        assert prop_nodes[0].properties["property_name"] == "username"

    def test_fixture_file_parse(self, ast_builder: ASTBuilder, sample_java_dir: Path) -> None:
        """The sample Java fixture file can be parsed without errors."""
        java_file = sample_java_dir / "UserService.java"
        source = java_file.read_text()
        nodes, edges = ast_builder.build(str(java_file), source)
        assert len(nodes) > 10  # rough sanity check
        assert len(edges) > 5

    def test_constructor_has_method_label(self, ast_builder: ASTBuilder) -> None:
        """A ``constructor_declaration`` node carries the 'Method' label."""
        source = (
            "public class Foo {\n"
            "    public Foo(int x) {\n"
            "        System.out.println(x);\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = ast_builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) >= 1
        constructor_nodes = [m for m in methods if m.properties.get("name") == "Foo"]
        assert len(constructor_nodes) == 1

    def test_enum_has_enum_label(self, ast_builder: ASTBuilder) -> None:
        """An ``enum_declaration`` node carries the 'Enum' label."""
        source = "public enum Color {\n    RED, GREEN, BLUE\n}\n"
        nodes, _ = ast_builder.build("Color.java", source)
        enums = [n for n in nodes if n.has_label("Enum")]
        assert len(enums) == 1
        assert enums[0].properties["name"] == "Color"
