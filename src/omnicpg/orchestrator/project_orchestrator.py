"""ProjectOrchestrator — core engine that drives the analysis pipeline.

Supports two modes of operation:

* **In-memory** (``analyze``) — collects all nodes and edges into lists.
  Convenient for small-to-medium projects.
* **Streaming** (``analyze_streaming``) — yields ``(nodes, edges)`` in
  configurable chunks so that the caller can persist each chunk and release
  memory.  Essential for projects with 20 000+ files.

Both modes support optional **parallel parsing** via a thread pool to
utilise multiple CPU cores for tree-sitter work (the C extension releases
the GIL).

The ``analysis_level`` parameter controls how deeply each file is parsed:

* ``FULL`` — full AST + CFG + DFG (default).
* ``ARCHITECTURAL`` — skeleton only (Module/Class/Method/Field + CALLS).
* ``STRUCTURAL`` — statement-level (no expression/literal nodes).
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from omnicpg.models.analysis_level import AnalysisLevel
from omnicpg.models.edge import CPGEdge, EdgeType

if TYPE_CHECKING:
    from collections.abc import Iterator

    from omnicpg.interfaces.language_plugin import LanguagePlugin
    from omnicpg.models.node import CPGNode

logger = logging.getLogger(__name__)

# Default number of files per chunk in streaming mode.
_DEFAULT_CHUNK_SIZE = 100

# Default number of parallel workers (1 = sequential, matching legacy behaviour).
_DEFAULT_MAX_WORKERS = 1

# Name-only call resolution is intentionally bounded: common Java method names
# such as get/set/toString can otherwise expand to caller x every same-named
# method in the project, creating millions of low-confidence edges.
_MAX_HEURISTIC_CALL_TARGETS = 32

# Upper bound on receiver-expression recursion (method-call / field-access
# chains).  Real-world fluent chains are short; this guards runaway recursion.
_MAX_RECEIVER_CHAIN_DEPTH = 8

_STREAMING_PARENT_TYPES = frozenset(
    {
        "argument_list",
        "formal_parameter",
        "formal_parameters",
        "parameters",
    }
)

_StreamingCallSite = tuple[str, str | None, str]

_JAVA_CLASS_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)


@dataclass
class _StreamingTypeIndex:
    """Lightweight Java-style type metadata retained across streaming chunks."""

    class_ids: set[str]
    class_by_simple: dict[str, list[str]]
    methods_by_class: dict[str, dict[str, list[str]]]
    method_enclosing_class: dict[str, str]
    fields_by_class: dict[str, dict[str, str]]
    vars_by_method: dict[str, dict[str, str]]
    call_receivers: dict[str, str]
    call_node_types: dict[str, str]
    # Per-class method return types (simple names) for chained-receiver
    # inference, raw super-type simple names per class, and the resolved
    # supertype class-id chain (populated once before resolution).
    return_types_by_class: dict[str, dict[str, str]]
    class_supertype_names: dict[str, list[str]]
    supertypes_by_class: dict[str, list[str]]
    # Disambiguation context for simple names shared by several classes:
    # class id → declaring package, and file path → {simple name → imported FQN}.
    package_by_class: dict[str, str]
    imports_by_file: dict[str, dict[str, str]]
    # Method simple name -> set of captured return-type simple names across all
    # classes; lets a chain whose root is unresolvable still infer its result
    # type when the tail getter has a single, project-wide return type.
    return_types_by_method: dict[str, set[str]] = field(default_factory=dict)


def _process_pool_worker(
    args: tuple[str, str, str],
) -> tuple[list[Any], list[Any]]:
    """Top-level worker function executed in a subprocess (must be picklable).

    Rebuilds the plugin from its fully-qualified class name so that the
    tree-sitter Language objects are created fresh inside the worker process
    (they cannot be pickled across process boundaries).

    Args:
        args: ``(file_path_str, plugin_class_qualname, analysis_level_value)``

    Returns:
        ``(nodes, edges)`` as plain lists (picklable).
    """
    import importlib

    file_path_str, plugin_qualname, level_value = args
    file_path = Path(file_path_str)

    # Re-import and instantiate the plugin inside the worker process.
    module_name, class_name = plugin_qualname.rsplit(".", 1)
    module = importlib.import_module(module_name)
    plugin_cls = getattr(module, class_name)
    plugin = plugin_cls()

    analysis_level = AnalysisLevel(level_value)

    encodings = ["utf-8", "gbk", "gb2312", "latin-1"]
    source_code = ""
    for enc in encodings:
        try:
            source_code = file_path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if not source_code:
        source_code = file_path.read_text(encoding="utf-8", errors="replace")

    ast_nodes, ast_edges = plugin.parse_to_ast(
        str(file_path), source_code, analysis_level=analysis_level
    )
    edges = list(ast_edges)

    if analysis_level == AnalysisLevel.ARCHITECTURAL:
        return ast_nodes, edges

    cfg_edges = plugin.build_cfg(ast_nodes, ast_edges)
    edges.extend(cfg_edges)

    if analysis_level == AnalysisLevel.STRUCTURAL:
        return ast_nodes, edges

    dfg_edges = plugin.build_dfg(ast_nodes, cfg_edges, ast_edges)
    edges.extend(dfg_edges)

    return ast_nodes, edges


def _read_file_with_fallback_encoding(file_path: Path) -> str:
    """Try multiple encodings to read a file.

    Attempts: utf-8, gbk, gb2312, latin-1.
    """
    encodings = ["utf-8", "gbk", "gb2312", "latin-1"]

    for encoding in encodings:
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    # Last resort: replace undecodable bytes.
    return file_path.read_text(encoding="utf-8", errors="replace")


class ProjectOrchestrator:
    """Scan a project directory and orchestrate CPG generation.

    The orchestrator is language-agnostic: it delegates actual parsing and
    graph construction to :class:`LanguagePlugin` instances injected at
    construction time.

    Args:
        plugins: One or more language plugins. The orchestrator uses each
            plugin's ``supported_extensions`` to decide which plugin handles
            which file.
        max_workers: Number of threads used for parallel file analysis.
            Defaults to ``1`` (sequential).  Higher values speed up
            CPU-bound tree-sitter parsing because the C extension releases
            the GIL.
        analysis_level: Desired granularity for CPG generation.  Defaults
            to ``FULL`` (every AST node).  Use ``ARCHITECTURAL`` for
            large-scale projects where only skeleton-level structure is
            needed.
    """

    def __init__(
        self,
        plugins: list[LanguagePlugin],
        max_workers: int = _DEFAULT_MAX_WORKERS,
        analysis_level: AnalysisLevel = AnalysisLevel.FULL,
    ) -> None:
        """Initialise the orchestrator with the given language plugins."""
        if not plugins:
            raise ValueError("At least one LanguagePlugin must be provided")
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        self._plugins = plugins
        self._max_workers = max_workers
        self._analysis_level = analysis_level
        self._extension_map: dict[str, LanguagePlugin] = {}
        for plugin in plugins:
            for ext in plugin.supported_extensions:
                if ext in self._extension_map:
                    logger.warning(
                        "Extension '%s' is already registered; overwriting with %s",
                        ext,
                        type(plugin).__name__,
                    )
                self._extension_map[ext] = plugin

    # ── Public API ────────────────────────────────────────────────────────

    def scan_directory(self, path: str) -> list[Path]:
        """Recursively collect files whose extensions match a registered plugin.

        Args:
            path: Root directory to scan.

        Returns:
            Sorted list of matching file paths.

        Raises:
            FileNotFoundError: If *path* does not exist.
            NotADirectoryError: If *path* is not a directory.
        """
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Directory or file not found: {path}")

        if root.is_file():
            raise ValueError(f"PROJECT_PATH must be a directory, got file: {path}")

        matched: list[Path] = []
        for file_path in root.rglob("*"):
            if file_path.is_file() and file_path.suffix in self._extension_map:
                matched.append(file_path)
        matched.sort()
        logger.info("Scanned %s — found %d matching files", path, len(matched))
        return matched

    def analyze(self, directory: str) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Run the full analysis pipeline on *directory*.

        Steps:
        1. Scan for supported files.
        2. For each file, route to the appropriate plugin.
        3. Generate AST, CFG, and DFG sub-graphs.
        4. Run cross-file call-graph analysis.
        5. Aggregate and return all nodes and edges.

        Args:
            directory: Root directory to analyze.

        Returns:
            A tuple of ``(all_nodes, all_edges)``.
        """
        files = self.scan_directory(directory)
        all_nodes: list[CPGNode] = []
        all_edges: list[CPGEdge] = []

        self._analyze_files(files, all_nodes, all_edges)

        # Cross-file call graph (runs over *all* nodes and edges from every file).
        call_edges = self._build_call_graph(all_nodes, all_edges)
        all_edges.extend(call_edges)

        # Cross-file inter-procedural DFG (binds arguments to parameters, etc.)
        inter_dfg_edges = self._build_interprocedural_dfg(all_nodes, all_edges)
        all_edges.extend(inter_dfg_edges)

        logger.info(
            "Analysis complete: %d nodes, %d edges",
            len(all_nodes),
            len(all_edges),
        )
        return all_nodes, all_edges

    def analyze_streaming(
        self,
        directory: str,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> Iterator[tuple[list[CPGNode], list[CPGEdge]]]:
        """Yield ``(nodes, edges)`` in chunks to bound memory usage.

        The method processes files in groups of *chunk_size*.  Each yielded
        tuple contains the AST, CFG and DFG results for that group.  After
        the caller has consumed every chunk, a **final** chunk is yielded
        containing only the cross-file call-graph edges (which require a
        lightweight definition index built incrementally across all chunks).

        Typical usage with a :class:`GraphDBAdapter`::

            for nodes, edges in orchestrator.analyze_streaming(directory):
                adapter.insert_nodes(nodes)
                adapter.insert_edges(edges)

        Args:
            directory: Root directory to analyze.
            chunk_size: Number of files per chunk.

        Yields:
            ``(nodes, edges)`` tuples. The last tuple contains only
            call-graph edges (its ``nodes`` list is empty).
        """
        files = self.scan_directory(directory)
        yield from self.analyze_streaming_files(files, chunk_size=chunk_size)

    def analyze_streaming_files(
        self,
        files: list[Path] | list[str],
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> Iterator[tuple[list[CPGNode], list[CPGEdge]]]:
        """Yield ``(nodes, edges)`` for an explicit file list.

        This is used by resume flows to process only pending files while
        preserving full streaming call-graph and inter-procedural DFG logic.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")

        normalized_files: list[Path] = []
        for file_path in files:
            path_obj = file_path if isinstance(file_path, Path) else Path(file_path)
            if path_obj.is_file() and path_obj.suffix in self._extension_map:
                normalized_files.append(path_obj)
        normalized_files.sort()

        # Lightweight accumulators for cross-file call-graph resolution.
        definition_index: dict[str, list[str]] = {}
        call_sites: list[_StreamingCallSite] = []
        node_file_map: dict[str, str] = {}
        child_to_parent: dict[str, str] = {}
        method_nodes: dict[str, str] = {}

        method_params: dict[str, list[str]] = {}
        method_returns: dict[str, list[str]] = {}
        call_args: dict[str, list[str]] = {}
        type_index = _StreamingTypeIndex(
            class_ids=set(),
            class_by_simple={},
            methods_by_class={},
            method_enclosing_class={},
            fields_by_class={},
            vars_by_method={},
            call_receivers={},
            call_node_types={},
            return_types_by_class={},
            return_types_by_method={},
            class_supertype_names={},
            supertypes_by_class={},
            package_by_class={},
            imports_by_file={},
        )

        total_nodes = 0
        total_edges = 0
        total_files = len(normalized_files)
        total_chunks = (total_files + chunk_size - 1) // chunk_size if total_files else 0
        processed_files = 0
        t_start = time.monotonic()

        for chunk_index, start in enumerate(range(0, len(normalized_files), chunk_size), 1):
            chunk_files = normalized_files[start : start + chunk_size]
            chunk_nodes: list[CPGNode] = []
            chunk_edges: list[CPGEdge] = []

            t_chunk = time.monotonic()
            self._analyze_files(chunk_files, chunk_nodes, chunk_edges)
            chunk_elapsed = time.monotonic() - t_chunk

            self._update_call_graph_index(
                chunk_nodes,
                chunk_edges,
                definition_index,
                call_sites,
                node_file_map,
                child_to_parent,
                method_nodes,
                method_params,
                method_returns,
                call_args,
                type_index,
            )

            total_nodes += len(chunk_nodes)
            total_edges += len(chunk_edges)
            processed_files += len(chunk_files)
            pct = (processed_files / total_files * 100) if total_files else 100.0
            logger.info(
                "Streaming chunk %d/%d: %d files → %d nodes, %d edges "
                "(%.1fs) [%.0f%% — %d/%d files, cumulative: %d/%d]",
                chunk_index,
                total_chunks,
                len(chunk_files),
                len(chunk_nodes),
                len(chunk_edges),
                chunk_elapsed,
                pct,
                processed_files,
                total_files,
                total_nodes,
                total_edges,
            )
            yield chunk_nodes, chunk_edges

        call_edges = self._build_call_graph_from_index(
            definition_index,
            call_sites,
            node_file_map,
            child_to_parent,
            method_nodes,
            type_index,
        )
        if call_edges:
            total_edges += len(call_edges)
            logger.info("Streaming: yielding %d call-graph edges", len(call_edges))
            yield [], call_edges

        inter_dfg_edges = self._build_interprocedural_dfg_from_index(
            call_edges,
            method_params,
            method_returns,
            call_args,
        )
        if inter_dfg_edges:
            total_edges += len(inter_dfg_edges)
            logger.info("Streaming: yielding %d inter-procedural DFG edges", len(inter_dfg_edges))
            yield [], inter_dfg_edges

        logger.info(
            "Streaming analysis complete: %d nodes, %d edges total (%.1fs elapsed)",
            total_nodes,
            total_edges,
            time.monotonic() - t_start,
        )

    # ── Private helpers ───────────────────────────────────────────────────

    def _route_file(self, path: Path) -> LanguagePlugin | None:
        """Return the plugin registered for *path*'s extension, or ``None``."""
        return self._extension_map.get(path.suffix)

    def _analyze_single_file(
        self,
        file_path: Path,
    ) -> tuple[list[CPGNode], list[CPGEdge]]:
        """Analyse a single file and return ``(nodes, edges)``.

        This is the unit of work submitted to the thread pool.
        """
        plugin = self._route_file(file_path)
        if plugin is None:
            return [], []  # pragma: no cover — defensive; scan already filters

        source_code = _read_file_with_fallback_encoding(file_path)
        logger.info("Analyzing %s with %s", file_path, type(plugin).__name__)

        # 1. AST — pass analysis_level so the builder can prune.
        ast_nodes, ast_edges = plugin.parse_to_ast(
            str(file_path),
            source_code,
            analysis_level=self._analysis_level,
        )

        edges: list[CPGEdge] = []
        edges.extend(ast_edges)

        # In ARCHITECTURAL mode, skip CFG / DFG — method internals are not
        # expanded into individual nodes.
        if self._analysis_level == AnalysisLevel.ARCHITECTURAL:
            return ast_nodes, edges

        # 2. CFG — pass ast_edges so the builder can use the PARENT_OF index
        #    for O(1) child lookups instead of O(n) full-list scans.
        t_cfg = time.monotonic()
        cfg_edges = plugin.build_cfg(ast_nodes, ast_edges)
        cfg_elapsed = time.monotonic() - t_cfg
        logger.info(
            "CFG for %s: %d FLOWS_TO edges (%.2fs)",
            file_path,
            len(cfg_edges),
            cfg_elapsed,
        )
        edges.extend(cfg_edges)

        # In STRUCTURAL mode, skip DFG — expression-level detail is pruned.
        if self._analysis_level == AnalysisLevel.STRUCTURAL:
            return ast_nodes, edges

        # 3. DFG — likewise pass ast_edges for fast scope resolution.
        t_dfg = time.monotonic()
        dfg_edges = plugin.build_dfg(ast_nodes, cfg_edges, ast_edges)
        dfg_elapsed = time.monotonic() - t_dfg
        logger.info(
            "DFG for %s: %d REACHES edges (%.2fs)",
            file_path,
            len(dfg_edges),
            dfg_elapsed,
        )
        edges.extend(dfg_edges)

        return ast_nodes, edges

    def _analyze_files(
        self,
        files: list[Path],
        out_nodes: list[CPGNode],
        out_edges: list[CPGEdge],
    ) -> None:
        """Analyse *files* (possibly in parallel) and append results.

        Two parallel strategies are supported, selected via the
        ``USE_PROCESS_POOL`` environment variable:

        * ``USE_PROCESS_POOL=true`` — ``ProcessPoolExecutor``.  Each worker
          subprocess is GIL-free, giving true parallelism for CPU-bound
          CFG/DFG Python code.  Plugin objects are rebuilt inside each worker
          (tree-sitter Language objects are not picklable).  Recommended for
          ``FULL`` analysis of large projects on multi-core hosts.

        * Default (``USE_PROCESS_POOL`` unset or ``false``) —
          ``ThreadPoolExecutor``.  Lower process-spawn overhead; effective
          when tree-sitter C parsing dominates (``ARCHITECTURAL`` mode) since
          the C extension releases the GIL.
        """
        if self._max_workers <= 1 or len(files) <= 1:
            # Sequential path — no pool overhead.
            for file_path in files:
                nodes, edges = self._analyze_single_file(file_path)
                out_nodes.extend(nodes)
                out_edges.extend(edges)
            return

        use_process_pool = os.environ.get("USE_PROCESS_POOL", "false").lower() == "true"

        if use_process_pool:
            # Build a per-file plugin class map so each worker can reconstruct
            # its plugin without pickling the live plugin object.
            plugin_qualname_for: dict[str, str] = {}
            for fp in files:
                plugin = self._route_file(fp)
                if plugin is not None:
                    cls = type(plugin)
                    plugin_qualname_for[str(fp)] = f"{cls.__module__}.{cls.__qualname__}"

            worker_args = [
                (str(fp), plugin_qualname_for[str(fp)], self._analysis_level.value)
                for fp in files
                if str(fp) in plugin_qualname_for
            ]
            with ProcessPoolExecutor(max_workers=self._max_workers) as pool:
                for nodes, edges in pool.map(_process_pool_worker, worker_args):
                    out_nodes.extend(nodes)
                    out_edges.extend(edges)
        else:
            # Thread pool — lower spawn cost; GIL released by tree-sitter C ext.
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                future_to_path = {pool.submit(self._analyze_single_file, fp): fp for fp in files}
                for future in as_completed(future_to_path):
                    nodes, edges = future.result()
                    out_nodes.extend(nodes)
                    out_edges.extend(edges)

    def _build_call_graph(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge] | None = None,
    ) -> list[CPGEdge]:
        """Run cross-file call-graph analysis using all registered plugins.

        Each plugin is given the complete set of nodes and edges so it
        can resolve call-sites to their enclosing methods via the AST
        ``PARENT_OF`` edges.
        """
        call_edges: list[CPGEdge] = []
        seen_plugins: set[int] = set()
        for plugin in self._plugins:
            pid = id(plugin)
            if pid in seen_plugins:
                continue
            seen_plugins.add(pid)
            edges = plugin.build_call_graph(all_nodes, all_edges)
            call_edges.extend(edges)
        return call_edges

    def _build_interprocedural_dfg(
        self,
        all_nodes: list[CPGNode],
        all_edges: list[CPGEdge],
    ) -> list[CPGEdge]:
        """Run inter-procedural data-flow analysis across all registered plugins.

        This connects arguments at call sites to parameters in called methods,
        relying on the CALLS edges produced by _build_call_graph.
        """
        inter_dfg_edges: list[CPGEdge] = []
        seen_plugins: set[int] = set()
        for plugin in self._plugins:
            pid = id(plugin)
            if pid in seen_plugins:
                continue
            seen_plugins.add(pid)
            edges = plugin.build_interprocedural_dfg(all_nodes, all_edges)
            inter_dfg_edges.extend(edges)
        return inter_dfg_edges

    # ── Streaming call-graph helpers ─────────────────────────────────────

    @staticmethod
    def _update_call_graph_index(
        nodes: list[CPGNode],
        edges: list[CPGEdge],
        definition_index: dict[str, list[str]],
        call_sites: list[_StreamingCallSite],
        node_file_map: dict[str, str],
        child_to_parent: dict[str, str],
        method_nodes: dict[str, str],
        method_params: dict[str, list[str]],
        method_returns: dict[str, list[str]],
        call_args: dict[str, list[str]],
        type_index: _StreamingTypeIndex,
    ) -> None:
        """Incrementally update the call-graph index from a chunk's nodes and edges.

        Only stores the minimal metadata needed for call-graph resolution,
        not the full node objects.
        """
        parent_container_types: dict[str, str] = {}

        for node in nodes:
            file_path = str(node.properties.get("file_path", ""))
            node_type = str(node.properties.get("type", ""))

            if node_type in _STREAMING_PARENT_TYPES:
                parent_container_types[node.id] = node_type

            # Track Java class-like declarations for receiver-type resolution.
            if node_type in _JAVA_CLASS_TYPES:
                name = node.properties.get("name")
                if name is not None:
                    type_index.class_ids.add(node.id)
                    type_index.class_by_simple.setdefault(str(name), []).append(node.id)
                    node_file_map[node.id] = file_path
                    type_index.package_by_class[node.id] = str(node.properties.get("package", ""))
                    base_names: list[str] = []
                    superclass = node.properties.get("superclass")
                    if superclass:
                        base_names.append(_simple_type_name(str(superclass)))
                    for base in node.properties.get("base_classes", ()) or ():
                        base_names.append(_simple_type_name(str(base)))
                    if base_names:
                        type_index.class_supertype_names[node.id] = base_names

            # Track imports for ambiguous simple-name disambiguation.
            if node.has_label("Import"):
                fqn = _extract_import_fqn(str(node.properties.get("code", "")))
                if fqn and "." in fqn:
                    simple = fqn.rsplit(".", 1)[1]
                    if simple != "*":
                        type_index.imports_by_file.setdefault(file_path, {})[simple] = fqn

            # Track definitions.
            if node.has_label("Method"):
                name = node.properties.get("name")
                if name is not None:
                    name_str = str(name)
                    definition_index.setdefault(name_str, []).append(node.id)
                    node_file_map[node.id] = file_path
                    method_nodes[node.id] = name_str

            # Track call sites.
            if node_type in (
                "call",
                "method_invocation",
                "object_creation_expression",
                "method_reference",
            ):
                callee = _extract_callee_name_from_node(node)
                call_sites.append((node.id, callee, file_path))
                type_index.call_node_types[node.id] = node_type
                receiver = node.properties.get("receiver")
                if isinstance(receiver, str) and receiver.strip():
                    type_index.call_receivers[node.id] = receiver.strip()

        # Build child→parent map from structural edges.
        for edge in edges:
            if edge.edge_type in (EdgeType.PARENT_OF, EdgeType.CONTAINS):
                child_to_parent.setdefault(edge.target_id, edge.source_id)

        # Second pass for DFG metadata (parameters, returns, arguments)
        for node in nodes:
            ntype = str(node.properties.get("type", ""))
            pid = child_to_parent.get(node.id)
            if not pid:
                continue

            # Bound identifiers in parameters
            if ntype == "identifier":
                ptype = parent_container_types.get(pid, "")
                if ptype in ("parameters", "formal_parameters"):
                    # Direct parent is the container (Python style or top-level)
                    method_id = child_to_parent.get(pid)
                    if method_id:
                        method_params.setdefault(method_id, []).append(node.id)
                elif ptype == "formal_parameter":
                    # Java style: identifier -> formal_parameter -> formal_parameters -> Method.
                    # The intra-procedural Java DFG treats the formal_parameter
                    # node as the definition, so inter-procedural edges must
                    # target that node rather than the parameter-name child.
                    container_id = child_to_parent.get(pid)
                    if container_id:
                        method_id = child_to_parent.get(container_id)
                        if method_id:
                            method_params.setdefault(method_id, []).append(pid)

            # Check for argument_list parent (more generic than identifier)
            if ntype not in ("(", ")", ",") and parent_container_types.get(pid) == "argument_list":
                call_id = child_to_parent.get(pid)
                if call_id:
                    call_args.setdefault(call_id, []).append(node.id)

            # Collect returns
            if ntype == "return_statement":
                # Find enclosing Method. This might be deep.
                curr: str | None = pid
                for _ in range(10):  # Depth limit for performance
                    if curr in method_nodes:
                        method_returns.setdefault(curr, []).append(node.id)
                        break
                    if curr is None:
                        break
                    curr = child_to_parent.get(curr)

        # Third pass for lightweight Java-style type metadata used to narrow
        # streaming CALLS edges from name-only matches to receiver-class matches.
        for node in nodes:
            ntype = str(node.properties.get("type", ""))
            if node.id in method_nodes:
                class_id = _find_enclosing_indexed_node(
                    node.id,
                    child_to_parent,
                    type_index.class_ids,
                )
                if class_id is not None:
                    type_index.method_enclosing_class[node.id] = class_id
                    type_index.methods_by_class.setdefault(class_id, {}).setdefault(
                        method_nodes[node.id], []
                    ).append(node.id)
                    return_type = node.properties.get("return_type")
                    if return_type:
                        simple_rt = _simple_type_name(str(return_type))
                        type_index.return_types_by_class.setdefault(class_id, {})[
                            method_nodes[node.id]
                        ] = simple_rt
                        type_index.return_types_by_method.setdefault(
                            method_nodes[node.id], set()
                        ).add(simple_rt)
                continue

            if ntype == "field_declaration":
                class_id = _find_enclosing_indexed_node(
                    node.id,
                    child_to_parent,
                    type_index.class_ids,
                )
                declared_type = node.properties.get("declared_type")
                if class_id is not None and declared_type:
                    simple_type = _simple_type_name(str(declared_type))
                    for var_name in node.properties.get("var_names", ()) or ():
                        type_index.fields_by_class.setdefault(class_id, {})[str(var_name)] = (
                            simple_type
                        )
                continue

            if ntype in (
                "local_variable_declaration",
                "enhanced_for_statement",
                "catch_formal_parameter",
            ):
                method_id = _find_enclosing_indexed_node(
                    node.id,
                    child_to_parent,
                    method_nodes,
                )
                declared_type = node.properties.get("declared_type")
                if method_id is not None and declared_type:
                    simple_type = _simple_type_name(str(declared_type))
                    for var_name in node.properties.get("var_names", ()) or ():
                        type_index.vars_by_method.setdefault(method_id, {})[str(var_name)] = (
                            simple_type
                        )
                continue

            if ntype == "formal_parameter":
                method_id = _find_enclosing_indexed_node(
                    node.id,
                    child_to_parent,
                    method_nodes,
                )
                if method_id is not None:
                    name, type_name = _parse_formal_parameter_code(
                        str(node.properties.get("code", ""))
                    )
                    if name and type_name:
                        type_index.vars_by_method.setdefault(method_id, {})[name] = (
                            _simple_type_name(type_name)
                        )

    @staticmethod
    def _build_call_graph_from_index(
        definition_index: dict[str, list[str]],
        call_sites: list[_StreamingCallSite],
        node_file_map: dict[str, str],
        child_to_parent: dict[str, str] | None = None,
        method_nodes: dict[str, str] | None = None,
        type_index: _StreamingTypeIndex | None = None,
    ) -> list[CPGEdge]:
        """Build call-graph edges from the lightweight accumulated index.

        When *child_to_parent* and *method_nodes* are provided, the builder
        resolves each call-site to its enclosing Method and emits
        **Method → Method** edges.  Otherwise falls back to the raw
        call-site node as source.
        """
        if child_to_parent is None:
            child_to_parent = {}
        if method_nodes is None:
            method_nodes = {}

        if type_index is not None and type_index.class_supertype_names:
            _resolve_streaming_supertypes(type_index)

        edges: list[CPGEdge] = []
        seen: set[tuple[str, str]] = set()

        for call_node_id, callee_name, caller_file in call_sites:
            if callee_name is None:
                continue

            # Resolve call-site → enclosing Method.
            source_id = call_node_id
            caller_name: str | None = None
            caller_method_file = caller_file
            current = call_node_id
            walked: set[str] = {current}
            while True:
                parent_id = child_to_parent.get(current)
                if parent_id is None or parent_id in walked:
                    break
                if parent_id in method_nodes:
                    source_id = parent_id
                    caller_name = method_nodes[parent_id]
                    caller_method_file = node_file_map.get(parent_id, caller_file)
                    break
                walked.add(parent_id)
                current = parent_id

            targets, resolution = _resolve_streaming_targets(
                call_node_id,
                callee_name,
                source_id,
                definition_index,
                type_index,
                node_file_map,
                caller_method_file,
            )
            for target_id in targets:
                if source_id == target_id:
                    continue
                pair = (source_id, target_id)
                if pair in seen:
                    continue
                seen.add(pair)

                props: dict[str, str] = {
                    "callee": callee_name,
                    "caller_file": caller_method_file,
                    "target_file": node_file_map.get(target_id, ""),
                    "callsite_id": call_node_id,
                    "call_node_id": call_node_id,
                    "resolution": resolution,
                }
                if caller_name:
                    props["caller"] = caller_name
                if (
                    type_index
                    and type_index.call_node_types.get(call_node_id) == "method_reference"
                ):
                    props["call_kind"] = "method_reference"

                edges.append(
                    CPGEdge(
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=EdgeType.CALLS,
                        properties=MappingProxyType(props),
                    )
                )
        logger.info("Streaming call graph: generated %d CALLS edges", len(edges))
        return edges

    @staticmethod
    def _build_interprocedural_dfg_from_index(
        call_edges: list[CPGEdge],
        method_params: dict[str, list[str]],
        method_returns: dict[str, list[str]],
        call_args: dict[str, list[str]],
    ) -> list[CPGEdge]:
        """Build inter-procedural REACHES edges using the lightweight index."""
        inter_edges: list[CPGEdge] = []

        # Iterate through CALLS edges directly
        for edge in call_edges:
            target_method_id = edge.target_id
            params = method_params.get(target_method_id, [])
            returns = method_returns.get(target_method_id, [])

            # Find the CALL node. If source is a Method, we search for calls inside.
            # In streaming mode, we don't have the full tree in memory.
            # HOWEVER, we can store 'call_node_id' in the CALLS edge properties!
            # Let's update _build_call_graph_from_index to include this.
            call_node_id = edge.properties.get("call_node_id") or edge.properties.get(
                "callsite_id"
            )
            if not call_node_id:
                continue

            # A. Arguments -> Parameters
            args = call_args.get(call_node_id, [])
            for i, arg_id in enumerate(args):
                if i < len(params):
                    param_id = params[i]
                    inter_edges.append(
                        CPGEdge(
                            source_id=arg_id,
                            target_id=param_id,
                            edge_type=EdgeType.REACHES,
                            properties=MappingProxyType({"interprocedural": "argument"}),
                        )
                    )

            # B. Returns -> Call
            for ret_id in returns:
                inter_edges.append(
                    CPGEdge(
                        source_id=ret_id,
                        target_id=call_node_id,
                        edge_type=EdgeType.REACHES,
                        properties=MappingProxyType({"interprocedural": "return"}),
                    )
                )

        return inter_edges


def _extract_callee_name_from_node(node: CPGNode) -> str | None:
    """Extract a callee name from a call-site node.

    Java AST nodes already expose a structured ``name`` property for
    ``method_invocation`` and ``object_creation_expression`` nodes.  Prefer it
    over string parsing so streaming call-graph construction keeps the same
    call-site identity that the full Java V2 builder uses.
    """
    node_type = str(node.properties.get("type", ""))
    name = node.properties.get("name")
    if isinstance(name, str) and name.strip():
        callee = name.strip()
        if node_type == "object_creation_expression":
            callee = callee.split("<", maxsplit=1)[0].rsplit(".", maxsplit=1)[-1]
        if callee.isidentifier():
            return callee

    return _extract_callee_name_from_code(str(node.properties.get("code", "")))


def _looks_like_type_name(text: str) -> bool:
    """True when *text* is a plain (possibly qualified/generic/array) type name."""
    base = text.strip()
    while base.endswith("[]"):
        base = base[:-2].strip()
    if "<" in base:
        base = base[: base.index("<")].strip()
    if not base:
        return False
    return all(part.isidentifier() for part in base.split("."))


def _extract_cast_target_type(receiver: str) -> str | None:
    """Return the cast target type of a leading cast expression, else ``None``.

    Legacy generics-free Java pervasively casts collection elements before
    invoking methods, e.g. ``((FzItemDto) coll.get(0)).getVal()`` whose
    receiver text is ``((FzItemDto) coll.get(0))``.  The cast target type
    (``FzItemDto``) is the receiver's static type, so it can drive type-aware
    resolution instead of a name-only match.  Redundant wrapping parentheses
    are unwrapped first.
    """
    text = receiver.strip()
    if not text.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner = text[1:i].strip()
                rest = text[i + 1 :].strip()
                if not rest:
                    # Whole expression wrapped in parentheses — unwrap & retry.
                    return _extract_cast_target_type(inner)
                # ``(<inner>) <rest>`` is a cast iff ``inner`` is a type name.
                return inner if _looks_like_type_name(inner) else None
    return None


def _is_reflection_receiver(receiver: str | None) -> bool:
    """True when *receiver* statically evaluates to a ``java.lang.Class``.

    Covers the two ubiquitous reflection shapes ``Foo.class`` and
    ``<expr>.getClass()`` (``getClass`` is final on ``java.lang.Object``).
    Methods invoked on such a receiver (``getName``, ``getSimpleName`` …) are
    always external, so they must never be linked to same-named project
    methods.
    """
    if receiver is None:
        return False
    text = receiver.rstrip()
    return text.endswith(".class") or text.endswith("getClass()")


def _resolve_streaming_targets(
    call_node_id: str,
    callee_name: str,
    source_method_id: str,
    definition_index: dict[str, list[str]],
    type_index: _StreamingTypeIndex | None,
    node_file_map: dict[str, str] | None = None,
    caller_file: str = "",
) -> tuple[list[str], str]:
    """Resolve streaming call targets, preferring receiver-class matches."""
    if type_index is None:
        return _resolve_heuristic_streaming_targets(
            callee_name,
            definition_index,
            node_file_map,
            caller_file,
        )

    target_class_id: str | None = None
    call_type = type_index.call_node_types.get(call_node_id, "")
    receiver = type_index.call_receivers.get(call_node_id)
    caller_class_id = type_index.method_enclosing_class.get(source_method_id)

    # ``Foo.class.getName()`` / ``x.getClass().getName()`` and friends invoke
    # ``java.lang.Class`` reflection methods, which are always external. Never
    # link them to same-named project methods (this would explode to every
    # ``getName`` in the codebase).
    if call_type != "object_creation_expression" and _is_reflection_receiver(receiver):
        return [], "typed"

    if call_type == "object_creation_expression":
        target_class_id = _resolve_streaming_class_of_type(
            callee_name, type_index, caller_file, caller_class_id=None
        )
    elif receiver is None or receiver in {"this", "super"}:
        target_class_id = type_index.method_enclosing_class.get(source_method_id)
    else:
        target_class_id = _infer_streaming_receiver_class(
            receiver, source_method_id, caller_class_id, type_index, caller_file
        )

    if target_class_id is not None:
        targets = _resolve_streaming_in_hierarchy(target_class_id, callee_name, type_index)
        if targets:
            return targets, "typed"
        # Receiver type is known but the method isn't in the captured hierarchy
        # (e.g. a base class outside the analysis scope). Constrain candidates to
        # the receiver type's own name-based hierarchy instead of exploding to a
        # global name match, which would emit many false edges.
        allowed = _streaming_name_ancestor_ids(target_class_id, type_index)
        constrained = [
            target
            for target in definition_index.get(callee_name, [])
            if type_index.method_enclosing_class.get(target) in allowed
        ]
        return constrained, "typed"

    # Receiver's declared type name is known but its class could not be pinned
    # down above. Two sub-cases, both of which avoid the global name match:
    #   * the type was never analysed (no class with that simple name) -> the
    #     call leaves the analysis scope, so suppress it entirely; and
    #   * the simple name is ambiguous (several same-named classes in scope, so
    #     disambiguation failed) -> constrain candidates to the union of those
    #     classes' name-based hierarchies instead of exploding to every
    #     same-named method in the project.
    if receiver is not None and receiver not in {"this", "super"}:
        type_name = _streaming_receiver_type_name(
            receiver, source_method_id, caller_class_id, type_index
        )
        if type_name:
            candidate_classes = type_index.class_by_simple.get(type_name)
            if not candidate_classes:
                return [], "typed"
            allowed_classes: set[str] = set()
            for cid in candidate_classes:
                allowed_classes |= _streaming_name_ancestor_ids(cid, type_index)
            constrained = [
                target
                for target in definition_index.get(callee_name, [])
                if type_index.method_enclosing_class.get(target) in allowed_classes
            ]
            return constrained, "typed"

    # ``new Foo(...)`` whose simple name is ambiguous (several same-named classes
    # in scope) could not be pinned to a single class above. The call is still a
    # constructor of a known-in-scope type, so constrain to the candidate
    # classes' own constructors and label it typed rather than exploding to every
    # same-named method in the project.
    if call_type == "object_creation_expression":
        candidate_classes = type_index.class_by_simple.get(callee_name)
        if candidate_classes:
            allowed_ctor: set[str] = set(candidate_classes)
            constrained = [
                target
                for target in definition_index.get(callee_name, [])
                if type_index.method_enclosing_class.get(target) in allowed_ctor
            ]
            return constrained, "typed"

    return _resolve_heuristic_streaming_targets(
        callee_name,
        definition_index,
        node_file_map,
        caller_file,
    )


def _streaming_receiver_type_name(
    receiver: str,
    source_method_id: str,
    caller_class_id: str | None,
    type_index: _StreamingTypeIndex,
) -> str | None:
    """Return the declared simple type name of a *receiver* expression.

    Resolves the three common shapes whose type is statically knowable even
    when the type's class was not analysed:

    * ``new Foo(...)`` → ``Foo``;
    * a bare ``ident`` → its local-variable / parameter / field type;
    * a single field access ``obj.field`` → the field's declared type; and
    * a method-call chain ``a.getB()`` → ``getB``'s declared return type, when
      the chain root resolves, or — failing that — ``getB``'s project-wide
      return type when it is unambiguous.

    Returns ``None`` when the type cannot be inferred (e.g. an uncaptured
    return type), where suppression would be unsafe.
    """
    text = receiver.strip()
    if text.startswith("new "):
        head = text[4:].split("(", 1)[0].split("[", 1)[0].strip()
        return _simple_type_name(head) if head else None
    # ``((Foo) expr)`` casts expose their target type for suppression too.
    cast_type = _extract_cast_target_type(text)
    if cast_type is not None:
        return _simple_type_name(cast_type)
    if text.isidentifier():
        declared = type_index.vars_by_method.get(source_method_id, {}).get(text)
        if declared is None and caller_class_id is not None:
            declared = type_index.fields_by_class.get(caller_class_id, {}).get(text)
        return declared
    # Method-call chain ``a.getB()`` → the declared return type of ``getB``.
    if text.endswith(")"):
        call_head = _strip_trailing_call_args(text)
        if call_head is None:
            return None
        if "." in call_head:
            obj, method = call_head.rsplit(".", 1)
            obj_class = _infer_streaming_receiver_class(
                obj, source_method_id, caller_class_id, type_index
            )
        else:
            method, obj_class = call_head, caller_class_id
        if not method.isidentifier():
            return None
        if obj_class is not None:
            resolved = _lookup_streaming_return_type(obj_class, method, type_index)
            if resolved is not None:
                return resolved
        # Root unresolvable (e.g. an ambiguously-named declared type): fall back
        # to the project-wide return type when ``method`` has exactly one.
        return _global_return_type(method, type_index)
    if "." in text:
        obj, member = text.rsplit(".", 1)
        if member.isidentifier():
            obj_class = _infer_streaming_receiver_class(
                obj, source_method_id, caller_class_id, type_index
            )
            if obj_class is not None:
                return _lookup_streaming_field_type(obj_class, member, type_index)
    return None


def _streaming_name_ancestor_ids(
    target_class_id: str,
    type_index: _StreamingTypeIndex,
) -> set[str]:
    """Return *target*'s name-based super-type closure (existing classes only).

    Walks ``class_supertype_names`` (raw super-type simple names captured from
    the ``superclass``/``base_classes`` properties) so the closure includes
    bases that were dropped from ``supertypes_by_class`` due to ambiguity, while
    naturally excluding bases that were never analysed.
    """
    result = {target_class_id}
    stack = [target_class_id]
    while stack:
        cid = stack.pop()
        for base_name in type_index.class_supertype_names.get(cid, []):
            for candidate in type_index.class_by_simple.get(base_name, []):
                if candidate not in result:
                    result.add(candidate)
                    stack.append(candidate)
    return result


def _resolve_streaming_supertypes(type_index: _StreamingTypeIndex) -> None:
    """Resolve raw super-type simple names to unambiguous class ids (once)."""
    for class_id, base_names in type_index.class_supertype_names.items():
        supers: list[str] = []
        for base_name in base_names:
            candidates = type_index.class_by_simple.get(base_name, [])
            if len(candidates) == 1 and candidates[0] != class_id:
                supers.append(candidates[0])
        if supers:
            type_index.supertypes_by_class[class_id] = supers


def _resolve_streaming_in_hierarchy(
    target_class_id: str,
    callee_name: str,
    type_index: _StreamingTypeIndex,
) -> list[str]:
    """Resolve *callee_name* against *target_class_id*, walking up supertypes.

    Mirrors Java inheritance: the nearest declaration on each supertype branch
    wins.  Returns the receiver class' own declaration when present, otherwise
    the closest inherited one.
    """
    seen: set[str] = set()
    stack = [target_class_id]
    targets: list[str] = []
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        found = type_index.methods_by_class.get(cid, {}).get(callee_name)
        if found:
            targets.extend(found)
            continue  # nearest definition on this branch wins
        stack.extend(type_index.supertypes_by_class.get(cid, []))
    return list(dict.fromkeys(targets))


def _infer_streaming_receiver_class(
    expr: str,
    source_method_id: str,
    caller_class_id: str | None,
    type_index: _StreamingTypeIndex,
    caller_file: str = "",
    depth: int = 0,
) -> str | None:
    """Infer the class id a receiver *expression* refers to (streaming variant).

    Handles bare identifiers (locals / params / fields / static class names),
    ``new Foo(...)``, method-call chains (``a.getB().getC()`` via declared
    return types) and field-access chains (``a.b.c``).  Returns ``None`` when
    the type cannot be inferred unambiguously.
    """
    if depth > _MAX_RECEIVER_CHAIN_DEPTH:
        return None
    text = expr.strip()
    if not text:
        return None
    if text in {"this", "super"}:
        return caller_class_id
    # ``((Foo) expr).m()`` — the cast target type is the receiver's static type.
    cast_type = _extract_cast_target_type(text)
    if cast_type is not None:
        return _resolve_streaming_class_of_type(
            cast_type, type_index, caller_file, caller_class_id
        )
    if text.startswith("new "):
        head = text[4:].split("(", 1)[0].split("[", 1)[0].strip()
        if not head:
            return None
        return _resolve_streaming_class_of_type(head, type_index, caller_file, caller_class_id)

    if text.isidentifier():
        declared = type_index.vars_by_method.get(source_method_id, {}).get(text)
        if declared is None and caller_class_id is not None:
            declared = type_index.fields_by_class.get(caller_class_id, {}).get(text)
        if declared is not None:
            return _resolve_streaming_class_of_type(
                declared, type_index, caller_file, caller_class_id
            )
        return _resolve_streaming_class_of_type(text, type_index, caller_file, caller_class_id)

    # Method-call chain: strip the trailing balanced ``(...)`` group.
    if text.endswith(")"):
        call_head = _strip_trailing_call_args(text)
        if call_head is None:
            return None
        if "." in call_head:
            obj, method = call_head.rsplit(".", 1)
            obj_class = _infer_streaming_receiver_class(
                obj, source_method_id, caller_class_id, type_index, caller_file, depth + 1
            )
        else:
            method, obj_class = call_head, caller_class_id
        if obj_class is None or not method.isidentifier():
            return None
        return_type = _lookup_streaming_return_type(obj_class, method, type_index)
        if not return_type:
            return None
        return _resolve_streaming_class_of_type(
            return_type, type_index, caller_file, caller_class_id
        )

    # Field-access chain: ``<expr>.field``.
    if "." in text:
        obj, member = text.rsplit(".", 1)
        if not member.isidentifier():
            return None
        obj_class = _infer_streaming_receiver_class(
            obj, source_method_id, caller_class_id, type_index, caller_file, depth + 1
        )
        if obj_class is None:
            return None
        field_type = _lookup_streaming_field_type(obj_class, member, type_index)
        if not field_type:
            return None
        return _resolve_streaming_class_of_type(
            field_type, type_index, caller_file, caller_class_id
        )

    return None


def _resolve_streaming_class_of_type(
    type_name: str | None,
    type_index: _StreamingTypeIndex,
    caller_file: str = "",
    caller_class_id: str | None = None,
) -> str | None:
    """Resolve a (possibly qualified/generic) type name to a unique class id.

    Disambiguates simple names shared by several classes using the caller's
    file imports, then its package, before giving up.
    """
    if not type_name:
        return None
    simple = _simple_type_name(str(type_name))
    classes = type_index.class_by_simple.get(simple, [])
    if not classes:
        return None
    if len(classes) == 1:
        return classes[0]
    return _disambiguate_streaming_classes(
        simple, classes, type_index, caller_file, caller_class_id
    )


def _disambiguate_streaming_classes(
    simple: str,
    classes: list[str],
    type_index: _StreamingTypeIndex,
    caller_file: str,
    caller_class_id: str | None,
) -> str | None:
    """Pick one class among same-simple-name candidates via imports / package."""
    # 1. Explicit import in the caller's file wins.
    imported_fqn = type_index.imports_by_file.get(caller_file, {}).get(simple)
    if imported_fqn and "." in imported_fqn:
        imported_pkg = imported_fqn.rsplit(".", 1)[0]
        matches = [
            cid for cid in classes if type_index.package_by_class.get(cid, "") == imported_pkg
        ]
        if len(matches) == 1:
            return matches[0]
    # 2. Same-package candidate.
    if caller_class_id is not None:
        caller_pkg = type_index.package_by_class.get(caller_class_id)
        if caller_pkg is not None:
            matches = [
                cid for cid in classes if type_index.package_by_class.get(cid, "") == caller_pkg
            ]
            if len(matches) == 1:
                return matches[0]
    return None


def _lookup_streaming_return_type(
    class_id: str,
    method: str,
    type_index: _StreamingTypeIndex,
) -> str | None:
    """Return *method*'s declared return type, walking up the supertype chain."""
    seen: set[str] = set()
    stack = [class_id]
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        found = type_index.return_types_by_class.get(cid, {}).get(method)
        if found is not None:
            return found
        stack.extend(type_index.supertypes_by_class.get(cid, []))
    return None


def _global_return_type(
    method: str,
    type_index: _StreamingTypeIndex,
) -> str | None:
    """Return *method*'s project-wide return type when it is unambiguous.

    Used as a last resort for chains whose root cannot be resolved (e.g. the
    declared type name is shared by several classes). When ``method`` has a
    single captured return type across the whole project, that type is a safe
    basis for suppression / constraint; otherwise returns ``None``.
    """
    returns = type_index.return_types_by_method.get(method)
    if returns is not None and len(returns) == 1:
        return next(iter(returns))
    return None


def _lookup_streaming_field_type(
    class_id: str,
    field: str,
    type_index: _StreamingTypeIndex,
) -> str | None:
    """Return *field*'s declared type, walking up the supertype chain."""
    seen: set[str] = set()
    stack = [class_id]
    while stack:
        cid = stack.pop()
        if cid in seen:
            continue
        seen.add(cid)
        found = type_index.fields_by_class.get(cid, {}).get(field)
        if found is not None:
            return found
        stack.extend(type_index.supertypes_by_class.get(cid, []))
    return None


def _strip_trailing_call_args(text: str) -> str | None:
    """Strip the final balanced ``(...)`` group, returning the callee head.

    ``a.getB().getC(x, y)`` → ``a.getB().getC``.  Returns ``None`` when the
    text does not end in a balanced call.
    """
    text = text.rstrip()
    if not text.endswith(")"):
        return None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        char = text[i]
        if char == ")":
            depth += 1
        elif char == "(":
            depth -= 1
            if depth == 0:
                return text[:i].rstrip()
    return None


def _resolve_heuristic_streaming_targets(
    callee_name: str,
    definition_index: dict[str, list[str]],
    node_file_map: dict[str, str] | None,
    caller_file: str,
) -> tuple[list[str], str]:
    """Return bounded name-only targets for streaming call resolution."""
    targets = definition_index.get(callee_name, [])
    if len(targets) <= _MAX_HEURISTIC_CALL_TARGETS:
        return targets, "heuristic"

    if node_file_map is not None and caller_file:
        same_file_targets = [
            target_id for target_id in targets if node_file_map.get(target_id) == caller_file
        ]
        if same_file_targets and len(same_file_targets) <= _MAX_HEURISTIC_CALL_TARGETS:
            return same_file_targets, "heuristic"

    logger.warning(
        "Skipping heuristic CALLS expansion for %s: %d candidates exceeds limit %d",
        callee_name,
        len(targets),
        _MAX_HEURISTIC_CALL_TARGETS,
    )
    return [], "heuristic"


def _find_enclosing_indexed_node(
    node_id: str,
    child_to_parent: dict[str, str],
    indexed_ids: set[str] | dict[str, Any],
) -> str | None:
    """Walk parent links until a node id present in *indexed_ids* is found.

    Walks the full parent chain (a tree, so it terminates naturally); a
    ``seen`` set guards against any corrupt cyclic parent chain.
    """
    current = node_id
    seen: set[str] = {current}
    while True:
        parent_id = child_to_parent.get(current)
        if parent_id is None or parent_id in seen:
            return None
        if parent_id in indexed_ids:
            return parent_id
        seen.add(parent_id)
        current = parent_id


def _simple_type_name(type_str: str) -> str:
    """Normalize a Java-ish type name to its simple class name."""
    text = type_str.strip()
    if "<" in text:
        text = text[: text.index("<")]
    text = text.replace("[]", "").strip()
    if "." in text:
        text = text.rsplit(".", maxsplit=1)[-1]
    return text


def _extract_import_fqn(code: str) -> str | None:
    """Return the imported fully-qualified name from a Java import statement.

    Handles normal and ``static`` imports by returning the last token; wildcard
    imports (``import a.b.*;``) return the ``a.b.*`` form unchanged.
    """
    text = code.strip().rstrip(";")
    if not text.startswith("import "):
        return None
    parts = text.split()
    return parts[-1] if len(parts) >= 2 else None


def _parse_formal_parameter_code(code: str) -> tuple[str, str]:
    """Return ``(name, type)`` from a Java formal-parameter snippet."""
    tokens = [token for token in code.strip().split() if not token.startswith("@")]
    if len(tokens) < 2:
        return "", ""
    name = tokens[-1]
    if name.startswith("..."):
        name = name[3:]
    if name.endswith("[]"):
        name = name[:-2]
    return name, " ".join(tokens[:-1])


def _extract_callee_name_from_code(code: str) -> str | None:
    """Extract a function/method name from call-site code.

    Handles ``func(…)`` and ``obj.method(…)`` patterns.
    """
    if "(" not in code:
        return None
    prefix = code.split("(", maxsplit=1)[0].strip()
    if not prefix:
        return None
    if "." in prefix:
        name = prefix.rsplit(".", maxsplit=1)[-1]
        return name if name.isidentifier() else None
    return prefix if prefix.isidentifier() else None
