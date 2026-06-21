"""Graph enrichment — post-import passes that materialize derived edges.

These passes run against the *already-persisted* Neo4j graph and do not
require re-parsing source code.  They close gaps that per-file analysis
cannot fill on its own — most importantly cross-file inheritance edges,
whose base types almost always live in a different file than the
referencing class.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any, TypeVar

import tree_sitter_python as tspython
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

if TYPE_CHECKING:
    from collections.abc import Callable

if TYPE_CHECKING:
    from omnicpg.adapters.neo4j_adapter import Neo4jAdapter

logger = logging.getLogger(__name__)

# Batch size for UNWIND-based edge MERGE writes.
_EDGE_BATCH_SIZE = 1000


def _build_indexes(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build fully-qualified-name and simple-name lookup indexes.

    Returns:
        ``(fqn_index, simple_index)`` where ``fqn_index`` maps a class FQN
        to its node id and ``simple_index`` maps a simple class name to the
        list of node ids sharing that name.
    """
    fqn_index: dict[str, str] = {}
    simple_index: dict[str, list[str]] = {}
    for row in rows:
        node_id = str(row["id"])
        fqn = str(row.get("fqn") or "")
        name = str(row.get("name") or "")
        if fqn:
            fqn_index[fqn] = node_id
        if name:
            simple_index.setdefault(name, []).append(node_id)
    return fqn_index, simple_index


def _resolve_base(
    base_name: str,
    referrer_id: str,
    referrer_fqn: str,
    fqn_index: dict[str, str],
    simple_index: dict[str, list[str]],
) -> str | None:
    """Resolve a base type name to a node id within the project.

    Resolution order mirrors the per-file Java resolver but operates over
    the *whole* project:

    1. exact FQN match,
    2. FQN suffix match for qualified names,
    3. unique simple-name match,
    4. for ambiguous simple names, the candidate sharing the longest FQN
       prefix with the referrer (only if unambiguously closest).
    """
    name = base_name.strip()
    if not name:
        return None

    # 1. exact FQN.
    if name in fqn_index:
        target = fqn_index[name]
        return target if target != referrer_id else None

    # 2. qualified name → match by FQN suffix.
    if "." in name:
        matches = [cid for fqn, cid in fqn_index.items() if fqn.endswith("." + name)]
        if len(matches) == 1:
            return matches[0] if matches[0] != referrer_id else None
        simple = name.rsplit(".", 1)[-1]
    else:
        simple = name

    candidates = [cid for cid in simple_index.get(simple, []) if cid != referrer_id]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # 3. ambiguous simple name → prefer nearest enclosing scope by longest
    # shared FQN prefix with the referrer.
    ref_parts = referrer_fqn.split(".")
    fqn_by_id = {cid: fqn for fqn, cid in fqn_index.items()}

    def shared_prefix_len(cid: str) -> int:
        shared = 0
        for a, b in zip(ref_parts, fqn_by_id.get(cid, "").split("."), strict=False):
            if a != b:
                break
            shared += 1
        return shared

    best = max(candidates, key=shared_prefix_len)
    best_score = shared_prefix_len(best)
    if sum(1 for cid in candidates if shared_prefix_len(cid) == best_score) == 1:
        return best
    return None


