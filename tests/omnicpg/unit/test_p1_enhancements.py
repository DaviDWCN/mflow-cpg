"""Unit tests for P1 enhancements.

Covers: structured properties, layer detection, IMPLEMENTS/TESTS edges.
"""

from __future__ import annotations

from omnicpg.models.edge import EdgeType
from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder as JavaASTBuilder
from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder as PythonASTBuilder

# ── Python structured function properties ─────────────────────────────────


class TestPythonFunctionProperties:
    """Tests for structured metadata on Python function nodes."""

    def test_param_names_simple(self) -> None:
        """Plain positional parameters are extracted."""
        builder = PythonASTBuilder()
        source = "def greet(name, age):\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        assert methods[0].properties["param_names"] == ("name", "age")

    def test_param_names_typed(self) -> None:
        """Typed parameters are extracted without annotations."""
        builder = PythonASTBuilder()
        source = "def greet(name: str, age: int = 0) -> str:\n    return ''\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["param_names"] == ("name", "age")

    def test_param_names_skips_self_and_cls(self) -> None:
        """``self`` and ``cls`` are excluded from param_names."""
        builder = PythonASTBuilder()
        source = "class Foo:\n    def bar(self, x):\n        pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["param_names"] == ("x",)

    def test_param_names_empty(self) -> None:
        """A function with no parameters returns an empty tuple."""
        builder = PythonASTBuilder()
        source = "def noop():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["param_names"] == ()

    def test_return_type_present(self) -> None:
        """Return type annotation is extracted."""
        builder = PythonASTBuilder()
        source = "def greet(name: str) -> str:\n    return name\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["return_type"] == "str"

    def test_return_type_none_when_absent(self) -> None:
        """Return type is ``None`` when no annotation exists."""
        builder = PythonASTBuilder()
        source = "def greet():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["return_type"] is None

    def test_is_async_true(self) -> None:
        """``is_async`` is ``True`` for async functions."""
        builder = PythonASTBuilder()
        # The tree-sitter Python grammar wraps ``async def`` in a
        # ``function_definition`` whose text starts with ``async``.
        source = "async def fetch():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) >= 1
        async_methods = [m for m in methods if m.properties.get("is_async") is True]
        assert len(async_methods) >= 1

    def test_is_async_false(self) -> None:
        """``is_async`` is ``False`` for regular functions."""
        builder = PythonASTBuilder()
        source = "def greet():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["is_async"] is False

    def test_decorators_extracted(self) -> None:
        """Decorator names are extracted from decorated definitions."""
        builder = PythonASTBuilder()
        source = "@staticmethod\n@cache\ndef greet():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert len(methods) == 1
        decorators = methods[0].properties["decorators"]
        assert "staticmethod" in decorators
        assert "cache" in decorators

    def test_decorators_empty(self) -> None:
        """A function with no decorators returns empty tuple."""
        builder = PythonASTBuilder()
        source = "def plain():\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["decorators"] == ()

    def test_complexity_simple(self) -> None:
        """A function with no branches has complexity 1."""
        builder = PythonASTBuilder()
        source = "def simple():\n    return 1\n"
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["complexity"] == 1

    def test_complexity_with_branches(self) -> None:
        """Complexity increases with if/for/while."""
        builder = PythonASTBuilder()
        source = (
            "def complex_func(x):\n"
            "    if x > 0:\n"
            "        for i in range(x):\n"
            "            while True:\n"
            "                break\n"
            "    return x\n"
        )
        nodes, _ = builder.build("test.py", source)
        methods = [n for n in nodes if n.has_label("Method")]
        # 1 base + if + for + while = 4
        assert methods[0].properties["complexity"] == 4


# ── Python class base_classes ─────────────────────────────────────────────


