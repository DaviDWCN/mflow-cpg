"""Unit tests for deterministic ID generation and idempotent analysis."""

from __future__ import annotations

from omnicpg.utils.id_gen import (
    generate_deterministic_id,
    generate_deterministic_id_from_key,
    generate_id,
)


class TestDeterministicIdGeneration:
    """Tests for :func:`generate_deterministic_id`."""

    def test_same_inputs_same_output(self) -> None:
        """Identical inputs must always produce the same ID."""
        id1 = generate_deterministic_id("src/main.py", "function_definition", "foo", 10)
        id2 = generate_deterministic_id("src/main.py", "function_definition", "foo", 10)
        assert id1 == id2

    def test_different_file_different_id(self) -> None:
        """Changing the file path produces a different ID."""
        id1 = generate_deterministic_id("src/a.py", "function_definition", "foo", 10)
        id2 = generate_deterministic_id("src/b.py", "function_definition", "foo", 10)
        assert id1 != id2

    def test_different_type_different_id(self) -> None:
        """Changing the node type produces a different ID."""
        id1 = generate_deterministic_id("a.py", "function_definition", "foo", 10)
        id2 = generate_deterministic_id("a.py", "class_definition", "foo", 10)
        assert id1 != id2

    def test_different_name_different_id(self) -> None:
        """Changing the name produces a different ID."""
        id1 = generate_deterministic_id("a.py", "function_definition", "foo", 10)
        id2 = generate_deterministic_id("a.py", "function_definition", "bar", 10)
        assert id1 != id2

    def test_different_line_different_id(self) -> None:
        """Same-name entities on different lines produce different IDs."""
        id1 = generate_deterministic_id("a.py", "function_definition", "foo", 10)
        id2 = generate_deterministic_id("a.py", "function_definition", "foo", 20)
        assert id1 != id2

    def test_output_is_32_hex_chars(self) -> None:
        """The ID must be a 32-character lowercase hex string."""
        result = generate_deterministic_id("x.py", "module", "x", 1)
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_random_id_differs_from_deterministic(self) -> None:
        """Random UUIDs and deterministic IDs have different formats."""
        random = generate_id()
        deterministic = generate_deterministic_id("x.py", "module", "x", 1)
        # Random UUID has dashes; deterministic is pure hex.
        assert "-" in random
        assert "-" not in deterministic


class TestDeterministicIdFromKey:
    """Tests for :func:`generate_deterministic_id_from_key`."""

    def test_same_key_same_output(self) -> None:
        """Identical keys always produce the same ID."""
        k = "some-method-id:entry"
        assert generate_deterministic_id_from_key(k) == generate_deterministic_id_from_key(k)

    def test_different_keys_different_output(self) -> None:
        """Different keys produce different IDs."""
        assert generate_deterministic_id_from_key("a:entry") != generate_deterministic_id_from_key(
            "a:exit"
        )

    def test_output_is_32_hex_chars(self) -> None:
        """The ID must be a 32-character lowercase hex string."""
        result = generate_deterministic_id_from_key("test-key")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)


class TestASTBuilderIdempotency:
    """Verify that re-parsing the same file produces identical node IDs."""

    def test_python_ast_idempotent(self) -> None:
        """Two parses of the same Python source produce the same node IDs."""
        from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder

        builder = ASTBuilder()
        source = "class Foo:\n    def bar(self):\n        return 42\n"

        nodes1, _ = builder.build("src/example.py", source)
        nodes2, _ = builder.build("src/example.py", source)

        ids1 = sorted(n.id for n in nodes1)
        ids2 = sorted(n.id for n in nodes2)
        assert ids1 == ids2

    def test_java_ast_idempotent(self) -> None:
        """Two parses of the same Java source produce the same node IDs."""
        from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder

        builder = ASTBuilder()
        source = "public class Foo {\n    public int bar() {\n        return 42;\n    }\n}\n"

        nodes1, _ = builder.build("src/Foo.java", source)
        nodes2, _ = builder.build("src/Foo.java", source)

        ids1 = sorted(n.id for n in nodes1)
        ids2 = sorted(n.id for n in nodes2)
        assert ids1 == ids2

    def test_python_cfg_entry_exit_idempotent(self) -> None:
        """CFG Entry/Exit IDs are deterministic across re-builds."""
        from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder
        from omnicpg.plugins.python_plugin.cfg_builder import CFGBuilder

        ast_builder = ASTBuilder()
        source = "def foo():\n    x = 1\n    return x\n"

        nodes, ast_edges = ast_builder.build("test.py", source)
        cfg1 = CFGBuilder().build(nodes, ast_edges)
        cfg2 = CFGBuilder().build(nodes, ast_edges)

        ids1 = sorted((e.source_id, e.target_id) for e in cfg1)
        ids2 = sorted((e.source_id, e.target_id) for e in cfg2)
        assert ids1 == ids2

    def test_java_cfg_entry_exit_idempotent(self) -> None:
        """Java CFG Entry/Exit IDs are deterministic across re-builds."""
        from omnicpg.plugins.java_plugin.ast_builder import ASTBuilder
        from omnicpg.plugins.java_plugin.cfg_builder import CFGBuilder

        ast_builder = ASTBuilder()
        source = (
            "public class Foo {\n"
            "    public int bar() {\n"
            "        int x = 1;\n"
            "        return x;\n"
            "    }\n"
            "}\n"
        )

        nodes, ast_edges = ast_builder.build("Foo.java", source)
        cfg1 = CFGBuilder().build(nodes, ast_edges)
        cfg2 = CFGBuilder().build(nodes, ast_edges)

        ids1 = sorted((e.source_id, e.target_id) for e in cfg1)
        ids2 = sorted((e.source_id, e.target_id) for e in cfg2)
        assert ids1 == ids2

    def test_different_files_different_ids(self) -> None:
        """Same source in different files should have different IDs."""
        from omnicpg.plugins.python_plugin.ast_builder import ASTBuilder

        builder = ASTBuilder()
        source = "def foo():\n    pass\n"

        nodes1, _ = builder.build("a.py", source)
        nodes2, _ = builder.build("b.py", source)

        ids1 = set(n.id for n in nodes1)
        ids2 = set(n.id for n in nodes2)
        # Node IDs should not overlap since files differ.
        assert not ids1 & ids2