def materialize_inheritance_edges(
    adapter: Neo4jAdapter,
    project_id: str,
) -> dict[str, Any]:
    """Materialize cross-file ``IMPLEMENTS`` edges from class metadata.

    Reads every ``Class`` / ``Interface`` node's ``superclass`` (single
    base) and ``base_classes`` (interface list) properties, resolves each
    base type to a node within the same project, and MERGEs an
    ``IMPLEMENTS`` edge.  Each edge carries:

    * ``project_id`` — project isolation key,
    * ``base_class`` — the raw base type name as written in source,
    * ``kind`` — ``"extends"`` for the superclass, ``"implements"`` for
      interfaces.

    External base types (e.g. ``Serializable``, ``TimerTask``) that have
    no node in the project are skipped.

    Returns:
        A summary dict with resolution counts.
    """
    rows = adapter.query(
        """
        MATCH (c:Node)
        WHERE c.project_id = $project_id
          AND (c:Class OR c:Interface)
        RETURN c.id AS id, c.fqn AS fqn, c.name AS name,
               c.superclass AS superclass, c.base_classes AS base_classes
        """,
        project_id=project_id,
    )

    fqn_index, simple_index = _build_indexes(rows)

    edge_rows: list[dict[str, str]] = []
    unresolved = 0
    for row in rows:
        referrer_id = str(row["id"])
        referrer_fqn = str(row.get("fqn") or "")

        pending: list[tuple[str, str]] = []
        superclass = row.get("superclass")
        if superclass:
            pending.append((str(superclass), "extends"))
        base_classes = row.get("base_classes") or []
        for base in base_classes:
            if base:
                pending.append((str(base), "implements"))

        for base_name, kind in pending:
            target_id = _resolve_base(
                base_name, referrer_id, referrer_fqn, fqn_index, simple_index
            )
            if target_id is None:
                unresolved += 1
                continue
            edge_rows.append(
                {
                    "src": referrer_id,
                    "dst": target_id,
                    "base_class": base_name,
                    "kind": kind,
                    "project_id": project_id,
                }
            )

    created = 0
    for start in range(0, len(edge_rows), _EDGE_BATCH_SIZE):
        batch = edge_rows[start : start + _EDGE_BATCH_SIZE]
        adapter.query(
            """
            UNWIND $rows AS row
            MATCH (a:Node {id: row.src})
            MATCH (b:Node {id: row.dst})
            MERGE (a)-[r:IMPLEMENTS]->(b)
            SET r.project_id = row.project_id,
                r.base_class = row.base_class,
                r.kind = row.kind
            """,
            rows=batch,
        )
        created += len(batch)

    summary = {
        "classes_scanned": len(rows),
        "edges_created": created,
        "unresolved_bases": unresolved,
    }
    logger.info(
        "Inheritance materialization: %d edges from %d classes (%d unresolved external bases)",
        created,
        len(rows),
        unresolved,
    )
    return summary


# ── Semantic Enrichment ────────────────────────────────────────────────────


T = TypeVar("T")


def _retry_with_backoff(
    func: Callable[[], T], max_retries: int = 3, base_delays: tuple[float, ...] = (2.0, 4.0, 8.0)
) -> T:
    """Execute a function with exponential backoff and jitter on temporary HTTP errors."""
    attempt = 0
    while attempt < max_retries:
        try:
            return func()
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and attempt < max_retries - 1:
                delay = base_delays[attempt] if attempt < len(base_delays) else base_delays[-1]
                jitter = random.uniform(0.1, 0.5)
                time.sleep(delay + jitter)
                attempt += 1
                logger.debug("Retry attempt %d after HTTP %d error", attempt, e.code)
            else:
                raise
        except urllib.error.URLError:
            raise
    raise urllib.error.URLError("Max retries exceeded")


MAX_CONTEXT_TOKENS = 4000


def _truncate_code(code: str, file_path: str | None = None) -> str:
    """Truncate code using AST-aware compression (comments, docstrings, long strings).
    Falls back to basic truncation if parsing fails or code is still too long.
    """
    if not code:
        return code

    # 1. Determine language
    lang_str = None
    if file_path:
        file_path_lower = file_path.lower()
        if file_path_lower.endswith(".py"):
            lang_str = "python"
        elif file_path_lower.endswith(".java") or file_path_lower.endswith(".jsp"):
            lang_str = "java"

    # Heuristic guess if no file_path
    if not lang_str:
        if "def " in code or "import " in code:
            lang_str = "python"
        elif "public " in code or "class " in code or "void " in code:
            lang_str = "java"
        else:
            lang_str = "python"  # fallback

    try:
        if lang_str == "python":
            lang = Language(tspython.language())
        else:
            lang = Language(tsjava.language())

        parser = Parser(lang)
        source_bytes = code.encode("utf-8")
        tree = parser.parse(source_bytes)

        # Traverse AST to collect nodes to replace
        replacements = []  # list of (start_byte, end_byte, replacement_bytes)

        def traverse(node):
            if lang_str == "python":
                if node.type == "comment":
                    replacements.append((node.start_byte, node.end_byte, b"# ..."))
                elif node.type == "string":
                    # Check if it's a docstring or a long string
                    node_text = source_bytes[node.start_byte:node.end_byte]
                    if node_text.startswith(b'"""') or node_text.startswith(b"'''") or len(node_text) > 100:
                        replacements.append((node.start_byte, node.end_byte, b'"[... string ...]"'))
                else:
                    for child in node.children:
                        traverse(child)
            elif lang_str == "java":
                if node.type in ("block_comment", "line_comment"):
                    replacements.append((node.start_byte, node.end_byte, b"/* ... */"))
                elif node.type == "string_literal":
                    node_text = source_bytes[node.start_byte:node.end_byte]
                    if len(node_text) > 100:
                        replacements.append((node.start_byte, node.end_byte, b'"[... string ...]"'))
                else:
                    for child in node.children:
                        traverse(child)

        traverse(tree.root_node)

        # Sort replacements by start_byte descending to replace from back to front
        replacements.sort(key=lambda x: x[0], reverse=True)

        # Apply replacements
        last_start = len(source_bytes)
        result_parts = []
        for start, end, repl in replacements:
            if end <= last_start:
                # Part after this replacement up to last_start
                result_parts.append(source_bytes[end:last_start])
                result_parts.append(repl)
                last_start = start
        if last_start > 0:
            result_parts.append(source_bytes[:last_start])

        result_parts.reverse()
        compressed_code = b"".join(result_parts).decode("utf-8", errors="ignore")
    except Exception as e:
        logger.debug("AST compression failed, falling back to heuristic: %s", e)
        compressed_code = code

    # Fallback if still too long or compression didn't run
    estimated_tokens = len(compressed_code) / 3.5
    if estimated_tokens > MAX_CONTEXT_TOKENS:
        keep_chars = int(MAX_CONTEXT_TOKENS * 3.5 * 0.2)
        return compressed_code[:keep_chars] + "\n\n[... truncated for length ...]\n\n" + compressed_code[-keep_chars:]

    return compressed_code


