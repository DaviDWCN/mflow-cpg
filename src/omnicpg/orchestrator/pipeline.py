from __future__ import annotations

import hashlib
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from omnicpg.interfaces.language_plugin import LanguagePlugin

from omnicpg.adapters.neo4j_adapter import Neo4jAdapter
from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.orchestrator.project_orchestrator import ProjectOrchestrator

logger = logging.getLogger(__name__)


# registries for dependency inversion
_config_provider: Callable[[], Any] | None = None
_post_processors: list[Callable[[Neo4jAdapter, str], None]] = []


def register_config_provider(provider_fn: Callable[[], Any]) -> None:
    """Register a configuration provider callback."""
    global _config_provider
    _config_provider = provider_fn


def register_pipeline_post_processor(post_proc_fn: Callable[[Neo4jAdapter, str], None]) -> None:
    """Register a post-processor to run after AST parsing and graph enrichment."""
    _post_processors.append(post_proc_fn)


def get_config() -> Any:
    """Get the process-level configuration from the registered provider."""
    if _config_provider is not None:
        return _config_provider()
    return None


def _derive_project_id(project_path: str) -> str:
    """Derive a deterministic project id from the analyzed path."""
    real_path = os.path.realpath(project_path)
    path_hash = hashlib.sha256(real_path.encode("utf-8")).hexdigest()[:16]
    return f"proj-{path_hash}"


def get_plugins_for_directory(directory: str) -> list[LanguagePlugin]:
    """Identify which plugins should be used for a given directory or file."""
    from omnicpg.plugins.java_plugin.plugin import JavaPlugin
    from omnicpg.plugins.lsif_plugin.plugin import LSIFPlugin
    from omnicpg.plugins.openapi_plugin.plugin import OpenAPIPlugin
    from omnicpg.plugins.python_plugin.plugin import PythonPlugin

    plugins: list[LanguagePlugin] = []

    # Simple check for demo purposes, can be expanded
    has_java = False
    has_python = False
    has_openapi = False
    has_lsif = False

    if os.path.isfile(directory):
        if directory.endswith((".java", ".jsp", ".xml", ".properties")):
            has_java = True
        elif directory.endswith(".py"):
            has_python = True
        elif directory.endswith((".json", ".yaml", ".yml")):
            has_openapi = True
        elif directory.endswith(".lsif"):
            has_lsif = True
    else:
        for _, _subdirs, files in os.walk(directory):
            for file in files:
                if file.endswith((".java", ".jsp", ".xml", ".properties")):
                    has_java = True
                if file.endswith(".py"):
                    has_python = True
                if file.endswith((".json", ".yaml", ".yml")):
                    has_openapi = True
                if file.endswith(".lsif"):
                    has_lsif = True
            if has_java and has_python and has_openapi and has_lsif:
                break

    if has_java:
        plugins.append(JavaPlugin())
    if has_python:
        plugins.append(PythonPlugin())
    if has_openapi:
        plugins.append(OpenAPIPlugin())
    if has_lsif:
        plugins.append(LSIFPlugin())

    return plugins


