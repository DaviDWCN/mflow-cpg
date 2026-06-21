"""Java taint source / sink / sanitizer rule catalog.

This module provides a **readable, queryable catalog** of well-known Java taint
*sources* (untrusted input), *sinks* (dangerous operations) and *sanitizers*
(input neutralisation), inspired by the rule sets shipped with industry tools
(CodeQL, Find-Sec-Bugs, OWASP). It is intentionally *data-only* plus a small
matcher so that callers (MCP tools, custom queries) retain full control over
how the classification is used — consistent with the project's taint-analysis
contract that the engine itself does not hardcode source/sink *semantics* into
``REACHES`` edges.

The :func:`classify_invocation` helper tags ``method_invocation`` /
``object_creation_expression`` call sites with a ``security_role`` and
``security_category`` so downstream analysers can quickly locate candidate
endpoints of a taint flow without re-deriving the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

# Security role assigned to a call site.
ROLE_SOURCE = "source"
ROLE_SINK = "sink"
ROLE_SANITIZER = "sanitizer"

# Vulnerability categories (aligned with common CWE groupings).
CAT_SQL_INJECTION = "sql_injection"
CAT_COMMAND_INJECTION = "command_injection"
CAT_PATH_TRAVERSAL = "path_traversal"
CAT_XSS = "xss"
CAT_DESERIALIZATION = "deserialization"
CAT_SSRF = "ssrf"
CAT_LDAP_INJECTION = "ldap_injection"
CAT_XXE = "xxe"
CAT_CODE_INJECTION = "code_injection"
CAT_UNTRUSTED_INPUT = "untrusted_input"
CAT_VALIDATION = "validation"
CAT_ENCODING = "encoding"


@dataclass(frozen=True)
class SecurityRule:
    """A single taint rule matched against a Java call site.

    Attributes:
        role: One of :data:`ROLE_SOURCE`, :data:`ROLE_SINK`,
            :data:`ROLE_SANITIZER`.
        category: The vulnerability / data category (``CAT_*``).
        method: The invoked method name (or constructed type for ``new`` sites).
        receiver_hint: Optional substring that must appear in the call
            receiver / type for the rule to match. ``None`` matches any
            receiver. Used to reduce false positives on common method names.
    """

    role: str
    category: str
    method: str
    receiver_hint: str | None = None


# ── Sources: untrusted / attacker-controlled input ────────────────────────────
_SOURCES: tuple[SecurityRule, ...] = (
    # Servlet / HTTP request parameters and headers.
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getParameter"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getParameterValues"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getParameterMap"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getHeader"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getHeaders"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getQueryString"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getCookies"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getRequestURI"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getRequestURL"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getPathInfo"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getInputStream", "request"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getReader", "request"),
    # Environment / system-controlled input.
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getenv", "System"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "getProperty", "System"),
    # Console / stream reads.
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "nextLine", "Scanner"),
    SecurityRule(ROLE_SOURCE, CAT_UNTRUSTED_INPUT, "readLine"),
)

# ── Sinks: dangerous operations that must not receive tainted data ────────────
_SINKS: tuple[SecurityRule, ...] = (
    # SQL injection.
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "executeQuery"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "executeUpdate"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "execute", "Statement"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "executeLargeUpdate"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "addBatch"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "createQuery"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "createNativeQuery"),
    SecurityRule(ROLE_SINK, CAT_SQL_INJECTION, "nativeQuery"),
    # Command injection.
    SecurityRule(ROLE_SINK, CAT_COMMAND_INJECTION, "exec"),
    SecurityRule(ROLE_SINK, CAT_COMMAND_INJECTION, "ProcessBuilder"),
    SecurityRule(ROLE_SINK, CAT_COMMAND_INJECTION, "command", "ProcessBuilder"),
    SecurityRule(ROLE_SINK, CAT_COMMAND_INJECTION, "start", "ProcessBuilder"),
    # Path traversal.
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "File"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "FileInputStream"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "FileOutputStream"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "FileReader"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "FileWriter"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "RandomAccessFile"),
    SecurityRule(ROLE_SINK, CAT_PATH_TRAVERSAL, "get", "Paths"),
    # Cross-site scripting (response writers).
    SecurityRule(ROLE_SINK, CAT_XSS, "print", "Writer"),
    SecurityRule(ROLE_SINK, CAT_XSS, "println", "Writer"),
    SecurityRule(ROLE_SINK, CAT_XSS, "write", "Writer"),
    SecurityRule(ROLE_SINK, CAT_XSS, "getWriter", "response"),
    # Deserialization.
    SecurityRule(ROLE_SINK, CAT_DESERIALIZATION, "readObject"),
    SecurityRule(ROLE_SINK, CAT_DESERIALIZATION, "readUnshared"),
    SecurityRule(ROLE_SINK, CAT_DESERIALIZATION, "ObjectInputStream"),
    # Code / expression injection.
    SecurityRule(ROLE_SINK, CAT_CODE_INJECTION, "eval"),
    SecurityRule(ROLE_SINK, CAT_CODE_INJECTION, "forName", "Class"),
    SecurityRule(ROLE_SINK, CAT_CODE_INJECTION, "newInstance"),
    # SSRF (outbound requests).
    SecurityRule(ROLE_SINK, CAT_SSRF, "openConnection"),
    SecurityRule(ROLE_SINK, CAT_SSRF, "openStream"),
    SecurityRule(ROLE_SINK, CAT_SSRF, "URL"),
    # LDAP injection.
    SecurityRule(ROLE_SINK, CAT_LDAP_INJECTION, "search", "Context"),
)

# ── Sanitizers: neutralise tainted data ──────────────────────────────────────
_SANITIZERS: tuple[SecurityRule, ...] = (
    # Output encoding (XSS).
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "escapeHtml"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "escapeHtml4"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "escapeXml"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "escapeJavaScript"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "htmlEscape"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "encodeForHTML"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "encodeForJavaScript"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "encodeForURL"),
    SecurityRule(ROLE_SANITIZER, CAT_ENCODING, "forHtml", "Encode"),
    # Parameterised SQL (binds rather than concatenates).
    SecurityRule(ROLE_SANITIZER, CAT_SQL_INJECTION, "prepareStatement"),
    SecurityRule(ROLE_SANITIZER, CAT_SQL_INJECTION, "setString"),
    SecurityRule(ROLE_SANITIZER, CAT_SQL_INJECTION, "setParameter"),
    # Validation / canonicalisation.
    SecurityRule(ROLE_SANITIZER, CAT_VALIDATION, "matches", "Pattern"),
    SecurityRule(ROLE_SANITIZER, CAT_PATH_TRAVERSAL, "getCanonicalPath"),
    SecurityRule(ROLE_SANITIZER, CAT_PATH_TRAVERSAL, "normalize"),
)


# Index rules by method name for O(1) candidate lookup.
def _index(rules: tuple[SecurityRule, ...]) -> dict[str, tuple[SecurityRule, ...]]:
    index: dict[str, list[SecurityRule]] = {}
    for rule in rules:
        index.setdefault(rule.method, []).append(rule)
    return {name: tuple(items) for name, items in index.items()}


_RULES_BY_METHOD: dict[str, tuple[SecurityRule, ...]] = _index(_SOURCES + _SINKS + _SANITIZERS)

# All rules, exposed for callers that want the full catalog.
ALL_RULES: tuple[SecurityRule, ...] = _SOURCES + _SINKS + _SANITIZERS


def classify_invocation(method_name: str | None, receiver: str | None) -> SecurityRule | None:
    """Return the best-matching :class:`SecurityRule` for a call site, if any.

    Args:
        method_name: The invoked method name, or the constructed type for
            ``object_creation_expression`` nodes (``new Foo(...)`` → ``Foo``).
        receiver: The textual receiver / qualifier of the call (may be ``None``).

    Returns:
        The matching rule, preferring rules whose ``receiver_hint`` is satisfied
        over generic (receiver-agnostic) rules, or ``None`` when nothing matches.
    """
    if not method_name:
        return None
    candidates = _RULES_BY_METHOD.get(method_name)
    if not candidates:
        return None

    recv = receiver or ""
    hinted: SecurityRule | None = None
    generic: SecurityRule | None = None
    for rule in candidates:
        if rule.receiver_hint is None:
            generic = generic or rule
        elif rule.receiver_hint.lower() in recv.lower():
            hinted = hinted or rule
    # A receiver-specific match is more precise; fall back to a generic rule.
    return hinted or generic