def _fetch_embedding(
    text: str,
    api_base: str,
    api_key: str | None = None,
    model: str = "nomic-embed-text",
) -> list[float] | None:
    """Fetch a vector embedding for the given text from a local LLM via API."""
    if not text.strip():
        return None
    url = f"{api_base.rstrip('/')}/embeddings"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = {
        "model": model,
        "input": text,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    def fetch() -> list[float] | None:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if "data" in result and len(result["data"]) > 0:
                embedding = result["data"][0].get("embedding")
                if isinstance(embedding, list):
                    return [float(x) for x in embedding]
        return None

    try:
        return _retry_with_backoff(fetch)
    except (urllib.error.URLError, json.JSONDecodeError, ValueError, KeyError) as e:
        logger.debug("Failed to fetch embedding: %s", e)
    return None


def _fetch_semantic_summary(
    code: str,
    api_base: str,
    api_key: str | None,
    model: str,
    node_type: str = "Method",
    context_str: str = "",
    file_path: str | None = None,
) -> str | None:
    """Fetch a semantic summary for the given code from a local LLM via API."""
    if not code or not code.strip():
        return None

    truncated_code = _truncate_code(code, file_path)

    if node_type == "Class":
        system_prompt = (
            "You are an expert software engineer analyzing a Class. "
            "Output your analysis strictly as a JSON object with the following keys: "
            "'intent' (a summary of the architectural role and core domain responsibilities), "
            "'side_effects' (any external system interactions, state mutations, "
            "or resource usage), "
            "'data_sources' (data structures, models, or entities operated on), "
            "'taint_tags' (a list of string tags like 'Handles PII', 'Network IO', "
            "'Cryptographic', or an empty list). "
            "Do not output markdown formatting, just the raw JSON object."
        )
    else:
        system_prompt = (
            "You are an expert software engineer. Summarize the core business logic, "
            "input processing, and outcome of this function in one sentence. "
            "Do not include boilerplate or conversational text. Output only the summary."
        )

    user_content = truncated_code
    if context_str:
        user_content = f"{context_str}\n\nCode:\n{truncated_code}"

    # Construct standard OpenAI chat completion payload
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_content,
            },
        ],
        "temperature": 0.1,
        "max_tokens": 150,
    }

    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )

    def fetch() -> str | None:
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode("utf-8"))
            if "choices" in result and len(result["choices"]) > 0:
                summary = result["choices"][0].get("message", {}).get("content", "").strip()
                return summary if summary else None
        return None

    try:
        return _retry_with_backoff(fetch)
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.debug("Failed to fetch semantic summary: %s", e)
    return None


def _process_semantic_and_embedding(
    code: str,
    api_base: str,
    api_key: str | None,
    model: str,
    embedding_model: str,
    node_type: str = "Method",
    context_str: str = "",
    file_path: str | None = None,
) -> dict[str, Any] | None:
    summary_json_str = _fetch_semantic_summary(
        code, api_base, api_key, model, node_type, context_str, file_path
    )
    if not summary_json_str:
        return None

    try:
        # Sometimes models wrap json in markdown blocks
        clean_str = summary_json_str.strip()
        if clean_str.startswith("```json"):
            clean_str = clean_str[7:]
        if clean_str.endswith("```"):
            clean_str = clean_str[:-3]
        parsed = json.loads(clean_str.strip())

        intent = parsed.get("intent", "")
        side_effects = parsed.get("side_effects", "")
        data_sources = parsed.get("data_sources", "")
        taint_tags = parsed.get("taint_tags", [])

        result = {
            "intent": intent,
            "side_effects": side_effects,
            "data_sources": data_sources,
            "taint_tags": taint_tags,
        }

        if intent:
            result["intent_embedding"] = _fetch_embedding(
                intent, api_base, api_key, embedding_model
            )
        if side_effects:
            result["side_effects_embedding"] = _fetch_embedding(
                side_effects, api_base, api_key, embedding_model
            )
        if data_sources:
            result["data_sources_embedding"] = _fetch_embedding(
                data_sources, api_base, api_key, embedding_model
            )

        return result
    except json.JSONDecodeError as e:
        logger.debug("Failed to parse LLM JSON output: %s", e)
        return None