def run_analysis_pipeline(
    path: str,
    uri: str = "",
    user: str = "",
    password: str = "",
    clear_db: bool = False,
    max_workers: int = 1,
    chunk_size: int = 500,
    db_batch_size: int = 500,
    analysis_level: AnalysisLevel | None = None,
    language: str = "auto",
    project_id: str = "",
    resume: bool = False,
    specific_files: list[str] | None = None,
    deleted_files: list[str] | None = None,
) -> dict[str, Any]:
    """End-to-end analysis pipeline."""
    try:
        unified_cfg = get_config()
        if unified_cfg is not None:
            if not uri:
                uri = unified_cfg.neo4j.uri
            if not user:
                user = unified_cfg.neo4j.username
            if not password:
                password = unified_cfg.neo4j.password
            if analysis_level is None:
                analysis_level = AnalysisLevel(unified_cfg.cpg.analysis_level)
    except Exception:
        if not uri:
            uri = "bolt://localhost:7687"
        if not user:
            user = "neo4j"
        if not password:
            password = "password"
        if analysis_level is None:
            analysis_level = AnalysisLevel.FULL

    if not project_id:
        project_id = _derive_project_id(path)
    plugins: list[LanguagePlugin]
    if language == "java":
        from omnicpg.plugins.java_plugin.plugin import JavaPlugin

        plugins = [JavaPlugin()]
    elif language == "python":
        from omnicpg.plugins.python_plugin.plugin import PythonPlugin

        plugins = [PythonPlugin()]
    elif language == "openapi":
        from omnicpg.plugins.openapi_plugin.plugin import OpenAPIPlugin

        plugins = [OpenAPIPlugin()]
    elif language == "lsif":
        from omnicpg.plugins.lsif_plugin.plugin import LSIFPlugin

        plugins = [LSIFPlugin()]
    else:
        plugins = get_plugins_for_directory(path)

    if not plugins:
        return {"status": "error", "message": f"No plugins found for path: {path}"}

    orchestrator = ProjectOrchestrator(
        plugins=plugins,
        max_workers=max_workers,
        analysis_level=analysis_level,
    )

    adapter = Neo4jAdapter(batch_size=db_batch_size)
    adapter.connect(uri, (user, password))

    files_for_analysis: list[str] | None = None

    if specific_files is not None:
        files_for_analysis = specific_files

    if clear_db:
        adapter.clear()
        # Drop secondary indexes before bulk load — they will be rebuilt after.
        # Keeping only the uniqueness constraint lets MERGE resolve duplicates
        # without paying the cost of maintaining name/file_path indexes on every write.
        adapter.drop_secondary_indexes()

    # For ARCHITECTURAL mode, ensure skeleton-optimised indexes are in place.
    if analysis_level == AnalysisLevel.ARCHITECTURAL:
        adapter.ensure_architectural_indexes()

    if resume and clear_db:
        logger.warning("resume=true is ignored when clear_db=true")

    if resume and not clear_db and os.path.isdir(path) and specific_files is None:
        all_files = [str(p) for p in orchestrator.scan_directory(path)]
        existing_rows = adapter.query(
            "MATCH (n:Node {project_id: $project_id}) "
            "WHERE n.file_path IS NOT NULL "
            "RETURN DISTINCT n.file_path AS file_path",
            project_id=project_id,
        )
        existing_files = {
            str(row.get("file_path", ""))
            for row in existing_rows
            if isinstance(row.get("file_path"), str) and row.get("file_path")
        }
        files_for_analysis = [
            file_path for file_path in all_files if file_path not in existing_files
        ]
        logger.info(
            "Resume mode: %d total files, %d already indexed, %d pending",
            len(all_files),
            len(existing_files),
            len(files_for_analysis),
        )

    # ── Incremental file-level cleanup ────────────────────────────────
    # When *not* clearing the entire DB, remove stale nodes for each file
    # that will be re-analysed.  This ensures deleted code entities don't
    # persist as ghost nodes, and works together with deterministic IDs
    # and MERGE-based inserts for a fully idempotent incremental pipeline.
    files_to_clear: list[str] = []
    if not clear_db:
        if files_for_analysis is not None:
            files_to_clear.extend(files_for_analysis)
        elif os.path.isfile(path):
            files_to_clear.append(path)
        elif os.path.isdir(path):
            for root, _, filenames in os.walk(path):
                for fname in filenames:
                    files_to_clear.append(os.path.join(root, fname))

        if deleted_files:
            files_to_clear.extend(deleted_files)

        if files_to_clear:
            cleared_count = adapter.clear_files_batch(files_to_clear)
            if cleared_count:
                logger.info(
                    "Incremental cleanup: removed %d stale nodes across %d files",
                    cleared_count,
                    len(files_to_clear),
                )

    total_nodes = 0
    total_edges = 0

    # In a full-rebuild run, keep node writes MERGE to tolerate occasional
    # duplicate deterministic IDs across plugins/files, but use CREATE for
    # relationships to avoid MERGE dedup overhead.
    bulk_load_edges = clear_db

    try:
        if files_for_analysis is not None and not files_for_analysis:
            return {
                "status": "success",
                "total_nodes": 0,
                "total_edges": 0,
                "path": path,
                "level": analysis_level.name,
                "project_id": project_id,
            }

        if chunk_size > 0:
            stream_iter = (
                orchestrator.analyze_streaming_files(files_for_analysis, chunk_size=chunk_size)
                if files_for_analysis is not None
                else orchestrator.analyze_streaming(path, chunk_size=chunk_size)
            )
            for _chunk_idx, (nodes, edges) in enumerate(stream_iter, 1):
                if nodes:
                    adapter.insert_nodes(nodes, bulk_load=False, project_id=project_id)
                    total_nodes += len(nodes)
                if edges:
                    adapter.insert_edges(edges, bulk_load=bulk_load_edges, project_id=project_id)
                    total_edges += len(edges)
        else:
            nodes, edges = orchestrator.analyze(path)
            adapter.insert_nodes(nodes, bulk_load=False, project_id=project_id)
            adapter.insert_edges(edges, bulk_load=bulk_load_edges, project_id=project_id)
            total_nodes = len(nodes)
            total_edges = len(edges)

        # ── Post-import enrichment ────────────────────────────────────
        # Materialize cross-file inheritance edges (base types almost
        # always live in a different file than the referencing class, so
        # per-file analysis cannot connect them) and ensure the code
        # full-text index that powers keyword search for AI agents.
        inheritance_summary: dict[str, Any] = {}
        parameter_summary: dict[str, Any] = {}
        cha_summary: dict[str, Any] = {}
        role_summary: dict[str, Any] = {}
        try:
            from omnicpg.orchestrator.graph_enrichment import (
                classify_architectural_roles,
                materialize_inheritance_edges,
                materialize_java_parameter_reaches_edges,
                materialize_cha_polymorphism_edges,
            )

            inheritance_summary = materialize_inheritance_edges(adapter, project_id)
            parameter_summary = materialize_java_parameter_reaches_edges(adapter, project_id)
            try:
                cha_summary = materialize_cha_polymorphism_edges(adapter, project_id)
            except Exception as e:
                logger.warning(f"CHA Polymorphism edge materialization failed: {e}", exc_info=True)
            role_summary = classify_architectural_roles(adapter, project_id)

            # Run registered post-processors (e.g., LLM semantic enrichment)
            for post_proc in _post_processors:
                try:
                    post_proc(adapter, project_id)
                except Exception as e:
                    logger.warning(f"Pipeline post-processor failed: {e}", exc_info=True)
        except Exception:
            logger.warning("Graph enrichment pass failed", exc_info=True)
        try:
            adapter.ensure_fulltext_indexes()
        except Exception:
            logger.warning("Full-text index creation failed", exc_info=True)

        # ── Git metadata ──────────────────────────────────────────────────
        try:
            from omnicpg.utils.git_utils import get_git_commit

            commit_hash = get_git_commit(path if os.path.isdir(path) else os.path.dirname(path))
            if commit_hash:
                adapter.query(
                    """
                    MERGE (m:ProjectMetadata {project_id: $project_id})
                    SET m.last_commit = $commit_hash, m.updated_at = timestamp()
                    """,
                    project_id=project_id,
                    commit_hash=commit_hash,
                )
        except Exception:
            logger.warning("Failed to save git commit metadata", exc_info=True)

        return {
            "status": "success",
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "inheritance_edges": inheritance_summary.get("edges_created", 0),
            "parameter_reaches_edges": parameter_summary.get("edges_materialized", 0),
            "virtual_calls_edges": cha_summary.get("virtual_calls_created", 0),
            "virtual_reaches_edges": cha_summary.get("virtual_reaches_created", 0),
            "classified_roles": role_summary.get("classes_classified", 0),
            "path": path,
            "level": analysis_level.name,
            "project_id": project_id,
        }
    except Exception as e:
        logger.error(f"Analysis failed: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        if clear_db:
            # Rebuild secondary indexes now that all data is loaded.
            try:
                adapter.rebuild_secondary_indexes()
            except Exception:
                logger.warning("Failed to rebuild secondary indexes", exc_info=True)
        adapter.disconnect()
