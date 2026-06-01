"""Static evidence tools for agentic auto-review investigations.

These tools intentionally expose facts and candidates instead of assigning
authoritative domain boundaries. Director and specialist agents decide how to
interpret the evidence at review time.
"""

from __future__ import annotations

import base64
import json
import os
import re
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from agent.sandbox.manager import sandbox_file_tool_path, sandbox_shell_path
from agent.scenes.auto_review.models import ChangedFileContext, ReviewContext
from agent.scenes.auto_review.scope import changed_file_status

_GENERIC_TOOLS = (
    "rg",
    "git",
    "ctags",
    "cscope",
    "global",
    "python3",
    "node",
    "npm",
    "pytest",
    "go",
    "cargo",
    "rustc",
    "javac",
    "mvn",
    "gradle",
    "make",
    "shellcheck",
    "jq",
    "clangd",
    "clang-tidy",
    "cppcheck",
    "cmake",
)
_DEPENDENCY_MANIFESTS = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "uv.lock",
    "setup.py",
    "setup.cfg",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "Cargo.lock",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradle.lockfile",
    "composer.json",
    "composer.lock",
    "Gemfile",
    "Gemfile.lock",
    "go.work",
    "WORKSPACE",
    "WORKSPACE.bazel",
    "MODULE.bazel",
    "vcpkg.json",
    "conanfile.txt",
    "conanfile.py",
    "CMakePresets.json",
    "CMakeUserPresets.json",
}
_BUILD_FILE_NAMES = {
    "BUILD",
    "BUILD.bazel",
    "Dockerfile",
    "Makefile",
    "Rakefile",
    "Taskfile.yml",
    "Taskfile.yaml",
    "Justfile",
    "CMakeLists.txt",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
}
_BUILD_FILE_SUFFIXES = (".bazel", ".bzl", ".cmake", ".gradle", ".gradle.kts", ".mk")
_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "__pycache__",
    "build",
    "cmake-build-debug",
    "cmake-build-release",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
_SYMBOL_KEYWORDS = {
    "and",
    "async",
    "await",
    "break",
    "case",
    "catch",
    "const",
    "continue",
    "def",
    "defer",
    "del",
    "do",
    "elif",
    "else",
    "enum",
    "except",
    "false",
    "finally",
    "fn",
    "func",
    "if",
    "import",
    "in",
    "interface",
    "let",
    "match",
    "new",
    "nil",
    "none",
    "null",
    "or",
    "package",
    "pass",
    "raise",
    "for",
    "while",
    "switch",
    "return",
    "select",
    "sizeof",
    "static_cast",
    "dynamic_cast",
    "reinterpret_cast",
    "const_cast",
    "throw",
    "true",
    "try",
    "type",
    "var",
    "yield",
}
_FORMAT_TOKENS = (
    "api",
    "avro",
    "csv",
    "decoder",
    "deserialize",
    "encoder",
    "graphql",
    "json",
    "marshal",
    "migration",
    "openapi",
    "protobuf",
    "protocol",
    "schema",
    "serializer",
    "unmarshal",
    "xml",
    "yaml",
    "sexpr",
    "s-expression",
    "serialize",
    "parser",
    "parse",
    "writer",
    "reader",
    "format",
    "import",
    "export",
)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _execute(backend: SandboxBackendProtocol, command: str, *, timeout: int | None = 30) -> ExecuteResponse:
    return backend.execute(command, timeout=timeout)


def _output(result: ExecuteResponse) -> str:
    return str(getattr(result, "output", "") or "")


def _exit_code(result: ExecuteResponse) -> int:
    value = getattr(result, "exit_code", 1)
    return int(value) if value is not None else 1