def _build_context_str(row: dict[str, Any], node_type: str) -> str:
    """Build a contextual string for the LLM prompt."""
    context_parts = []
    name = row.get("name")

    if node_type == "Class":
        if name:
            context_parts.append(f"Class: {name}")
        parent = row.get("parent_class")
        if parent:
            context_parts.append(f"Inherits from: {parent}")
        methods = [m for m in (row.get("class_methods") or []) if m]
        if methods:
            # limit to 10 methods for context size
            context_parts.append(f"Core methods: {', '.join(methods[:10])}")

    else:  # Method
        if name:
            context_parts.append(f"Method: {name}")
        params = [p for p in (row.get("parameters") or []) if p]
        if params:
            context_parts.append(f"Parameters: {', '.join(params)}")
        calls = [c for c in (row.get("called_methods") or []) if c]
        if calls:
            # limit to 10 calls for context size
            context_parts.append(f"Calls: {', '.join(calls[:10])}")
        called_by = [cb for cb in (row.get("called_by") or []) if cb]
        if called_by:
            # limit to 10 callers for context size
            context_parts.append(f"Called by: {', '.join(called_by[:10])}")

    return " | ".join(context_parts)


def enrich_semantic_intent(
    adapter: Neo4jAdapter,
    project_id: str,
    api_base: str,
    api_key: str | None = None,
    model: str = "llama3",
    embedding_model: str = "nomic-embed-text",
) -> dict[str, Any]:
    """Tag ``Method`` and ``Class`` nodes with a ``semantic_summary`` and ``embedding``.

    Queries Neo4j for un-enriched nodes that have source code, fetches a summary
    from an OpenAI-compatible API endpoint (e.g., Ollama, vLLM), fetches its vector
    embedding, and writes them back to the graph.

    The pass is idempotent and confined to the given ``project_id``.
    """
    # Create an index to speed up finding unenriched nodes, if not exists.
    with contextlib.suppress(Exception):
        adapter.query(
            "CREATE INDEX idx_node_semantic_intent IF NOT EXISTS FOR (n:Node) ON "
            "(n.semantic_intent)"
        )

    # Create vector indexes for multi-vector fields
    for field in ("intent_embedding", "side_effects_embedding", "data_sources_embedding"):
        with contextlib.suppress(Exception):
            adapter.query(
                f"CREATE VECTOR INDEX node_{field} IF NOT EXISTS "
                f"FOR (n:Node) ON (n.{field}) "
                "OPTIONS {indexConfig: "
                "{`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}"
            )

    rows = adapter.query(
        """
        MATCH (n:Node)
        WHERE n.project_id = $project_id
          AND (n:Method OR n:Class)
          AND n.semantic_intent IS NULL
          AND n.code IS NOT NULL
        RETURN n.id AS id,
               n.code AS code,
               n.file_path AS file_path,
               labels(n) AS labels,
               n.name AS name,
               [(n)-[:CALLS]->(called) | called.name] AS called_methods,
               [(caller:Method)-[:CALLS]->(n) | caller.name] AS called_by,
               [(n)-[:PARENT_OF]->(p:Node) WHERE p.type='formal_parameter' | p.name] AS parameters,
               n.superclass AS parent_class,
               [(n)-[:PARENT_OF]->(m:Method) | m.name] AS class_methods
        """,
        project_id=project_id,
    )

    if not rows:
        return {"nodes_scanned": 0, "nodes_enriched": 0}

    updates: list[dict[str, Any]] = []

    # Process requests concurrently to maximize GPU/API throughput.
    max_workers = 10
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {}
        for row in rows:
            labels = row.get("labels") or []
            node_type = "Class" if "Class" in labels else "Method"
            context_str = _build_context_str(row, node_type)
            future = executor.submit(
                _process_semantic_and_embedding,
                str(row["code"]),
                api_base,
                api_key,
                model,
                embedding_model,
                node_type,
                context_str,
                row.get("file_path"),
            )
            future_to_id[future] = str(row["id"])
        for future in as_completed(future_to_id):
            node_id = future_to_id[future]
            try:
                result = future.result()
                if result:
                    update_dict: dict[str, Any] = {"id": node_id}
                    update_dict.update(result)
                    updates.append(update_dict)
            except Exception as exc:
                logger.debug("Node %s generated an exception: %s", node_id, exc)

    updated = 0
    for start in range(0, len(updates), _EDGE_BATCH_SIZE):
        batch = updates[start : start + _EDGE_BATCH_SIZE]
        adapter.query(
            """
            UNWIND $rows AS row
            MATCH (n:Node {id: row.id})
            SET n.semantic_intent = row.intent,
                n.semantic_side_effects = row.side_effects,
                n.semantic_data_sources = row.data_sources,
                n.semantic_taint_tags = row.taint_tags
            WITH n, row
            CALL apoc.do.when(
                row.intent_embedding IS NOT NULL,
                'SET n.intent_embedding = row.intent_embedding RETURN n',
                'RETURN n', {n:n, row:row}
            ) YIELD value AS v1
            CALL apoc.do.when(
                row.side_effects_embedding IS NOT NULL,
                'SET n.side_effects_embedding = row.side_effects_embedding RETURN n',
                'RETURN n', {n:n, row:row}
            ) YIELD value AS v2
            CALL apoc.do.when(
                row.data_sources_embedding IS NOT NULL,
                'SET n.data_sources_embedding = row.data_sources_embedding RETURN n',
                'RETURN n', {n:n, row:row}
            ) YIELD value AS v3
            RETURN count(*)
            """,
            rows=batch,
        )
        updated += len(batch)

    summary_stats = {
        "nodes_scanned": len(rows),
        "nodes_enriched": updated,
    }
    logger.info(
        "Semantic enrichment: %d/%d nodes enriched",
        updated,
        len(rows),
    )
    return summary_stats


