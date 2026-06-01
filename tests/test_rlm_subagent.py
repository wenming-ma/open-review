from __future__ import annotations

import inspect
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

import agent.rlm.subagent as rlm_subagent
from agent.rlm.subagent import (
    ReadOnlyRepoAnalysisBackend,
    RepoAnalystRLMRunner,
    RepoAnalystSubagentRunnable,
    _OpenReviewLangChainRLMClient,
    _build_rlm_backend_config,
    _extract_instruction,
    _extract_query_spec,
    _patched_rlm_get_client,
)
from agent.utils.structured_output import SimpleSubagentResult


class _FakeRLMRunner:
    def __init__(self):
        self.calls = []
        self.scene = "mention"
        self.model_id = "test-model"

    async def arun(self, *, instruction: str, context: dict, config: dict | None = None) -> str:
        self.calls.append(
            {
                "instruction": instruction,
                "context": context,
                "config": config,
            }
        )
        return "repo analysis result"

    def _build_context_payload(self):
        return {
            "project_id": "team/project",
            "mr_iid": 15,
            "run_id": "run-1",
        }


class _RecordingSpan:
    def __init__(self, records: list[dict], *, name: str, attributes: dict, tags: list[str], span_kind: str):
        self.record = {
            "name": name,
            "attributes": attributes,
            "tags": tags,
            "span_kind": span_kind,
            "input": None,
            "output": None,
            "events": [],
            "error": None,
            "exceptions": [],
        }
        records.append(self.record)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def set_input(self, value):
        self.record["input"] = value

    def set_output(self, value):
        self.record["output"] = value

    def add_event(self, name, attributes=None):
        self.record["events"].append((name, attributes or {}))

    def record_exception(self, exc):
        self.record["exceptions"].append(type(exc).__name__)

    def set_error_status(self, message):
        self.record["error"] = message


def _install_recording_span(monkeypatch):
    records: list[dict] = []

    def fake_start_open_review_span(name, *, attributes=None, metadata=None, tags=None, span_kind=None, **kwargs):
        del metadata, kwargs
        return _RecordingSpan(
            records,
            name=name,
            attributes=attributes or {},
            tags=tags or [],
            span_kind=span_kind or "",
        )

    monkeypatch.setattr(rlm_subagent, "start_open_review_span", fake_start_open_review_span, raising=False)
    return records


async def test_repo_analyst_subagent_returns_messages_state():
    runner = _FakeRLMRunner()
    subagent = RepoAnalystSubagentRunnable(runner=runner, name="repo-analyst")

    result = await subagent.ainvoke(
        {
            "messages": [
                {"role": "user", "content": "Analyze the repository and identify impact paths."},
            ]
        },
        config={"configurable": {"thread_id": "rlm-thread-1"}},
    )

    assert "messages" in result
    assert "structured_response" in result
    assert len(result["messages"]) == 1
    assert isinstance(result["messages"][0], AIMessage)
    assert result["messages"][0].content == "repo analysis result"
    assert result["structured_response"] == SimpleSubagentResult(result="repo analysis result")
    assert runner.calls[0]["instruction"] == "Analyze the repository and identify impact paths."


async def test_repo_analyst_subagent_emits_named_open_review_span(monkeypatch):
    records = _install_recording_span(monkeypatch)
    runner = _FakeRLMRunner()
    subagent = RepoAnalystSubagentRunnable(runner=runner, name="repo-analyst")

    await subagent.ainvoke(
        {"messages": [{"role": "user", "content": "Trace cross-file impact."}]},
        config={"configurable": {"thread_id": "thread-1"}},
    )

    assert len(records) == 1
    span = records[0]
    assert span["name"] == "open_review.mention.subagent.repo-analyst"
    assert span["span_kind"] == "agent"
    assert span["attributes"] == {
        "open_review.rlm": True,
        "open_review.scene": "mention",
        "open_review.subagent_type": "repo-analyst",
        "open_review.model_id": "test-model",
        "open_review.project_id": "team/project",
        "open_review.mr_iid": 15,
        "open_review.run_id": "run-1",
        "open_review.thread_id": "thread-1",
    }
    assert span["tags"] == ["mention", "subagent", "repo-analyst", "rlm"]
    assert span["input"]["instruction"] == "Trace cross-file impact."
    assert span["output"]["result"] == "repo analysis result"
    assert span["events"][-1][0] == "invoke_completed"


