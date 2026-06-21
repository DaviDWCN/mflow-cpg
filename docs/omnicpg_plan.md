## Plan
We need to implement the short-term evolution (Phase 1) described in the proposal for the semantic analysis optimization:
1. **Refactor `_truncate_code`**: Introduce AST-aware compression.
2. **Optimize `_build_context_str`**: Introduce 2-degree call graph context (callers and dependencies).

### Detailed Steps:
1. **Optimize `_build_context_str`**
   - Modify the Neo4j query in `enrich_semantic_intent` to fetch 2-degree information. Specifically, for methods, add "who called me" `[(caller)-[:CALLS]->(n) | caller.name]` and 2nd degree calls.
   - However, a simpler way to do this inside the existing structure is to add `[(caller:Method)-[:CALLS]->(n) | caller.name] AS called_by` to the Neo4j query for Method nodes.
   - Update `_build_context_str` to include this `called_by` information in the prompt context.

2. **Refactor `_truncate_code`**
   - Replace the simplistic token heuristic with a more sophisticated AST-based code truncation.
   - Introduce `tree-sitter-python` and `tree-sitter-java` parsing. Since OmniCPG supports both, we can try to guess the language or use a fallback. Actually, `enrich_semantic_intent` doesn't currently get the language passed. We could pass `n.language AS language` in the Neo4j query or just use a simple heuristic to pick the parser. Wait, `CPGNode` usually has a `properties` dict. Does it have `language`? If not, we can just try parsing with python, and if it fails or we guess Java, use Java.
   - Actually, wait, the `ast_truncate_code` function I prototyped works fine using a heuristic! Wait, let's look at `Node` attributes in Neo4j. It's better to fetch `language` if available. Let's see if we can get `file_path` or `language`.
   - Update `_truncate_code` to take `language` as an optional parameter (from `row.get("language")` or `row.get("file_path")`), and use `tree-sitter` to parse and compress the code (e.g. replacing large string literals, block comments, docstrings with placeholders). If still too long, fallback to the 20%/20% cutoff.
   - We need to pass the `language` argument from `enrich_semantic_intent` up to `_truncate_code` via `_process_semantic_and_embedding` and `_fetch_semantic_summary`.
   - The query in `enrich_semantic_intent` can fetch `n.file_path` or `n.language` or just we use the guess_language heuristic since the heuristic works perfectly for our limited java vs python scope.

3. **Complete pre-commit steps to ensure proper testing, verification, review, and reflection are done.**
   - Run tests, check coverage, ensure `mypy` and `ruff` pass.