def materialize_java_parameter_reaches_edges(
    adapter: Neo4jAdapter,
    project_id: str,
) -> dict[str, Any]:
    """Materialize Java argument edges to ``formal_parameter`` definitions.

    Older streaming Java analysis bound inter-procedural ``argument`` REACHES
    edges to the parameter-name ``identifier`` child.  The intra-procedural
    Java DFG uses the enclosing ``formal_parameter`` node as the definition,
    so those chains stop at the identifier.  This pass adds the canonical
    ``argument -> formal_parameter`` edge in place while leaving legacy edges
    intact for compatibility.
    """
    rows = adapter.query(
        """
        MATCH (src:Node)-[old:REACHES]->(paramName:Node)<-[:PARENT_OF]-(param:Node)
        WHERE src.project_id = $project_id
          AND paramName.project_id = $project_id
          AND param.project_id = $project_id
          AND old.interprocedural = 'argument'
          AND paramName.type = 'identifier'
          AND param.type = 'formal_parameter'
        MERGE (src)-[fixed:REACHES]->(param)
        SET fixed.project_id = $project_id,
            fixed.interprocedural = 'argument',
            fixed.variable = coalesce(old.variable, param.name, paramName.code, paramName.name),
            fixed.index = old.index,
            fixed.repaired_from = 'parameter_identifier'
        RETURN count(DISTINCT fixed) AS edges_materialized
        """,
        project_id=project_id,
    )
    materialized = int(rows[0].get("edges_materialized", 0)) if rows else 0
    summary = {"edges_materialized": materialized}
    logger.info(
        "Java parameter REACHES materialization: %d edges",
        materialized,
    )
    return summary


# ── Architectural-role classification ──────────────────────────────────────

# role → architectural layer.  ``role`` is the fine-grained stereotype;
# ``layer`` is the coarse band used for top-down navigation.
_ROLE_LAYER: dict[str, str] = {
    "Controller": "web",
    "Service": "service",
    "Repository": "data",
    "Entity": "model",
    "DTO": "model",
    "Component": "service",
}

# Annotation simple-name → role (Spring / Jakarta / JPA stereotypes).
_ANNOTATION_ROLE: dict[str, str] = {
    "restcontroller": "Controller",
    "controller": "Controller",
    "service": "Service",
    "repository": "Repository",
    "component": "Component",
    "entity": "Entity",
    "table": "Entity",
    "mappedsuperclass": "Entity",
}