async def test_repo_analyst_subagent_records_span_error(monkeypatch):
    records = _install_recording_span(monkeypatch)

    class _FailingRunner(_FakeRLMRunner):
        async def arun(self, *, instruction: str, context: dict, config: dict | None = None) -> str:
            del instruction, context, config
            raise RuntimeError("rlm failed")

    subagent = RepoAnalystSubagentRunnable(runner=_FailingRunner(), name="repo-analyst")

    try:
        await subagent.ainvoke(
            {"messages": [{"role": "user", "content": "Trace cross-file impact."}]},
            config={"configurable": {"thread_id": "thread-1"}},
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError")

    assert len(records) == 1
    assert records[0]["name"] == "open_review.mention.subagent.repo-analyst"
    assert records[0]["exceptions"] == ["RuntimeError"]
    assert records[0]["error"] == "rlm failed"
    assert records[0]["events"][-1] == (
        "invoke_failed",
        {"error_type": "RuntimeError", "instruction_present": True},
    )


def test_repo_analyst_extracts_langchain_human_message_content():
    instruction = _extract_instruction({"messages": [HumanMessage(content="real request")]})

    assert instruction == "real request"


def test_repo_analyst_backend_blocks_git_mutation_with_global_options():
    class _Backend:
        cwd = "/tmp/repo"
        root_dir = "/tmp/repo"
        host_root_dir = "/tmp/repo"

        def execute(self, command: str, *, timeout: int | None = None):
            return SimpleNamespace(output=f"executed:{command}", exit_code=0, truncated=False)

    backend = ReadOnlyRepoAnalysisBackend(_Backend())

    for command in (
        "git -C /tmp/repo push origin HEAD",
        "git -c user.name=a commit -m x",
        "command git push origin HEAD",
        "env GIT_DIR=.git git reset --hard HEAD~1",
    ):
        result = backend.execute(command)
        assert result.exit_code == 126
        assert "blocked" in result.output


def test_repo_analyst_backend_blocks_shell_write_primitives():
    class _Backend:
        cwd = "/tmp/repo"
        root_dir = "/tmp/repo"
        host_root_dir = "/tmp/repo"

        def execute(self, command: str, *, timeout: int | None = None):
            return SimpleNamespace(output=f"executed:{command}", exit_code=0, truncated=False)

    backend = ReadOnlyRepoAnalysisBackend(_Backend())

    for command in (
        "echo hacked > tracked.txt",
        "cat README.md >> notes.txt",
        "tee tracked.txt",
        "rm -rf build",
        "sed -i 's/a/b/' src/file.cpp",
        "awk 'BEGIN { system(\"touch tracked.txt\") }'",
        "find . -name '*.tmp' -delete",
        "python -c 'open(\"tracked.txt\", \"w\").write(\"bad\")'",
    ):
        result = backend.execute(command)
        assert result.exit_code == 126
        assert "read-only" in result.output


def test_repo_analyst_backend_allows_read_only_shell_inspection():
    class _Backend:
        cwd = "/tmp/repo"
        root_dir = "/tmp/repo"
        host_root_dir = "/tmp/repo"

        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute(self, command: str, *, timeout: int | None = None):
            self.commands.append(command)
            return SimpleNamespace(output=f"executed:{command}", exit_code=0, truncated=False)

    raw_backend = _Backend()
    backend = ReadOnlyRepoAnalysisBackend(raw_backend)

    for command in (
        "pwd",
        "ls -la",
        "rg 'needle' . | head -20",
        "find . -name '*.py' | sort",
        "head -40 README.md",
        "cat README.md | cut -d ' ' -f 1 | uniq",
        "git status --short --branch",
        "git diff --unified=3 HEAD~1 -- agent/rlm/subagent.py",
        "cd /tmp/repo && git grep repo-analyst",
    ):
        result = backend.execute(command)
        assert result.exit_code == 0

    assert raw_backend.commands


def test_repo_analyst_extracts_structured_question_paths_and_keywords():
    spec = _extract_query_spec(
        {
            "question": "Where can this regression escape?",
            "file_paths": ["src/a.cpp", " include/a.h ", ""],
            "keywords": ["Foo::bar", "  netlist  ", ""],
        }
    )

    assert spec.question == "Where can this regression escape?"
    assert spec.file_paths == ["src/a.cpp", "include/a.h"]
    assert spec.keywords == ["Foo::bar", "netlist"]


def test_repo_analyst_extracts_json_query_spec_from_message_text():
    spec = _extract_query_spec(
        {
            "messages": [
                {
                    "role": "user",
                    "content": '{"question": "Trace symbol impact", "paths": ["src/b.cpp"], "keywords": ["RouterController"]}',
                }
            ]
        }
    )

    assert spec.question == "Trace symbol impact"
    assert spec.file_paths == ["src/b.cpp"]
    assert spec.keywords == ["RouterController"]


def test_repo_analyst_routes_custom_anthropic_base_url_through_project_model_factory():
    backend, kwargs = _build_rlm_backend_config(
        SimpleNamespace(
            provider="anthropic",
            model="MiniMax-M2.7",
            model_id="anthropic:MiniMax-M2.7",
            api_key="test-key",
            base_url="https://api.minimax.io/anthropic",
        )
    )

    assert backend == "open_review_langchain"
    assert kwargs == {
        "model_name": "anthropic:MiniMax-M2.7",
        "model_id": "anthropic:MiniMax-M2.7",
        "snapshot": {
            "LLM_ACTIVE_PROVIDER": "anthropic",
            "LLM_MODEL_ID": "anthropic:MiniMax-M2.7",
            "ANTHROPIC_MODEL": "MiniMax-M2.7",
            "ANTHROPIC_API_KEY": "test-key",
            "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
        },
    }


def test_repo_analyst_keeps_native_anthropic_for_default_base_url():
    backend, kwargs = _build_rlm_backend_config(
        SimpleNamespace(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="test-key",
            base_url="https://api.anthropic.com",
        )
    )

    assert backend == "anthropic"
    assert kwargs == {
        "model_name": "claude-sonnet-4-6",
        "api_key": "test-key",
    }


def test_repo_analyst_rlm_constructor_options_match_installed_rlms():
    from rlm import RLM

    supported = set(inspect.signature(RLM.__init__).parameters)

    assert {
        "backend",
        "backend_kwargs",
        "environment",
        "max_depth",
        "max_iterations",
        "max_timeout",
        "custom_tools",
    } <= supported
    assert "max_concurrent_subcalls" not in supported


def test_repo_analyst_langchain_client_extracts_text_after_thinking_blocks():
    response = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "<repl>FINAL_VAR('ok')</repl>"},
        ]
    )

    assert _OpenReviewLangChainRLMClient._response_text(response) == "<repl>FINAL_VAR('ok')</repl>"


