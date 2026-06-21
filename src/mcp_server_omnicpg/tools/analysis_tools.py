"""Analysis tools for OmniCPG MCP Server."""

import logging
import os
import subprocess
from typing import Any

from mcp_server_omnicpg.config import Config
from mcp_server_omnicpg.neo4j_adapter import get_adapter
from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.orchestrator.pipeline import run_analysis_pipeline

logger = logging.getLogger(__name__)


def _is_project_root_target(abs_path: str) -> bool:
    """Return whether target path resolves to MCP project root."""
    project_root = os.path.realpath(os.environ.get("PROJECT_PATH", "/workspace"))
    target = os.path.realpath(abs_path)
    return target == project_root


def analyze_path(
    path: str,
    level: str = "FULL",
    language: str = "auto",
    chunk_size: int = 500,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Trigger an incremental analysis on a specific path.

    Args:
        path: Path relative to the workspace (e.g., 'src/main.java' or 'modules/core')
        level: Analysis level ('FULL' or 'ARCHITECTURAL')
        language: Programming language ('java', 'python', or 'auto')
        chunk_size: Processing chunk size for streaming
        max_workers: Concurrent workers
    """
    # Resolve absolute path. On Windows, ``os.path.isabs('/foo')`` is False even
    # though POSIX-style absolute paths should pass through unchanged.
    if not (os.path.isabs(path) or path.startswith(("/", "\\"))):
        base_path = os.environ.get("PROJECT_PATH", "/workspace")
        abs_path = os.path.join(base_path, path).replace("\\", "/")
    else:
        abs_path = path

    if _is_project_root_target(abs_path):
        return {
            "status": "error",
            "message": (
                "MCP 仅允许增量分析, 禁止对项目根路径执行全量重建。"
                "请通过 omnicpg 主镜像执行全量分析。"
            ),
        }

    logger.info(f"Triggering incremental analysis for: {abs_path} (Level: {level})")

    try:
        level_enum = AnalysisLevel[level.upper()]
    except KeyError:
        return {"status": "error", "message": f"Invalid analysis level: {level}"}

    # Use configuration from environment/MCP config
    result: dict[str, Any] = run_analysis_pipeline(
        path=abs_path,
        uri=Config.NEO4J_URI,
        user=Config.NEO4J_USER,
        password=Config.NEO4J_PASSWORD,
        clear_db=False,  # Always incremental
        max_workers=max_workers,
        chunk_size=chunk_size,
        analysis_level=level_enum,
        language=language.lower(),
    )

    return result


def sync_git_changes(
    commit_from: str = "HEAD~1",
    commit_to: str = "HEAD",
    level: str = "FULL",
    language: str = "auto",
    chunk_size: int = 500,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Sync the graph based on git changes between two commits.

    Args:
        commit_from: The starting git commit.
        commit_to: The ending git commit.
        level: Analysis level ('FULL' or 'ARCHITECTURAL')
        language: Programming language ('java', 'python', or 'auto')
        chunk_size: Processing chunk size for streaming
        max_workers: Concurrent workers
    """
    base_path = os.environ.get("PROJECT_PATH", "/workspace")
    logger.info(f"Syncing git changes from {commit_from} to {commit_to} in {base_path}")

    try:
        level_enum = AnalysisLevel[level.upper()]
    except KeyError:
        return {"status": "error", "message": f"Invalid analysis level: {level}"}

    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", commit_from, commit_to],
            cwd=base_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to run git diff: {e}")
        return {"status": "error", "message": f"Failed to get git changes: {e!s}"}

    files_to_analyze = []
    deleted_files = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            status = parts[0][0]  # M, A, D, R, etc.

            if status == "R":
                # Handle renames which have an extra token: R100 old_name new_name
                old_file_path = parts[1]
                new_file_path = parts[2] if len(parts) >= 3 else parts[-1]
                abs_old_file_path = os.path.join(base_path, old_file_path).replace("\\", "/")
                abs_new_file_path = os.path.join(base_path, new_file_path).replace("\\", "/")
                deleted_files.append(abs_old_file_path)
                files_to_analyze.append(abs_new_file_path)
            else:
                file_path = parts[-1]
                abs_file_path = os.path.join(base_path, file_path).replace("\\", "/")

                if status == "D":
                    deleted_files.append(abs_file_path)
                else:
                    files_to_analyze.append(abs_file_path)

    if not files_to_analyze and not deleted_files:
        return {
            "status": "success",
            "message": "No relevant git changes found.",
            "total_nodes": 0,
            "total_edges": 0,
        }

    logger.info(
        f"Git sync: {len(files_to_analyze)} files to analyze, {len(deleted_files)} files to delete."
    )

    pipeline_result: dict[str, Any] = run_analysis_pipeline(
        path=base_path,
        uri=Config.NEO4J_URI,
        user=Config.NEO4J_USER,
        password=Config.NEO4J_PASSWORD,
        clear_db=False,
        max_workers=max_workers,
        chunk_size=chunk_size,
        analysis_level=level_enum,
        language=language.lower(),
        specific_files=files_to_analyze,
        deleted_files=deleted_files,
    )

    return pipeline_result


def verify_graph_sync(project_id: str) -> dict[str, Any]:
    """Verify that the files indexed in Neo4j match the files tracked by Git.

    Returns:
        A report containing counts and examples of missing files (in Git but not Neo4j)
        and ghost files (in Neo4j but not in Git).
    """
    base_path = os.environ.get("PROJECT_PATH", "/workspace")
    logger.info(f"Verifying graph sync for project {project_id} in {base_path}")

    # 1. Get tracked files from Git
    try:
        git_result = subprocess.run(
            ["git", "ls-files"], cwd=base_path, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error(f"Failed to run git ls-files: {e}")
        return {"status": "error", "message": f"Failed to list git files: {e!s}"}

    git_files = set()
    supported_extensions = (".java", ".jsp", ".xml", ".properties", ".py")

    for line in git_result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.endswith(supported_extensions):
            abs_file_path = os.path.join(base_path, line).replace("\\", "/")
            git_files.add(abs_file_path)

    # 2. Get indexed files from Neo4j
    adapter = get_adapter()
    try:
        query_result = adapter.query(
            "MATCH (n:File {project_id: $project_id}) RETURN DISTINCT n.file_path AS file_path",
            project_id=project_id,
        )
        neo4j_files = {row.get("file_path") for row in query_result if row.get("file_path")}
    except Exception as e:
        logger.error(f"Failed to query Neo4j for verification: {e}")
        return {"status": "error", "message": f"Failed to query Neo4j: {e!s}"}

    # 3. Compare
    missing_files = git_files - neo4j_files
    ghost_files = neo4j_files - git_files

    return {
        "status": "success",
        "tracked_files_count": len(git_files),
        "indexed_files_count": len(neo4j_files),
        "missing_files_count": len(missing_files),
        "ghost_files_count": len(ghost_files),
        "missing_files_sample": list(missing_files)[:10],
        "ghost_files_sample": list(ghost_files)[:10],
        "is_synced": len(missing_files) == 0 and len(ghost_files) == 0,
    }