# Name-suffix → role (ordered longest-first so e.g. ``ServiceImpl`` wins over
# ``Service`` and ``Repository`` wins before generic fallbacks).
_NAME_SUFFIX_ROLES: tuple[tuple[str, str], ...] = (
    ("ServiceImpl", "Service"),
    ("Controller", "Controller"),
    ("Servlet", "Controller"),
    ("Action", "Controller"),
    ("Repository", "Repository"),
    ("Mapper", "Repository"),
    ("DAO", "Repository"),
    ("Dao", "Repository"),
    ("Service", "Service"),
    ("Entity", "Entity"),
    ("DTO", "DTO"),
    ("Dto", "DTO"),
    ("VO", "DTO"),
    ("Form", "DTO"),
)

# Base-type simple-name suffix → role (inheritance-driven).
_BASE_SUFFIX_ROLES: tuple[tuple[str, str], ...] = (
    ("Dao", "Repository"),
    ("DAO", "Repository"),
    ("Repository", "Repository"),
    ("Mapper", "Repository"),
    ("ServiceImpl", "Service"),
    ("Service", "Service"),
    ("Action", "Controller"),
)


def _classify_role(
    name: str,
    annotations: list[str],
    base_names: list[str],
) -> tuple[str, str] | None:
    """Classify a class into an architectural ``(role, layer)``.

    Resolution precedence (first match wins, highest confidence first):

    1. framework stereotype annotations (``@Service`` …),
    2. base-type / interface naming (``extends FooDao`` …),
    3. the class' own name suffix (``FooServiceImpl`` …).

    Returns ``None`` when no rule matches.
    """
    lowered = {a.lower().lstrip("@") for a in annotations}
    for ann, role in _ANNOTATION_ROLE.items():
        if ann in lowered:
            return role, _ROLE_LAYER[role]

    for base in base_names:
        simple = base.rsplit(".", 1)[-1]
        for suffix, role in _BASE_SUFFIX_ROLES:
            if simple.endswith(suffix):
                return role, _ROLE_LAYER[role]

    for suffix, role in _NAME_SUFFIX_ROLES:
        if name.endswith(suffix):
            return role, _ROLE_LAYER[role]

    return None


def classify_architectural_roles(
    adapter: Neo4jAdapter,
    project_id: str,
) -> dict[str, Any]:
    """Tag ``Class`` / ``Interface`` nodes with architectural ``role``/``layer``.

    Derives a coarse architectural stereotype for every class using framework
    annotations, inheritance and naming conventions, then writes two
    properties — ``role`` (fine-grained) and ``layer`` (coarse band) — so AI
    agents can navigate the codebase top-down (e.g. *"list the service layer"*).

    The pass is idempotent and confined to the given ``project_id``.  Classes
    that match no rule are left untouched.

    Returns:
        A summary dict with the per-role counts and totals.
    """
    # Idempotent indexes backing role/layer navigation queries.
    for stmt in (
        "CREATE INDEX idx_node_role IF NOT EXISTS FOR (n:Node) ON (n.role)",
        "CREATE INDEX idx_node_layer IF NOT EXISTS FOR (n:Node) ON (n.layer)",
    ):
        try:
            adapter.query(stmt)
        except Exception:  # index may already exist / be creating
            logger.debug("Index creation skipped: %s", stmt)

    rows = adapter.query(
        """
        MATCH (c:Node)
        WHERE c.project_id = $project_id
          AND (c:Class OR c:Interface)
        RETURN c.id AS id, c.name AS name,
               c.annotations AS annotations,
               c.superclass AS superclass,
               c.base_classes AS base_classes
        """,
        project_id=project_id,
    )

    updates: list[dict[str, str]] = []
    by_role: dict[str, int] = {}
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        annotations = [str(a) for a in (row.get("annotations") or [])]
        base_names = [str(b) for b in (row.get("base_classes") or []) if b]
        superclass = row.get("superclass")
        if superclass:
            base_names.append(str(superclass))

        classified = _classify_role(name, annotations, base_names)
        if classified is None:
            continue
        role, layer = classified
        updates.append({"id": str(row["id"]), "role": role, "layer": layer})
        by_role[role] = by_role.get(role, 0) + 1

    updated = 0
    for start in range(0, len(updates), _EDGE_BATCH_SIZE):
        batch = updates[start : start + _EDGE_BATCH_SIZE]
        adapter.query(
            """
            UNWIND $rows AS row
            MATCH (n:Node {id: row.id})
            SET n.role = row.role, n.layer = row.layer
            """,
            rows=batch,
        )
        updated += len(batch)

    summary = {
        "classes_scanned": len(rows),
        "classes_classified": updated,
        "by_role": by_role,
    }
    logger.info(
        "Architectural-role classification: %d/%d classes tagged %s",
        updated,
        len(rows),
        by_role,
    )
    return summary