def test_repo_analyst_langchain_client_detects_placeholder_final_answer():
    assert _OpenReviewLangChainRLMClient._needs_response_retry("")
    assert _OpenReviewLangChainRLMClient._needs_response_retry("   ")
    assert _OpenReviewLangChainRLMClient._is_placeholder_final_answer("final_answer")
    assert _OpenReviewLangChainRLMClient._is_placeholder_final_answer("FINAL(final_answer)")
    assert not _OpenReviewLangChainRLMClient._is_placeholder_final_answer("FINAL_VAR(final_answer)")
    assert not _OpenReviewLangChainRLMClient._needs_response_retry("The real regression is raw text display.")


def test_repo_analyst_langchain_client_normalizes_rlm_default_answer_prompt():
    messages, system = _OpenReviewLangChainRLMClient._prepare_messages(
        [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "Please provide a final answer to the user's question based on the information provided."},
        ]
    )

    assert system == "system rules"
    assert messages[-1]["role"] == "user"
    assert "Write directly in natural language" in messages[-1]["content"]
    assert "REPL blocks" in messages[-1]["content"]


def test_repo_analyst_langchain_client_uses_project_model_factory(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeModel:
        def invoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content=[{"type": "text", "text": "model answer"}])

    def _fake_make_model_from_snapshot(snapshot, model_id=None, **kwargs):
        captured["snapshot"] = snapshot
        captured["model_id"] = model_id
        captured["kwargs"] = kwargs
        return _FakeModel()

    monkeypatch.setattr(rlm_subagent, "make_model_from_snapshot", _fake_make_model_from_snapshot)

    client = _OpenReviewLangChainRLMClient(
        snapshot={"LLM_ACTIVE_PROVIDER": "anthropic"},
        model_id="anthropic:MiniMax-M2.7",
    )

    assert client.completion([{"role": "user", "content": "question"}]) == "model answer"
    assert captured["snapshot"] == {"LLM_ACTIVE_PROVIDER": "anthropic"}
    assert captured["model_id"] == "anthropic:MiniMax-M2.7"
    assert captured["kwargs"] == {"temperature": 0, "max_tokens": 32768}