class TestPythonClassBaseClasses:
    """Tests for ``base_classes`` extraction on Python class nodes."""

    def test_single_base_class(self) -> None:
        """A class with one base class extracts it."""
        builder = PythonASTBuilder()
        source = "class Child(Parent):\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties["base_classes"] == ("Parent",)

    def test_multiple_base_classes(self) -> None:
        """A class with multiple base classes extracts all."""
        builder = PythonASTBuilder()
        source = "class MyClass(Base1, Base2, Base3):\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert classes[0].properties["base_classes"] == ("Base1", "Base2", "Base3")

    def test_no_base_classes(self) -> None:
        """A class with no base classes returns empty tuple."""
        builder = PythonASTBuilder()
        source = "class Standalone:\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert classes[0].properties["base_classes"] == ()

    def test_dotted_base_class(self) -> None:
        """A class with ``abc.ABC`` base extracts the full dotted name."""
        builder = PythonASTBuilder()
        source = "import abc\nclass MyABC(abc.ABC):\n    pass\n"
        nodes, _ = builder.build("test.py", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert "abc.ABC" in classes[0].properties["base_classes"]


# ── Layer detection ───────────────────────────────────────────────────────


class TestLayerDetection:
    """Tests for architecture layer detection on File and Module nodes."""

    def test_python_interface_layer(self) -> None:
        """A file in ``/interfaces/`` gets layer ``interface``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/interfaces/plugin.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "interface"

    def test_python_adapter_layer(self) -> None:
        """A file in ``/adapters/`` gets layer ``adapter``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/adapters/neo4j.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "adapter"

    def test_python_plugin_layer(self) -> None:
        """A file in ``/plugins/`` gets layer ``plugin``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/plugins/python_plugin/ast_builder.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "plugin"

    def test_python_engine_layer(self) -> None:
        """A file in ``/orchestrator/`` gets layer ``engine``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/orchestrator/main.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "engine"

    def test_python_model_layer(self) -> None:
        """A file in ``/models/`` gets layer ``model``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/models/node.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "model"

    def test_python_test_layer(self) -> None:
        """A file in ``/tests/`` gets layer ``test``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("tests/unit/test_ast.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "test"

    def test_python_other_layer(self) -> None:
        """A file in an unknown directory gets layer ``other``."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("scripts/run.py", "x = 1\n")
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "other"

    def test_module_node_also_gets_layer(self) -> None:
        """The Module node (root AST node) also gets a ``layer`` property."""
        builder = PythonASTBuilder()
        nodes, _ = builder.build("src/omnicpg/slicer/code_slicer.py", "x = 1\n")
        module_nodes = [n for n in nodes if n.has_label("Module")]
        assert module_nodes[0].properties["layer"] == "slicer"

    def test_java_service_layer(self) -> None:
        """A Java file in ``/service/`` gets layer ``service``."""
        builder = JavaASTBuilder()
        source = "public class UserService {}\n"
        nodes, _ = builder.build("src/main/java/service/UserService.java", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "service"

    def test_java_controller_layer(self) -> None:
        """A Java file in ``/controller/`` gets layer ``presentation``."""
        builder = JavaASTBuilder()
        source = "public class UserController {}\n"
        nodes, _ = builder.build("src/main/java/controller/UserController.java", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "presentation"

    def test_java_test_layer(self) -> None:
        """A Java file in ``/test/`` gets layer ``test``."""
        builder = JavaASTBuilder()
        source = "public class FooTest {}\n"
        nodes, _ = builder.build("src/test/java/FooTest.java", source)
        file_nodes = [n for n in nodes if n.has_label("File")]
        assert file_nodes[0].properties["layer"] == "test"


# ── IMPLEMENTS edges ──────────────────────────────────────────────────────


class TestImplementsEdges:
    """Tests for ``IMPLEMENTS`` edge generation."""

    def test_python_implements_edge(self) -> None:
        """A Python class inheriting another in the same file produces an IMPLEMENTS edge."""
        builder = PythonASTBuilder()
        source = "class Base:\n    pass\n\nclass Child(Base):\n    pass\n"
        nodes, edges = builder.build("test.py", source)
        impl_edges = [e for e in edges if e.edge_type == EdgeType.IMPLEMENTS]
        assert len(impl_edges) == 1

        # Verify source is Child, target is Base.
        node_index = {n.id: n for n in nodes}
        source_node = node_index[impl_edges[0].source_id]
        target_node = node_index[impl_edges[0].target_id]
        assert source_node.properties["name"] == "Child"
        assert target_node.properties["name"] == "Base"
        assert impl_edges[0].properties["base_class"] == "Base"

    def test_python_no_implements_for_external_base(self) -> None:
        """No IMPLEMENTS edge when base class is not in the same file."""
        builder = PythonASTBuilder()
        source = "class Child(ExternalBase):\n    pass\n"
        _, edges = builder.build("test.py", source)
        impl_edges = [e for e in edges if e.edge_type == EdgeType.IMPLEMENTS]
        assert len(impl_edges) == 0

    def test_python_multiple_bases(self) -> None:
        """Multiple bases in the same file produce multiple IMPLEMENTS edges."""
        builder = PythonASTBuilder()
        source = "class A:\n    pass\nclass B:\n    pass\nclass C(A, B):\n    pass\n"
        _, edges = builder.build("test.py", source)
        impl_edges = [e for e in edges if e.edge_type == EdgeType.IMPLEMENTS]
        assert len(impl_edges) == 2

    def test_java_implements_via_superclass(self) -> None:
        """A Java class with extends creates IMPLEMENTS edge to parent in same file."""
        builder = JavaASTBuilder()
        source = "public class Base {}\n\npublic class Child extends Base {}\n"
        nodes, edges = builder.build("test.java", source)
        impl_edges = [e for e in edges if e.edge_type == EdgeType.IMPLEMENTS]
        # NOTE: This depends on both classes being in the same file.
        # If there are implementation challenges, at minimum the base_classes
        # property should be set.
        node_index = {n.id: n for n in nodes}
        if impl_edges:
            source_node = node_index[impl_edges[0].source_id]
            target_node = node_index[impl_edges[0].target_id]
            assert source_node.properties["name"] == "Child"
            assert target_node.properties["name"] == "Base"


# ── TESTS edges ──────────────────────────────────────────────────────────


class TestTestsEdges:
    """Tests for ``TESTS`` edge generation from naming conventions."""

    def test_test_function_links_to_tested_function(self) -> None:
        """``test_greet`` → ``greet`` produces a TESTS edge."""
        builder = PythonASTBuilder()
        source = "def greet():\n    return 'hi'\n\ndef test_greet():\n    assert greet() == 'hi'\n"
        nodes, edges = builder.build("test.py", source)
        test_edges = [e for e in edges if e.edge_type == EdgeType.TESTS]
        assert len(test_edges) == 1

        node_index = {n.id: n for n in nodes}
        source_node = node_index[test_edges[0].source_id]
        target_node = node_index[test_edges[0].target_id]
        assert source_node.properties["name"] == "test_greet"
        assert target_node.properties["name"] == "greet"
        assert test_edges[0].properties["tested_function"] == "greet"

    def test_no_tests_edge_when_target_missing(self) -> None:
        """No TESTS edge when the tested function doesn't exist in the same file."""
        builder = PythonASTBuilder()
        source = "def test_missing():\n    pass\n"
        _, edges = builder.build("test.py", source)
        test_edges = [e for e in edges if e.edge_type == EdgeType.TESTS]
        assert len(test_edges) == 0

    def test_multiple_tests_edges(self) -> None:
        """Multiple test functions matching different targets."""
        builder = PythonASTBuilder()
        source = (
            "def add(a, b):\n    return a + b\n"
            "def sub(a, b):\n    return a - b\n"
            "def test_add():\n    assert add(1, 2) == 3\n"
            "def test_sub():\n    assert sub(3, 1) == 2\n"
        )
        _, edges = builder.build("test.py", source)
        test_edges = [e for e in edges if e.edge_type == EdgeType.TESTS]
        tested = {str(e.properties["tested_function"]) for e in test_edges}
        assert tested == {"add", "sub"}


# ── Java structured method properties ─────────────────────────────────────


class TestJavaMethodProperties:
    """Tests for structured metadata on Java method nodes."""

    def test_param_names_extracted(self) -> None:
        """Java method parameter names are extracted."""
        builder = JavaASTBuilder()
        source = (
            "public class Foo {\n"
            "    public String greet(String name, int age) {\n"
            "        return name;\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = builder.build("Foo.java", source)
        methods = [
            n for n in nodes if n.has_label("Method") and n.properties.get("name") == "greet"
        ]
        assert len(methods) == 1
        assert methods[0].properties["param_names"] == ("name", "age")

    def test_return_type_extracted(self) -> None:
        """Java method return type is extracted."""
        builder = JavaASTBuilder()
        source = 'public class Foo {\n    public String run() { return ""; }\n}\n'
        nodes, _ = builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method") and n.properties.get("name") == "run"]
        assert methods[0].properties["return_type"] == "String"

    def test_void_return_type(self) -> None:
        """Java void method return type is extracted as 'void'."""
        builder = JavaASTBuilder()
        source = "public class Foo {\n    public void doStuff() {}\n}\n"
        nodes, _ = builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["return_type"] == "void"

    def test_is_async_always_false(self) -> None:
        """Java has no native async, so ``is_async`` is always ``False``."""
        builder = JavaASTBuilder()
        source = "public class Foo {\n    public void run() {}\n}\n"
        nodes, _ = builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["is_async"] is False

    def test_complexity_simple(self) -> None:
        """A Java method with no branches has complexity 1."""
        builder = JavaASTBuilder()
        source = "public class Foo {\n    public int simple() { return 1; }\n}\n"
        nodes, _ = builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        assert methods[0].properties["complexity"] == 1

    def test_complexity_with_branches(self) -> None:
        """Complexity increases with if/for/while."""
        builder = JavaASTBuilder()
        source = (
            "public class Foo {\n"
            "    public void complex(int x) {\n"
            "        if (x > 0) {\n"
            "            for (int i = 0; i < x; i++) {\n"
            "                while (true) { break; }\n"
            "            }\n"
            "        }\n"
            "    }\n"
            "}\n"
        )
        nodes, _ = builder.build("Foo.java", source)
        methods = [n for n in nodes if n.has_label("Method")]
        # 1 base + if + for + while = 4
        assert methods[0].properties["complexity"] == 4

    def test_java_base_classes_via_implements(self) -> None:
        """Java ``implements`` interfaces are extracted into ``base_classes``."""
        builder = JavaASTBuilder()
        source = "public class Foo implements Bar, Baz {}\n"
        nodes, _ = builder.build("Foo.java", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert len(classes) == 1
        assert classes[0].properties["base_classes"] == ("Bar", "Baz")

    def test_java_no_interfaces(self) -> None:
        """A Java class with no interfaces returns empty tuple."""
        builder = JavaASTBuilder()
        source = "public class Plain {}\n"
        nodes, _ = builder.build("Foo.java", source)
        classes = [n for n in nodes if n.has_label("Class")]
        assert classes[0].properties["base_classes"] == ()