def enrich_llm_architectural_roles(
    adapter: Neo4jAdapter,
    project_id: str,
    api_base: str,
    api_key: str | None = None,
    model: str = "llama3",
) -> dict[str, Any]:
    """Fallback LLM-driven architectural role classification for unclassified nodes."""
    rows = adapter.query(
        """
        MATCH (c:Node)
        WHERE c.project_id = $project_id
          AND (c:Class OR c:Interface)
          AND c.role IS NULL
          AND c.code IS NOT NULL
        RETURN c.id AS id,
               c.code AS code,
               c.name AS name,
               c.file_path AS file_path,
               c.annotations AS annotations,
               c.superclass AS superclass,
               c.base_classes AS base_classes,
               [(c)-[:PARENT_OF]->(m:Method) | m.name] AS class_methods
        """,
        project_id=project_id,
    )

    if not rows:
        return {"nodes_scanned": 0, "nodes_enriched": 0}

    updates: list[dict[str, Any]] = []

    def process_node(row: dict[str, Any]) -> dict[str, Any] | None:
        truncated_code = _truncate_code(str(row["code"]), row.get("file_path"))

        context_parts = []
        name = row.get("name")
        if name:
            context_parts.append(f"Class: {name}")
        parent = row.get("superclass")
        if parent:
            context_parts.append(f"Inherits from: {parent}")
        methods = [m for m in (row.get("class_methods") or []) if m]
        if methods:
            context_parts.append(f"Core methods: {', '.join(methods[:10])}")

        context_str = " | ".join(context_parts)

        system_prompt = (
            "You are an expert software architect analyzing a Class. "
            "Determine its true architectural role and broad layer based on its code, "
            "methods, and structure. Output your analysis strictly as a JSON object "
            "with two keys: 'role' (e.g. 'Controller', 'Service', 'Message Queue Consumer', "
            "'Data Pipeline Transform') and 'layer' (e.g. 'web', 'service', 'data', 'model'). "
            "Do not output markdown formatting, just the raw JSON object."
        )

        user_content = f"{context_str}\n\nCode:\n{truncated_code}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
        }

        url = f"{api_base.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )

        def fetch() -> str | None:
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "choices" in result and len(result["choices"]) > 0:
                    summary = result["choices"][0].get("message", {}).get("content", "").strip()
                    return summary if summary else None
            return None

        try:
            summary_json_str = _retry_with_backoff(fetch)
            if not summary_json_str:
                return None

            clean_str = summary_json_str.strip()
            if clean_str.startswith("```json"):
                clean_str = clean_str[7:]
            if clean_str.endswith("```"):
                clean_str = clean_str[:-3]
            parsed = json.loads(clean_str.strip())

            role = parsed.get("role")
            layer = parsed.get("layer")
            if role and layer:
                return {"id": row["id"], "role": role, "layer": layer}
        except (urllib.error.URLError, json.JSONDecodeError, ValueError) as e:
            logger.debug("Failed to fetch LLM role summary: %s", e)

        return None

    max_workers = 10
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {executor.submit(process_node, row): row["id"] for row in rows}
        for future in as_completed(future_to_id):
            try:
                result = future.result()
                if result:
                    updates.append(result)
            except Exception as exc:
                logger.debug("LLM role classification generated an exception: %s", exc)

    updated = 0
    for start in range(0, len(updates), _EDGE_BATCH_SIZE):
        batch = updates[start : start + _EDGE_BATCH_SIZE]
        adapter.query(
            """
            UNWIND $rows AS row
            MATCH (n:Node {id: row.id})
            SET n.role = row.role, n.layer = row.layer
            """,
            rows=batch,
        )
        updated += len(batch)

    summary_stats = {
        "nodes_scanned": len(rows),
        "nodes_enriched": updated,
    }
    logger.info("LLM role classification: %d/%d nodes enriched", updated, len(rows))
    return summary_stats


