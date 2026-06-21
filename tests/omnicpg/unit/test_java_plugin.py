"""Unit tests for the JavaPlugin (end-to-end through the plugin interface)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from omnicpg.models.edge import EdgeType
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator
from omnicpg.plugins.java_plugin import JavaPlugin


class TestJavaPlugin:
    """Tests for :class:`JavaPlugin` via its public interface."""

    def test_supported_extensions(self) -> None:
        """The plugin declares ``.java``, ``.jsp``, ``.xml`` as supported."""
        plugin = JavaPlugin()
        assert ".java" in plugin.supported_extensions
        assert ".jsp" in plugin.supported_extensions
        assert ".xml" in plugin.supported_extensions
        assert ".properties" in plugin.supported_extensions

    def test_parse_to_ast(self) -> None:
        """``parse_to_ast`` returns nodes and structural edges for Java."""
        plugin = JavaPlugin()
        source = "public class T {\n    public void greet() {\n        int x = 1;\n    }\n}\n"
        nodes, edges = plugin.parse_to_ast("T.java", source)
        assert len(nodes) > 0
        allowed = {EdgeType.PARENT_OF, EdgeType.CONTAINS, EdgeType.DEPENDS_ON}
        assert all(e.edge_type in allowed for e in edges)
        assert any(e.edge_type == EdgeType.PARENT_OF for e in edges)

    def test_build_cfg(self) -> None:
        """``build_cfg`` returns FLOWS_TO edges for Java methods."""
        plugin = JavaPlugin()
        source = (
            "public class T {\n"
            "    public int f() {\n"
            "        int x = 1;\n"
            "        return x;\n"
            "    }\n"
            "}\n"
        )
        nodes, ast_edges = plugin.parse_to_ast("T.java", source)
        cfg_edges = plugin.build_cfg(nodes, ast_edges)
        assert len(cfg_edges) > 0
        assert all(e.edge_type == EdgeType.FLOWS_TO for e in cfg_edges)

    def test_build_dfg(self) -> None:
        """``build_dfg`` returns REACHES edges for Java."""
        plugin = JavaPlugin()
        source = (
            "public class T {\n"
            "    public int f() {\n"
            "        int x = 1;\n"
            "        return x;\n"
            "    }\n"
            "}\n"
        )
        nodes, ast_edges = plugin.parse_to_ast("T.java", source)
        cfg_edges = plugin.build_cfg(nodes, ast_edges)
        dfg_edges = plugin.build_dfg(nodes, cfg_edges, ast_edges)
        assert len(dfg_edges) > 0
        assert all(e.edge_type == EdgeType.REACHES for e in dfg_edges)

    def test_end_to_end_via_orchestrator(self) -> None:
        """Full pipeline through ProjectOrchestrator with the real JavaPlugin."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "Sample.java").write_text(
                "public class Sample {\n"
                "    public String greet(String name) {\n"
                '        String msg = "Hi " + name;\n'
                "        return msg;\n"
                "    }\n"
                "}\n"
            )
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes, edges = orchestrator.analyze(tmpdir)

            assert len(nodes) > 0
            assert len(edges) > 0

            edge_types = {e.edge_type for e in edges}
            assert EdgeType.PARENT_OF in edge_types
            assert EdgeType.FLOWS_TO in edge_types
            assert EdgeType.REACHES in edge_types

    def test_cross_file_call_graph(self) -> None:
        """Call graph links method invocations across Java files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "Service.java").write_text(
                "public class Service {\n"
                "    public void doWork() {\n"
                "        System.out.println(1);\n"
                "    }\n"
                "}\n"
            )
            (Path(tmpdir) / "Client.java").write_text(
                "public class Client {\n"
                "    public void run() {\n"
                "        Service s = new Service();\n"
                "        s.doWork();\n"
                "    }\n"
                "}\n"
            )
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin()])
            _nodes, edges = orchestrator.analyze(tmpdir)

            call_edges = [e for e in edges if e.edge_type == EdgeType.CALLS]
            callees = {e.properties.get("callee") for e in call_edges}
            assert "doWork" in callees

    def test_mixed_java_and_python_plugins(self) -> None:
        """Multiple plugins (Java + Python) work together in one orchestrator."""
        from omnicpg.plugins.python_plugin import PythonPlugin

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "App.java").write_text(
                "public class App {\n"
                "    public void main() {\n"
                '        System.out.println("hello");\n'
                "    }\n"
                "}\n"
            )
            (Path(tmpdir) / "helper.py").write_text("def helper():\n    return 42\n")
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin(), PythonPlugin()])
            nodes, edges = orchestrator.analyze(tmpdir)

            assert len(nodes) > 0
            assert len(edges) > 0

            # Verify both Java and Python files were processed.
            files = {str(n.properties.get("file_path", "")) for n in nodes}
            assert any("App.java" in f for f in files)
            assert any("helper.py" in f for f in files)

    def test_jsp_file_via_orchestrator(self) -> None:
        """JSP files are correctly processed through the orchestrator."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "page.jsp").write_text('<% String msg = "hello"; %>\n<%= msg %>\n')
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes, _edges = orchestrator.analyze(tmpdir)

            assert len(nodes) > 0
            # Check that JSP root node exists.
            jsp_nodes = [n for n in nodes if n.properties.get("type") == "jsp_page"]
            assert len(jsp_nodes) == 1

    def test_xml_config_via_orchestrator(self) -> None:
        """XML config files are correctly processed through the orchestrator."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "beans.xml").write_text(
                '<?xml version="1.0"?>\n'
                "<beans>\n"
                '    <bean id="svc" class="com.example.Service"/>\n'
                "</beans>\n"
            )
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes, _edges = orchestrator.analyze(tmpdir)

            assert len(nodes) > 0
            bean_nodes = [n for n in nodes if n.has_label("SpringBean")]
            assert len(bean_nodes) == 1

    def test_properties_config_via_orchestrator(self) -> None:
        """Properties files become lightweight configuration nodes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.properties").write_text(
                "# comment\njdbc.url=jdbc:oracle:thin:@localhost:1521:test\nfeature.enabled=true\n"
            )
            orchestrator = ProjectOrchestrator(plugins=[JavaPlugin()])
            nodes, edges = orchestrator.analyze(tmpdir)

            entries = [n for n in nodes if n.properties.get("type") == "property_entry"]
            assert {n.properties.get("key") for n in entries} == {
                "jdbc.url",
                "feature.enabled",
            }
            assert any(e.edge_type == EdgeType.PARENT_OF for e in edges)
