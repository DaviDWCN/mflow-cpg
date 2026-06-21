"""
Extensible Hierarchical Semantic Enrichment Engine.
Uses unified config.yaml levels to enrich Neo4j CPG nodes.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import time
import random
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

def _retry_with_backoff(func, max_retries=3, base_delays=(2.0, 4.0, 8.0)):
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
            else:
                raise
        except urllib.error.URLError:
            raise
    raise urllib.error.URLError("Max retries exceeded")


class SemanticEnrichmentEngine:
    def __init__(self, adapter: Any, config: Any):
        """
        Initialize the engine with Neo4jAdapter and UnifiedConfig.
        """
        self.adapter = adapter
        self.config = config
        self.api_base = self.config.semantic_analysis.api_base.rstrip("/")
        self.api_key = self.config.semantic_analysis.api_key
        self.model = self.config.semantic_analysis.model
        self.embedding_model = self.config.semantic_analysis.embedding_model

    def _fetch_embedding(self, text: str) -> Optional[List[float]]:
        if not text or not text.strip():
            return None
        url = f"{self.api_base}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = {
            "model": self.embedding_model,
            "input": text,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        def fetch():
            with urllib.request.urlopen(req, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "data" in result and len(result["data"]) > 0:
                    embedding = result["data"][0].get("embedding")
                    if isinstance(embedding, list):
                        return [float(x) for x in embedding]
            return None

        try:
            return _retry_with_backoff(fetch)
        except Exception as e:
            logger.debug(f"Failed to fetch embedding: {e}")
            return None

    def _fetch_llm_response(self, prompt: str, system_prompt: str) -> Optional[str]:
        url = f"{self.api_base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 300,
        }
        
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        def fetch():
            with urllib.request.urlopen(req, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
                if "choices" in result and len(result["choices"]) > 0:
                    content = result["choices"][0].get("message", {}).get("content", "").strip()
                    return content if content else None
            return None

        try:
            return _retry_with_backoff(fetch)
        except Exception as e:
            logger.debug(f"Failed to fetch LLM response: {e}")
            return None

    def _clean_json_string(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()

    def _process_node(self, node_id: str, code: str, prompt_template: str, json_output: bool, level_name: str) -> Optional[Dict[str, Any]]:
        # Truncate code to avoid context overflow
        if len(code) > 12000:
            code = code[:6000] + "\n\n[... truncated ...]\n\n" + code[-6000:]

        response = self._fetch_llm_response(code, prompt_template)
        if not response:
            return None

        result: Dict[str, Any] = {"id": node_id}

        if json_output:
            try:
                clean_resp = self._clean_json_string(response)
                parsed = json.loads(clean_resp)
                
                # Dynamic property mapping based on level and parsed JSON keys
                # Backward compatibility for 'high_level'
                if level_name == "high_level":
                    intent = parsed.get("intent", "")
                    side_effects = parsed.get("side_effects", "")
                    data_sources = parsed.get("data_sources", "")
                    taint_tags = parsed.get("taint_tags", [])

                    result.update({
                        "semantic_intent": intent,
                        "semantic_side_effects": side_effects,
                        "semantic_data_sources": data_sources,
                        "semantic_taint_tags": taint_tags,
                    })

                    # Embeddings for backward compatible fields
                    if intent:
                        result["intent_embedding"] = self._fetch_embedding(intent)
                    if side_effects:
                        result["side_effects_embedding"] = self._fetch_embedding(side_effects)
                    if data_sources:
                        result["data_sources_embedding"] = self._fetch_embedding(data_sources)
                else:
                    # Generic JSON property writing
                    for k, v in parsed.items():
                        prop_name = f"semantic_{level_name}_{k}"
                        result[prop_name] = v
                        if isinstance(v, str) and len(v) > 0:
                            result[f"{prop_name}_embedding"] = self._fetch_embedding(v)
            except Exception as e:
                logger.debug(f"JSON parsing failed for level {level_name}: {e}. Saving raw output.")
                result[f"semantic_{level_name}_summary"] = response
        else:
            # Plain text summary
            # Backward compatibility: Method summaries are stored in semantic_intent
            if level_name == "details_level":
                result["semantic_intent"] = response
                result["intent_embedding"] = self._fetch_embedding(response)
            else:
                prop_name = f"semantic_{level_name}_summary"
                result[prop_name] = response
                result[f"{prop_name}_embedding"] = self._fetch_embedding(response)

        return result

    def enrich_project(self, project_id: str) -> Dict[str, Any]:
        """
        Enriches classes, modules, and methods in the project
        according to configured levels in config.yaml.
        """
        if not self.config.semantic_analysis.enabled:
            logger.info("Semantic analysis is disabled in configuration.")
            return {"status": "disabled"}

        levels = self.config.semantic_analysis.levels
        total_scanned = 0
        total_enriched = 0

        # Ensure vector indexes for backward compatible embeddings
        for field in ("intent_embedding", "side_effects_embedding", "data_sources_embedding"):
            try:
                self.adapter.query(
                    f"CREATE VECTOR INDEX node_{field} IF NOT EXISTS "
                    f"FOR (n:Node) ON (n.{field}) "
                    "OPTIONS {indexConfig: "
                    "{`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}"
                )
            except Exception:
                pass

        for level in levels:
            if not level.enabled:
                continue

            # Resolve query label filter
            labels_clause = " OR ".join(f"n:{lbl}" for lbl in level.target_labels)
            
            # Check if we already enriched this level
            # If level_name is high_level, we check semantic_intent.
            # Otherwise we check semantic_<level_name>_summary or semantic_<level_name>_intent
            check_field = "semantic_intent" if level.name in ("high_level", "details_level") else f"semantic_{level.name}_summary"

            query_str = f"""
            MATCH (n:Node)
            WHERE n.project_id = $project_id
              AND ({labels_clause})
              AND n.code IS NOT NULL
              AND n.{check_field} IS NULL
            RETURN n.id AS id, n.code AS code
            """
            
            rows = self.adapter.query(query_str, project_id=project_id)
            if not rows:
                logger.info(f"Level '{level.name}': No nodes to enrich.")
                continue

            logger.info(f"Level '{level.name}': Found {len(rows)} nodes to enrich.")
            total_scanned += len(rows)
            updates = []

            # Concurrently call LLM
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {
                    executor.submit(
                        self._process_node,
                        row["id"],
                        row["code"],
                        level.prompt,
                        level.json_output,
                        level.name
                    ): row["id"]
                    for row in rows
                }

                for future in as_completed(futures):
                    node_id = futures[future]
                    try:
                        res = future.result()
                        if res:
                            updates.append(res)
                    except Exception as e:
                        logger.error(f"Error enriching node {node_id} on level '{level.name}': {e}")

            # Write updates back to Neo4j in batch
            if updates:
                for batch_idx in range(0, len(updates), 100):
                    batch = updates[batch_idx : batch_idx + 100]
                    
                    # Construct a dynamic Cypher query to write all keys in updates
                    # Since updates has id and various semantic fields, we can use Cypher SET
                    query_write = """
                    UNWIND $batch AS row
                    MATCH (n:Node {id: row.id})
                    SET n += apoc.map.clean(row, ['id'], [])
                    """
                    self.adapter.query(query_write, batch=batch)
                
                total_enriched += len(updates)
                logger.info(f"Level '{level.name}': Enriched {len(updates)} nodes.")

        return {
            "status": "success",
            "nodes_scanned": total_scanned,
            "nodes_enriched": total_enriched
        }