def materialize_cha_polymorphism_edges(
    adapter: Neo4jAdapter,
    project_id: str,
) -> dict[str, Any]:
    """Materialize virtual CALLS and REACHES edges for polymorphic method calls.

    Using the Class Hierarchy Analysis (CHA) approach:
    1. Find all calls to interface/base class methods.
    2. Traverse IMPLEMENTS relationships to find concrete subclasses/implementations.
    3. If a subclass overrides/implements the target method (same name), build:
       - A virtual CALLS edge from caller to override method.
       - REACHES edges from call site arguments to override method parameters.
       - REACHES edges from override method returns to the call site node.
    """
    rows = adapter.query(
        """
        MATCH (caller:Method)-[c:CALLS]->(callee:Method)
        WHERE caller.project_id = $project_id AND callee.project_id = $project_id
        MATCH (base:Node)-[:PARENT_OF]->(callee)
        WHERE base.project_id = $project_id AND (base:Class OR base:Interface)
        MATCH (impl:Node)-[:IMPLEMENTS*]->(base)
        WHERE impl.project_id = $project_id AND (impl:Class OR impl:Interface)
        MATCH (impl)-[:PARENT_OF]->(override:Method)
        WHERE override.project_id = $project_id AND override.name = callee.name
        RETURN DISTINCT
               caller.id AS caller_id,
               callee.name AS callee_name,
               c.callsite_id AS callsite_id,
               override.id AS override_id
        """,
        project_id=project_id,
    )

    calls_created = 0
    reaches_created = 0

    for row in rows:
        caller_id = row["caller_id"]
        callee_name = row["callee_name"]
        callsite_id = row["callsite_id"]
        override_id = row["override_id"]

        if not callsite_id or not override_id:
            continue

        # 1. Create the virtual CALLS edge
        adapter.query(
            """
            MATCH (caller:Node {id: $caller_id})
            MATCH (override:Node {id: $override_id})
            MERGE (caller)-[r:CALLS]->(override)
            SET r.project_id = $project_id,
                r.callsite_id = $callsite_id,
                r.callee = $callee_name,
                r.resolution = "cha_polymorphism",
                r.virtual = true
            """,
            caller_id=caller_id,
            override_id=override_id,
            project_id=project_id,
            callsite_id=callsite_id,
            callee_name=callee_name,
        )
        calls_created += 1

        # 2. Bind arguments of the callsite to parameters of the override method
        # Fetch callsite arguments
        arg_rows = adapter.query(
            """
            MATCH (callsite:Node {id: $callsite_id})-[:PARENT_OF]->(arg_list:Node {type: 'argument_list'})-[:PARENT_OF]->(arg:Node)
            WHERE NOT arg.type IN ['(', ')', ',']
            RETURN arg.id AS id,
                   coalesce(arg.line_start, 0) AS line_start,
                   coalesce(arg.column_start, 0) AS column_start
            """,
            callsite_id=callsite_id,
        )
        # Fetch override parameters
        param_rows = adapter.query(
            """
            MATCH (override:Node {id: $override_id})-[:PARENT_OF*1..2]->(param:Node)
            WHERE "Parameter" IN labels(param) OR param.type = 'formal_parameter'
            RETURN param.id AS id,
                   param.name AS name,
                   coalesce(param.line_start, 0) AS line_start,
                   coalesce(param.column_start, 0) AS column_start
            """,
            override_id=override_id,
        )

        # Sort positional arguments and parameters
        sorted_args = sorted(arg_rows, key=lambda x: (int(x["line_start"]), int(x["column_start"])))
        sorted_params = sorted(param_rows, key=lambda x: (int(x["line_start"]), int(x["column_start"])))

        # Positional binding
        for i, arg in enumerate(sorted_args):
            if i < len(sorted_params):
                param = sorted_params[i]
                adapter.query(
                    """
                    MATCH (arg:Node {id: $arg_id})
                    MATCH (param:Node {id: $param_id})
                    MERGE (arg)-[r:REACHES]->(param)
                    SET r.project_id = $project_id,
                        r.variable = $param_name,
                        r.interprocedural = "argument",
                        r.index = $index,
                        r.virtual = true
                    """,
                    arg_id=arg["id"],
                    param_id=param["id"],
                    project_id=project_id,
                    param_name=param.get("name") or f"p{i}",
                    index=str(i),
                )
                reaches_created += 1

        # 3. Bind return statements of override to the callsite
        ret_rows = adapter.query(
            """
            MATCH p = (override:Node {id: $override_id})-[:PARENT_OF*]->(ret:Node)
            WHERE "Return" IN labels(ret)
              AND NONE(n IN nodes(p)[1..-1] WHERE "Class" IN labels(n) OR "Method" IN labels(n))
            RETURN ret.id AS id
            """,
            override_id=override_id,
        )
        for ret in ret_rows:
            adapter.query(
                """
                MATCH (ret:Node {id: $ret_id})
                MATCH (callsite:Node {id: $callsite_id})
                MERGE (ret)-[r:REACHES]->(callsite)
                SET r.project_id = $project_id,
                    r.interprocedural = "return",
                    r.virtual = true
                """,
                ret_id=ret["id"],
                callsite_id=callsite_id,
                project_id=project_id,
            )
            reaches_created += 1

    summary = {
        "virtual_calls_created": calls_created,
        "virtual_reaches_created": reaches_created,
    }
    logger.info(
        "CHA Polymorphism materialization: %d CALLS, %d REACHES edges created",
        calls_created,
        reaches_created,
    )
    return summary

