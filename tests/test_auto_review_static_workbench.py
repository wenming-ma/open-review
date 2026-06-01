from __future__ import annotations

import subprocess
from pathlib import Path

from deepagents.backends.protocol import ExecuteResponse

from agent.scenes.auto_review.models import ChangedFileContext, ReviewContext
from agent.scenes.auto_review.static_workbench import build_static_workbench_tools


class _LocalBackend:
    def __init__(self, root: Path) -> None:
        self.cwd = str(root)
        self.commands: list[str] = []

    def execute(self, command: str, timeout: int | None = None) -> ExecuteResponse:
        self.commands.append(command)
        result = subprocess.run(
            command,
            cwd=self.cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += f"\n[stderr] {result.stderr}"
        return ExecuteResponse(output=output, exit_code=result.returncode, truncated=False)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000):
        del offset
        path = Path(file_path)
        if not path.is_absolute():
            path = Path(self.cwd) / file_path
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"error": str(exc)}
        return {"file_data": {"content": content[:limit], "encoding": "utf-8"}}


def _review_context(repo_dir: Path, *, changed_files: list[ChangedFileContext]) -> ReviewContext:
    return ReviewContext(
        project_id="team/service",
        mr_iid=7,
        title="Update connectivity",
        source_branch="feature/connectivity",
        target_branch="main",
        base_sha="base",
        start_sha="start",
        head_sha="head",
        repo_dir=str(repo_dir),
        review_run_id="run-1",
        review_mode="full",
        diff_range="base...head",
        commit_range="base..head",
        diff_text="\n".join(item.diff for item in changed_files),
        diff_fingerprint="abc123",
        changed_files=changed_files,
    )


def _tools(backend: _LocalBackend, repo_dir: Path, context: ReviewContext):
    return {tool.__name__: tool for tool in build_static_workbench_tools(backend, str(repo_dir), context)}


def test_semantic_diff_extracts_symbols_from_renamed_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    source = repo / "alpha" / "beta" / "logic_unit.cc"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            [
                "class BOARD_CONNECTIVITY {};",
                "void BOARD_CONNECTIVITY::RebuildNets() {",
                "    RecomputeRatsnest();",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    changed = ChangedFileContext(
        file_path="alpha/beta/logic_unit.cc",
        old_path="alpha/beta/logic_unit.cc",
        diff=(
            "diff --git a/alpha/beta/logic_unit.cc b/alpha/beta/logic_unit.cc\n"
            "+class BOARD_CONNECTIVITY {};\n"
            "+void BOARD_CONNECTIVITY::RebuildNets() {\n"
            "+    RecomputeRatsnest();\n"
            "+}\n"
        ),
        added_lines=[1, 2, 3, 4],
    )

    result = _tools(_LocalBackend(tmp_path), repo, _review_context(repo, changed_files=[changed]))[
        "semantic_diff"
    ]()

    assert result["changed_files"][0]["file_path"] == "alpha/beta/logic_unit.cc"
    assert "BOARD_CONNECTIVITY" in result["candidate_symbols"]
    assert "BOARD_CONNECTIVITY::RebuildNets" in result["candidate_symbols"]
    assert "RecomputeRatsnest" in result["candidate_symbols"]


def test_format_probe_uses_content_tokens_for_renamed_extensions(tmp_path: Path):
    repo = tmp_path / "repo"
    design = repo / "fixtures" / "sample.projectx"
    design.parent.mkdir(parents=True)
    design.write_text(
        '{"schema": "invoice.v1", "serializer": "json", "fields": ["id", "total"]}\n',
        encoding="utf-8",
    )
    context = _review_context(repo, changed_files=[])

    result = _tools(_LocalBackend(tmp_path), repo, context)["format_probe"](
        file_path="fixtures/sample.projectx"
    )

    assert result["file_path"] == "fixtures/sample.projectx"
    assert "schema" in result["content_tokens"]
    assert "serializer" in result["content_tokens"]
    assert result["extension_used_for_classification"] is False


def test_repo_capabilities_reports_dependency_manifests_as_limitations(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vcpkg.json").write_text('{"dependencies": ["wxwidgets"]}\n', encoding="utf-8")
    context = _review_context(repo, changed_files=[])

    result = _tools(_LocalBackend(tmp_path), repo, context)["repo_capabilities"]()

    assert "vcpkg.json" in result["dependency_manifests"]
    assert "third_party_dependencies_declared_not_installed_by_static_workbench" in result["evidence_limitations"]
    assert "source_text_search" in result["safe_evidence_sources"]


def test_target_context_reads_cmake_without_configuring_or_building(tmp_path: Path):
    repo = tmp_path / "repo"
    source = repo / "alpha" / "beta" / "logic_unit.cc"
    source.parent.mkdir(parents=True)
    source.write_text("void f() {}\n", encoding="utf-8")
    (repo / "CMakeLists.txt").write_text(
        "add_library(service_core alpha/beta/logic_unit.cc)\n"
        "add_executable(service_app main.cc)\n",
        encoding="utf-8",
    )
    backend = _LocalBackend(tmp_path)
    context = _review_context(repo, changed_files=[])

    result = _tools(backend, repo, context)["target_context"]("alpha/beta/logic_unit.cc")

    assert "service_core" in result["candidate_targets"]
    assert any(item["path"] == "CMakeLists.txt" for item in result["evidence"])
    assert not any("cmake --build" in command or "cmake -S" in command for command in backend.commands)


def test_target_context_reads_package_manifest_without_installing(tmp_path: Path):
    repo = tmp_path / "repo"
    source = repo / "src" / "app.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export function main() { return 1; }\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{\n  "scripts": {\n    "build": "vite build src/app.ts"\n  }\n}\n',
        encoding="utf-8",
    )
    backend = _LocalBackend(tmp_path)
    context = _review_context(repo, changed_files=[])

    result = _tools(backend, repo, context)["target_context"]("src/app.ts")

    assert "build" in result["candidate_targets"]
    assert any(item["path"] == "package.json" for item in result["evidence"])
    assert not any("npm install" in command or "npm run" in command for command in backend.commands)


def test_symbol_impact_returns_references_and_candidate_declarations(tmp_path: Path):
    repo = tmp_path / "repo"
    source = repo / "engine" / "connectivity.cc"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class BOARD_CONNECTIVITY {};\n"
        "void BOARD_CONNECTIVITY::RebuildNets() {}\n"
        "void Refresh() { BOARD_CONNECTIVITY connectivity; connectivity.RebuildNets(); }\n",
        encoding="utf-8",
    )
    context = _review_context(repo, changed_files=[])

    result = _tools(_LocalBackend(tmp_path), repo, context)["symbol_impact"]("BOARD_CONNECTIVITY")

    assert result["symbol"] == "BOARD_CONNECTIVITY"
    assert len(result["references"]) >= 2
    assert any("class BOARD_CONNECTIVITY" in item["text"] for item in result["candidate_declarations"])
