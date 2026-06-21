"""Unit tests for project_id propagation in the analysis pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.orchestrator.pipeline import _derive_project_id, run_analysis_pipeline


@patch("omnicpg.orchestrator.pipeline.ProjectOrchestrator")
@patch("omnicpg.orchestrator.pipeline.Neo4jAdapter")
@patch("omnicpg.orchestrator.pipeline.get_plugins_for_directory")
def test_run_analysis_pipeline_passes_derived_project_id(
    mock_get_plugins: MagicMock,
    mock_adapter_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
) -> None:
    """Pipeline should derive and propagate a stable project_id when omitted."""
    mock_get_plugins.return_value = [MagicMock()]
    mock_adapter = MagicMock()
    mock_adapter_cls.return_value = mock_adapter
    mock_orchestrator = MagicMock()
    mock_orchestrator.analyze_streaming.return_value = [
        ([MagicMock()], [MagicMock()]),
    ]
    mock_orchestrator_cls.return_value = mock_orchestrator

    path = "/workspace/sample-project"
    result = run_analysis_pipeline(
        path=path,
        uri="bolt://localhost:7687",
        user="neo4j",
        password="secret",
        analysis_level=AnalysisLevel.FULL,
        chunk_size=1,
    )

    expected_project_id = _derive_project_id(path)
    assert result["project_id"] == expected_project_id
    assert mock_adapter.insert_nodes.call_args.kwargs["project_id"] == expected_project_id
    assert mock_adapter.insert_edges.call_args.kwargs["project_id"] == expected_project_id


@patch("omnicpg.orchestrator.pipeline.os.path.isdir")
@patch("omnicpg.orchestrator.pipeline.ProjectOrchestrator")
@patch("omnicpg.orchestrator.pipeline.Neo4jAdapter")
@patch("omnicpg.orchestrator.pipeline.get_plugins_for_directory")
def test_run_analysis_pipeline_resume_only_processes_pending_files(
    mock_get_plugins: MagicMock,
    mock_adapter_cls: MagicMock,
    mock_orchestrator_cls: MagicMock,
    mock_isdir: MagicMock,
) -> None:
    """Resume mode should skip files already present for the project_id."""
    mock_isdir.return_value = True
    mock_get_plugins.return_value = [MagicMock()]

    mock_adapter = MagicMock()
    mock_adapter.query.return_value = [{"file_path": "/repo/a.py"}]
    mock_adapter_cls.return_value = mock_adapter

    mock_orchestrator = MagicMock()
    mock_orchestrator.scan_directory.return_value = ["/repo/a.py", "/repo/b.py"]
    mock_orchestrator.analyze_streaming_files.return_value = [([MagicMock()], [MagicMock()])]
    mock_orchestrator_cls.return_value = mock_orchestrator

    result = run_analysis_pipeline(
        path="/repo",
        uri="bolt://localhost:7687",
        user="neo4j",
        password="secret",
        resume=True,
        chunk_size=1,
        analysis_level=AnalysisLevel.FULL,
    )

    assert result["status"] == "success"
    mock_orchestrator.analyze_streaming_files.assert_called_once_with(["/repo/b.py"], chunk_size=1)
    mock_adapter.clear_files_batch.assert_called_once_with(["/repo/b.py"])