def test_repo_analyst_patched_final_parser_resolves_minimax_placeholder_from_repl_variable():
    import rlm.core.rlm as rlm_core

    environment = SimpleNamespace(locals={"final_answer": "real analysis with evidence"})

    with _patched_rlm_get_client():
        assert rlm_core.find_final_answer("FINAL(final_answer)", environment=environment) == "real analysis with evidence"


def test_repo_analyst_patched_final_parser_suppresses_unresolved_placeholder():
    import rlm.core.rlm as rlm_core

    environment = SimpleNamespace(locals={})

    with _patched_rlm_get_client():
        assert rlm_core.find_final_answer("FINAL(final_answer)", environment=environment) is None


def test_repo_analyst_runner_invokes_original_rlm_with_repl_variables(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeRLM:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def completion(self, prompt, root_prompt=None):
            captured["prompt"] = prompt
            captured["root_prompt"] = root_prompt
            return SimpleNamespace(response="analysis result")

    class _Backend:
        cwd = "/repo"
        root_dir = "/repo"
        host_root_dir = "/repo"

        def __init__(self) -> None:
            self.reads: list[str] = []

        def read(self, file_path: str, offset: int = 0, limit: int = 4000):
            del offset, limit
            self.reads.append(file_path)
            return SimpleNamespace(
                error=None,
                file_data={"content": f"content for {file_path}", "encoding": "utf-8"},
            )

        def grep(self, pattern: str, path: str | None = None, glob: str | None = None):
            del path, glob
            matches_by_pattern = {
                "alpha": [
                    {"path": "/repo/src/top.cpp", "line": 1, "text": "alpha alpha"},
                    {"path": "/repo/src/top.cpp", "line": 2, "text": "alpha again"},
                    {"path": "/repo/src/second.cpp", "line": 3, "text": "alpha"},
                ],
                "beta": [
                    {"path": "/repo/src/top.cpp", "line": 4, "text": "beta"},
                    {"path": "/repo/src/beta.cpp", "line": 5, "text": "beta"},
                ],
            }
            return SimpleNamespace(error=None, matches=matches_by_pattern.get(pattern, []))

        def execute(self, command: str, *, timeout: int | None = None):
            del timeout
            return SimpleNamespace(output=f"executed:{command}", exit_code=0, truncated=False)

        def ls(self, path: str):
            return SimpleNamespace(error=None, entries=[])

        def glob(self, pattern: str, path: str = "/"):
            del pattern, path
            return SimpleNamespace(error=None, matches=[])

        def download_files(self, paths: list[str]):
            return []

    def _review_scope(file_path: str | None = None):
        if file_path:
            return {"path": file_path, "diff": f"diff for {file_path}", "added_lines": [10]}
        return {"changed_files": [{"path": "src/changed.cpp", "status": "modified"}]}

    raw_backend = _Backend()
    runner = RepoAnalystRLMRunner(
        scene="auto_review",
        backend=ReadOnlyRepoAnalysisBackend(raw_backend),
        repo_dir="/repo",
        file_tool_repo_dir="/repo",
        shell_repo_dir="/repo",
        model_id="openai:gpt-test",
        extra_tools={"review_scope": _review_scope},
    )

    monkeypatch.setattr(rlm_subagent, "_import_rlm_class", lambda: _FakeRLM)

    result = runner.run(
        instruction="Fallback question",
        context={
            "scene": "auto_review",
            "repo_root": "/repo",
            "shell_repo_root": "/repo",
            "query_spec": {
                "question": "Trace the risk",
                "file_paths": ["src/requested.cpp"],
                "keywords": ["alpha", "beta"],
            },
        },
    )

    assert result == SimpleSubagentResult(result="analysis result")
    assert captured["root_prompt"] == "Trace the risk"

    init = captured["init"]
    assert init["backend"] == "openai"
    assert init["backend_kwargs"]["model_name"] == "gpt-test"
    assert init["environment"] == "local"
    assert init["max_depth"] == 5
    assert init["max_iterations"] == 64
    assert init["max_timeout"] == 1800.0
    assert "max_concurrent_subcalls" not in init
    assert "Do not output bare placeholder text" in init["custom_system_prompt"]
    assert "If you use `FINAL_VAR(final_answer)`" in init["custom_system_prompt"]
    assert "requested_files_content" in init["custom_system_prompt"]

    custom_tools = init["custom_tools"]
    assert custom_tools["task"]["tool"] == "Trace the risk"
    assert callable(custom_tools["repo_read"]["tool"])
    assert custom_tools["requested_files_content"]["tool"]["src/requested.cpp"]["content"].endswith(
        "/repo/src/requested.cpp"
    )
    assert custom_tools["changed_files_content"]["tool"]["src/changed.cpp"]["content"].endswith(
        "/repo/src/changed.cpp"
    )
    assert custom_tools["diffs"]["tool"]["src/changed.cpp"]["diff"] == "diff for src/changed.cpp"
    assert list(custom_tools["keyword_top_files_content"]["tool"]["alpha"]) == [
        "src/top.cpp",
        "src/second.cpp",
    ]
    assert custom_tools["keyword_counts"]["tool"]["alpha"] == {
        "src/top.cpp": 2,
        "src/second.cpp": 1,
    }
    assert "src/requested.cpp" in custom_tools["all_selected_files_text"]["tool"]
    assert captured["prompt"] == custom_tools["corpus_manifest"]["tool"]


async def test_repo_analyst_subagent_passes_query_spec_to_runner():
    class _Runner(_FakeRLMRunner):
        def _build_context_payload(self):
            return {"scene": "mention", "project_id": "team/project"}

    runner = _Runner()
    subagent = RepoAnalystSubagentRunnable(runner=runner, name="repo-analyst")

    await subagent.ainvoke(
        {
            "question": "Find callers",
            "file_paths": ["src/c.cpp"],
            "keywords": ["caller_symbol"],
        }
    )

    assert runner.calls[0]["instruction"] == "Find callers"
    assert runner.calls[0]["context"]["query_spec"] == {
        "question": "Find callers",
        "file_paths": ["src/c.cpp"],
        "keywords": ["caller_symbol"],
    }
