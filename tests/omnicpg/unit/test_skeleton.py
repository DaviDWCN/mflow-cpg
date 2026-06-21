"""Unit tests for the macro-skeleton overlay (File nodes, CONTAINS, DEPENDS_ON, enriched props)."""

from __future__ import annotations

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder as JavaASTBuilder
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder as PythonASTBuilder

# ── Python skeleton tests ─────────────────────────────────────────────────────


class TestPythonSkeleton:
    """Tests for the Python AST builder's macro-skeleton overlay."""

    def test_file_node_emitted(self) -> None:
        """A :File node is emitted for each parsed Python file."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("app.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert len(file_nodes) == 1
        assert file_nodes[0].properties["name"] == "app.py"
        assert file_nodes[0].properties["file_path"] == "app.py"

    def test_contains_file_to_class(self) -> None:
        """A CONTAINS edge connects File → Class."""
        builder = PythonASTBuilder()
        source = "class Foo:\n    pass\n"
        nodes, edges = builder.build("foo.py", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        class_nodes = [n for n in nodes if n.has_label("Class")]
        assert len(file_nodes) == 1
        assert len(class_nodes) == 1
        contains = [
            e
            for e in edges
            if e.edge_type == EdgeType.CONTAINS
            and e.source_id == file_nodes[0].id
            and e.target_id == class_nodes[0].id
        ]
        assert len(contains) == 1

    def test_contains_class_to_method(self) -> None:
        """A CONTAINS edge connects Class → Method."""
        builder = PythonASTBuilder()
        source = "class Foo:\n    def bar(self):\n        pass\n"
        nodes, edges = builder.build("foo.py", source)
        class_nodes = [n for n in nodes if n.has_label("Class")]
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(class_nodes) == 1
        assert len(method_nodes) == 1
        contains = [
            e
            for e in edges
            if e.edge_type == EdgeType.CONTAINS
            and e.source_id == class_nodes[0].id
            and e.target_id == method_nodes[0].id
        ]
        assert len(contains) == 1

    def test_contains_file_to_toplevel_function(self) -> None:
        """A top-level function gets File → Method CONTAINS edge."""
        builder = PythonASTBuilder()
        source = "def greet():\n    pass\n"
        nodes, edges = builder.build("greet.py", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(file_nodes) == 1
        assert len(method_nodes) == 1
        contains = [
            e
            for e in edges
            if e.edge_type == EdgeType.CONTAINS
            and e.source_id == file_nodes[0].id
            and e.target_id == method_nodes[0].id
        ]
        assert len(contains) == 1

    def test_depends_on_import(self) -> None:
        """An import statement produces a DEPENDS_ON edge with the module name."""
        builder = PythonASTBuilder()
        source = "import os\nimport sys\n"
        _, edges = builder.build("app.py", source)
        dep_edges = [e for e in edges if e.edge_type == EdgeType.DEPENDS_ON]
        modules = {str(e.properties.get("module")) for e in dep_edges}
        assert "os" in modules
        assert "sys" in modules

    def test_depends_on_from_import(self) -> None:
        """A ``from … import`` statement produces a DEPENDS_ON edge."""
        builder = PythonASTBuilder()
        source = "from pathlib import Path\n"
        _, edges = builder.build("app.py", source)
        dep_edges = [e for e in edges if e.edge_type == EdgeType.DEPENDS_ON]
        assert len(dep_edges) >= 1
        assert any(str(e.properties.get("module")) == "pathlib" for e in dep_edges)

    def test_method_has_signature(self) -> None:
        """A function node carries a ``signature`` property."""
        builder = PythonASTBuilder()
        source = "def greet(name: str) -> str:\n    return 'hi'\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert "signature" in methods[0].properties
        sig = str(methods[0].properties["signature"])
        assert "greet" in sig

    def test_method_has_docstring(self) -> None:
        """A function with a docstring extracts it into the ``docstring`` property."""
        builder = PythonASTBuilder()
        source = 'def greet():\n    """Say hello."""\n    return "hi"\n'
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert "docstring" in methods[0].properties
        assert "Say hello" in str(methods[0].properties["docstring"])

    def test_method_has_source_code(self) -> None:
        """A function node carries its full source code."""
        builder = PythonASTBuilder()
        source = "def greet():\n    return 'hi'\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert "source_code" in methods[0].properties
        assert "return 'hi'" in str(methods[0].properties["source_code"])

    def test_class_has_docstring(self) -> None:
        """A class with a docstring extracts it."""
        builder = PythonASTBuilder()
        source = 'class Foo:\n    """A foo class."""\n    pass\n'
        nodes, _ = builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert "docstring" in classes[0].properties
        assert "A foo class" in str(classes[0].properties["docstring"])

    def test_import_node_has_import_label(self) -> None:
        """Import statements carry the ``Import`` label."""
        builder = PythonASTBuilder()
        source = "import os\nfrom sys import path\n"
        nodes, _ = builder.build("test.py", source)
        imports = [n for n in nodes if n.has_label("Import")]
        assert len(imports) >= 2


# ── Java skeleton tests ──────────────────────────────────────────────────────


class TestJavaSkeleton:
    """Tests for the Java AST builder's macro-skeleton overlay."""

    def test_file_node_emitted(self) -> None:
        """A :File node is emitted for each parsed Java file."""
        builder = JavaASTBuilder()
        source = "public class Foo {}\n"
        nodes, _ = builder.build("Foo.java", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert len(file_nodes) == 1
        assert file_nodes[0].properties["name"] == "Foo.java"

    def test_contains_file_to_class(self) -> None:
        """A CONTAINS edge connects File → Class in Java."""
        builder = JavaASTBuilder()
        source = "public class Bar {\n    public void run() {}\n}\n"
        nodes, edges = builder.build("Bar.java", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        class_nodes = [n for n in nodes if n.has_label("Class")]
        assert len(file_nodes) == 1
        assert len(class_nodes) == 1
        contains = [
            e
            for e in edges
            if e.edge_type == EdgeType.CONTAINS
            and e.source_id == file_nodes[0].id
            and e.target_id == class_nodes[0].id
        ]
        assert len(contains) == 1

    def test_contains_class_to_method(self) -> None:
        """A CONTAINS edge connects Class → Method in Java."""
        builder = JavaASTBuilder()
        source = "public class Bar {\n    public void run() {}\n}\n"
        nodes, edges = builder.build("Bar.java", source)
        class_nodes = [n for n in nodes if n.has_label("Class")]
        method_nodes = [n for n in nodes if n.has_label("Method")]
        assert len(class_nodes) == 1
        assert len(method_nodes) >= 1
        contains = [
            e
            for e in edges
            if e.edge_type == EdgeType.CONTAINS and e.source_id == class_nodes[0].id
        ]
        assert len(contains) >= 1

    def test_depends_on_import(self) -> None:
        """Java import declarations produce DEPENDS_ON edges."""
        builder = JavaASTBuilder()
        source = "import java.util.List;\nimport java.util.Map;\n\npublic class Foo {}\n"
        _, edges = builder.build("Foo.java", source)
        dep_edges = [e for e in edges if e.edge_type == EdgeType.DEPENDS_ON]
        modules = {str(e.properties.get("module")) for e in dep_edges}
        assert "java.util.List" in modules
        assert "java.util.Map" in modules

    def test_method_has_signature_and_source_code(self) -> None:
        """Java methods carry ``signature`` and ``source_code`` properties."""
        builder = JavaASTBuilder()
        source = (
            "public class Bar {\n"
            "    public String greet(String name) {\n"
            "        return name;\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = builder.build("Bar.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) >= 1
        greet = [m for m in methods if m.properties.get("name") == "greet"]
        assert len(greet) == 1
        assert "signature" in greet[0].properties
        assert "source_code" in greet[0].properties
        assert "greet" in str(greet[0].properties["signature"])

    def test_no_skeleton_for_jsp(self) -> None:
        """JSP files do NOT get a :File skeleton (only .java files do)."""
        builder = JavaASTBuilder()
        source = '<% String msg = "hello"; %>\n'
        nodes, _ = builder.build("page.jsp", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert len(file_nodes) == 0

    def test_no_skeleton_for_xml(self) -> None:
        """XML config files do NOT get a :File skeleton."""
        builder = JavaASTBuilder()
        source = (
            '<?xml version="1.0"?>\n'
            "<beans>\n"
            '    <bean id="svc" class="com.example.Service"/>\n'
            "</beans>\n"
        )
        nodes, _ = builder.build("beans.xml", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert len(file_nodes) == 0