def _json_output(result: ExecuteResponse, default: Any) -> Any:
    if _exit_code(result) != 0:
        return default
    text = _output(result).strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _repo_relative(repo_dir: str, path: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return path.replace("\\", "/")
    try:
        return candidate.resolve(strict=False).relative_to(Path(repo_dir).resolve(strict=False)).as_posix()
    except ValueError:
        return path.replace("\\", "/")


def _file_tool_path(backend: SandboxBackendProtocol, repo_dir: str, file_path: str) -> str:
    if os.path.isabs(file_path):
        return sandbox_file_tool_path(backend, file_path)
    return sandbox_file_tool_path(backend, os.path.join(repo_dir, file_path))


def _read_text(backend: SandboxBackendProtocol, repo_dir: str, file_path: str, *, limit: int = 20_000) -> str:
    result = backend.read(_file_tool_path(backend, repo_dir, file_path), 0, limit)
    if isinstance(result, dict):
        if result.get("error"):
            return ""
        file_data = result.get("file_data") or {}
        if isinstance(file_data, dict):
            return str(file_data.get("content") or "")
        return str(result.get("content") or "")
    error = getattr(result, "error", None)
    if error:
        return ""
    file_data = getattr(result, "file_data", None)
    if isinstance(file_data, dict):
        return str(file_data.get("content") or "")
    return str(getattr(file_data, "content", "") or "")


def _search_repo(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    query: str,
    *,
    path: str | None = None,
    glob: str | None = None,
    max_results: int = 80,
) -> list[dict[str, Any]]:
    root = sandbox_shell_path(backend, repo_dir)
    script = textwrap.dedent(
        f"""\
        python3 - <<'PY'
        import base64
        import fnmatch
        import json
        import os

        root = base64.b64decode("{_b64(root)}").decode("utf-8")
        query = base64.b64decode("{_b64(query)}").decode("utf-8")
        rel_path = base64.b64decode("{_b64(path or '')}").decode("utf-8")
        glob_pattern = base64.b64decode("{_b64(glob or '')}").decode("utf-8") or None
        max_results = {max(1, int(max_results))}
        excluded = {sorted(_EXCLUDED_DIRS)!r}

        target = os.path.join(root, rel_path) if rel_path and not os.path.isabs(rel_path) else (rel_path or root)
        needle = query.lower()
        results = []

        def include(path, rel, name):
            if not glob_pattern:
                return True
            return (
                fnmatch.fnmatch(rel, glob_pattern)
                or fnmatch.fnmatch(name, glob_pattern)
                or fnmatch.fnmatch(path, glob_pattern)
            )

        def scan_file(path):
            rel = os.path.relpath(path, root)
            name = os.path.basename(path)
            if not include(path, rel, name):
                return
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for index, line in enumerate(handle, start=1):
                        if needle in line.lower():
                            results.append({{"path": rel, "line": index, "text": line.rstrip("\\n")}})
                            if len(results) >= max_results:
                                return
            except OSError:
                return

        if os.path.isfile(target):
            scan_file(target)
        elif os.path.isdir(target):
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [name for name in dirnames if name not in excluded]
                for filename in filenames:
                    scan_file(os.path.join(dirpath, filename))
                    if len(results) >= max_results:
                        break
                if len(results) >= max_results:
                    break

        print(json.dumps(results, ensure_ascii=True))
        PY"""
    )
    return list(_json_output(_execute(backend, script), []))


def _find_named_files(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    names: set[str],
    *,
    max_results: int = 80,
) -> list[str]:
    root = sandbox_shell_path(backend, repo_dir)
    script = textwrap.dedent(
        f"""\
        python3 - <<'PY'
        import base64
        import json
        import os

        root = base64.b64decode("{_b64(root)}").decode("utf-8")
        names = set({sorted(names)!r})
        max_results = {max(1, int(max_results))}
        excluded = {sorted(_EXCLUDED_DIRS)!r}
        results = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in excluded]
            for filename in filenames:
                if filename in names:
                    results.append(os.path.relpath(os.path.join(dirpath, filename), root))
                    if len(results) >= max_results:
                        print(json.dumps(results, ensure_ascii=True))
                        raise SystemExit(0)
        print(json.dumps(results, ensure_ascii=True))
        PY"""
    )
    return list(_json_output(_execute(backend, script), []))


def _changed_file_matches(item: ChangedFileContext, file_path: str | None) -> bool:
    if not file_path:
        return True
    normalized = file_path.strip().lstrip("/")
    return normalized in {item.file_path, item.old_path}


def _added_lines(diff: str) -> list[str]:
    lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("+++") or not raw.startswith("+"):
            continue
        lines.append(raw[1:])
    return lines


def _extract_symbols(text: str) -> list[str]:
    symbols: list[str] = []
    for match in re.finditer(
        r"\b(?:class|struct|interface|trait|type|enum(?:\s+class)?)\s+([A-Za-z_$][\w$]*)",
        text,
    ):
        symbols.append(match.group(1))
    for match in re.finditer(
        r"\b(?:def|function|func|fn)\s+([A-Za-z_$][\w$]*)",
        text,
    ):
        symbols.append(match.group(1))
    for match in re.finditer(
        r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
        text,
    ):
        symbols.append(match.group(1))
    for match in re.finditer(r"\b((?:[A-Za-z_$][\w$]*(?:::|\.))*[A-Za-z_$][\w$]*)\s*\(", text):
        symbol = match.group(1)
        if symbol.lower() in _SYMBOL_KEYWORDS:
            continue
        symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _looks_like_declaration(line: str, symbol: str) -> bool:
    escaped = re.escape(symbol)
    if re.search(rf"\b(?:class|struct|interface|trait|type|enum(?:\s+class)?)\s+{escaped}\b", line):
        return True
    if re.search(rf"\b(?:def|function|func|fn)\s+{escaped}\b", line):
        return True
    if "::" in symbol and re.search(rf"\b{escaped}\s*\(", line):
        return True
    return bool(re.search(rf"\b{escaped}\b.*[{{;]", line))


def _build_file_filter(path: str) -> bool:
    name = os.path.basename(path)
    return name in _BUILD_FILE_NAMES or path.endswith(_BUILD_FILE_SUFFIXES)


def _candidate_targets_from_line(line: str) -> list[str]:
    targets: list[str] = []
    for pattern in (
        r"\badd_library\s*\(\s*([A-Za-z0-9_.:+-]+)",
        r"\badd_executable\s*\(\s*([A-Za-z0-9_.:+-]+)",
        r"\btarget_sources\s*\(\s*([A-Za-z0-9_.:+-]+)",
        r"\b(?:pyproject|project|name)\s*=\s*[\"']([A-Za-z0-9_.:+@/-]+)[\"']",
        r"^\s*[\"']([A-Za-z0-9_.:+@/-]+)[\"']\s*:",
        r"^\s*([A-Za-z0-9_.:+@/-]+)\s*:",
        r"\bname\s*=\s*[\"']([A-Za-z0-9_.:+@/-]+)[\"']",
    ):
        for match in re.finditer(pattern, line):
            targets.append(match.group(1))
    return targets


def build_static_workbench_tools(
    backend: SandboxBackendProtocol,
    repo_dir: str,
    review_context: ReviewContext | None,
) -> list[Callable[..., dict[str, Any]]]:
    """Build static, dependency-light evidence tools for auto-review agents."""

    def repo_capabilities() -> dict[str, Any]:
        """Inspect static evidence capabilities and known validation limitations.

        This does not install dependencies, configure build systems, build targets, run tests,
        or query CI. Treat the result as environment evidence for planning.
        """

        command = "for tool in " + " ".join(_GENERIC_TOOLS) + '; do command -v "$tool" >/dev/null 2>&1 && echo "$tool"; done'
        found = set(_output(_execute(backend, command)).splitlines())
        manifests = _find_named_files(backend, repo_dir, _DEPENDENCY_MANIFESTS)
        limitations = [
            "no_ci_signal_available_to_tool",
            "no_full_dependency_install_assumed",
            "no_required_local_build_test_or_ci_validation",
        ]
        if manifests:
            limitations.append("third_party_dependencies_declared_not_installed_by_static_workbench")
        return {
            "available_tools": sorted(found),
            "missing_tools": [tool for tool in _GENERIC_TOOLS if tool not in found],
            "dependency_manifests": manifests,
            "safe_evidence_sources": [
                "review_scope",
                "source_text_search",
                "symbol_reference_search",
                "build_manifest_text_reading",
                "format_token_probe",
            ],
            "evidence_limitations": limitations,
        }

    def semantic_diff(file_path: str | None = None, max_files: int = 20) -> dict[str, Any]:
        """Extract changed snippets and candidate identifiers from the frozen MR diff.

        Identifiers are candidates only. They help plan investigation but do not define
        the domain or impact scope by themselves.
        """

        changed_files = list(review_context.changed_files if review_context is not None else [])
        selected = [item for item in changed_files if _changed_file_matches(item, file_path)]
        summaries: list[dict[str, Any]] = []
        all_symbols: list[str] = []
        for item in selected[: max(1, int(max_files))]:
            added = _added_lines(item.diff)
            symbols = _extract_symbols("\n".join(added))
            if not symbols:
                symbols = _extract_symbols(_read_text(backend, repo_dir, item.file_path, limit=8_000))
            all_symbols.extend(symbols)
            summaries.append(
                {
                    "file_path": item.file_path,
                    "old_path": item.old_path,
                    "status": changed_file_status(item),
                    "candidate_symbols": symbols,
                    "added_line_samples": added[:12],
                }
            )
        return {
            "source": "orchestrator_frozen_diff",
            "file_filter": file_path,
            "changed_files": summaries,
            "candidate_symbols": list(dict.fromkeys(all_symbols)),
            "interpretation": "candidate_symbols_are_non_binding_evidence",
        }

    def evidence_search(
        query: str,
        path: str | None = None,
        glob: str | None = None,
        max_results: int = 80,
    ) -> dict[str, Any]:
        """Search source text for evidence lines without assigning semantic labels."""

        normalized = query.strip()
        if not normalized:
            return {"query": query, "matches": [], "error": "empty_query"}
        matches = _search_repo(backend, repo_dir, normalized, path=path, glob=glob, max_results=max_results)
        return {
            "query": normalized,
            "path": path,
            "glob": glob,
            "matches": matches,
            "result_count": len(matches),
            "truncated": len(matches) >= max(1, int(max_results)),
        }

    def symbol_impact(symbol: str, max_results: int = 80) -> dict[str, Any]:
        """Find declarations and references for an identifier using static text evidence."""

        normalized = symbol.strip()
        if not normalized:
            return {"symbol": symbol, "references": [], "candidate_declarations": [], "error": "empty_symbol"}
        references = _search_repo(backend, repo_dir, normalized, max_results=max_results)
        declarations = [item for item in references if _looks_like_declaration(item.get("text", ""), normalized)]
        related_contract_files = [
            item
            for item in references
            if str(item.get("path", "")).endswith(
                (
                    ".d.ts",
                    ".graphql",
                    ".h",
                    ".hh",
                    ".hpp",
                    ".hxx",
                    ".json",
                    ".proto",
                    ".schema",
                    ".toml",
                    ".yaml",
                    ".yml",
                )
            )
        ][:20]
        return {
            "symbol": normalized,
            "references": references,
            "candidate_declarations": declarations,
            "related_contract_files": related_contract_files,
            "interpretation": "references_are_textual_candidates_not_a_complete_call_graph",
        }

    def target_context(file_path: str, max_results: int = 80) -> dict[str, Any]:
        """Read build, packaging, and task manifests textually for candidate ownership.

        This never configures a build system, builds targets, or runs tests.
        """

        normalized = file_path.strip().lstrip("/")
        basename = os.path.basename(normalized)
        matches = _search_repo(backend, repo_dir, basename or normalized, max_results=max_results)
        evidence = [item for item in matches if _build_file_filter(str(item.get("path", "")))]
        candidate_targets: list[str] = []
        for item in evidence:
            candidate_targets.extend(_candidate_targets_from_line(str(item.get("text", ""))))
        return {
            "file_path": normalized,
            "source_basename": basename,
            "candidate_targets": list(dict.fromkeys(candidate_targets)),
            "evidence": evidence,
            "limitations": [
                "build_system_was_not_configured",
                "target_ownership_is_textual_candidate_evidence",
            ],
        }

    def format_probe(
        file_path: str | None = None,
        query: str | None = None,
        max_results: int = 80,
    ) -> dict[str, Any]:
        """Probe schema, protocol, parser, serializer, import, or export evidence."""

        content_tokens: list[str] = []
        evidence: list[dict[str, Any]] = []
        if file_path:
            content = _read_text(backend, repo_dir, file_path, limit=40_000)
            lowered = content.lower()
            for token in _FORMAT_TOKENS:
                if token.lower() in lowered:
                    content_tokens.append(token)
            if content.lstrip().startswith("(") and content_tokens:
                content_tokens.append("s_expression")
            if content_tokens:
                first_line = next((line for line in content.splitlines() if line.strip()), "")
                evidence.append(
                    {
                        "path": file_path,
                        "line": 1,
                        "text": first_line[:240],
                        "source": "file_content",
                    }
                )

        search_terms = [query.strip()] if query and query.strip() else []
        if not search_terms:
            search_terms = [
                "parser",
                "writer",
                "serialize",
                "schema",
                "protocol",
                "migration",
                "format",
                "import",
                "export",
            ]
        search_matches: list[dict[str, Any]] = []
        for term in search_terms[:6]:
            search_matches.extend(_search_repo(backend, repo_dir, term, max_results=max(1, int(max_results)) // 2))
            if len(search_matches) >= max_results:
                break
        evidence.extend(search_matches[:max_results])
        return {
            "file_path": file_path,
            "query": query,
            "content_tokens": list(dict.fromkeys(content_tokens)),
            "evidence": evidence[:max_results],
            "extension_used_for_classification": False,
            "interpretation": "schema_protocol_format_evidence_is_content_and_code_based_not_extension_based",
        }

    return [
        repo_capabilities,
        semantic_diff,
        evidence_search,
        symbol_impact,
        target_context,
        format_probe,
    ]
