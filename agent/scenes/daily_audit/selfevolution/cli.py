"""CLI entrypoints for sandbox-local daily audit self-evolution actions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.scenes.daily_audit.selfevolution.engine import (
    apply_evolved_code_direct_merge,
    apply_evolved_prompt_direct_merge,
    apply_evolved_skill_direct_merge,
    apply_evolved_tool_description_direct_merge,
    run_daily_audit_code_evolution,
    run_daily_audit_prompt_evolution,
    run_daily_audit_skill_evolution,
    run_daily_audit_tool_description_evolution,
)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-audit-evolution-cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("skill-evolve", "prompt-evolve", "tool-evolve", "code-evolve"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--project-id", required=True)
        sub.add_argument("--iterations", type=int, default=10)
        if name == "skill-evolve":
            sub.add_argument("--skill-name", required=True)
        elif name == "code-evolve":
            sub.add_argument("--target-path", required=True)
        else:
            sub.add_argument("--target-name", required=True)

    for name in ("apply-skill", "apply-prompt", "apply-tool", "apply-code"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--project-id", required=True)
        sub.add_argument("--candidate-path", required=True)
        sub.add_argument("--default-branch", required=True)
        if name == "apply-skill":
            sub.add_argument("--skill-name", required=True)
        elif name == "apply-code":
            sub.add_argument("--target-path", required=True)
        else:
            sub.add_argument("--target-name", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "skill-evolve":
        candidate_path, evaluation = run_daily_audit_skill_evolution(
            project_id=args.project_id,
            skill_name=args.skill_name,
            iterations=args.iterations,
            return_metadata=True,
        )
        _print_json(
            {
                "candidate_path": str(candidate_path),
                "baseline_score": evaluation.baseline_score,
                "candidate_score": evaluation.candidate_score,
                "heldout_examples": evaluation.heldout_examples,
                "gate_reason": evaluation.gate_reason,
            }
        )
        return 0

    if args.command == "prompt-evolve":
        candidate_path, evaluation = run_daily_audit_prompt_evolution(
            project_id=args.project_id,
            target_name=args.target_name,
            iterations=args.iterations,
            return_metadata=True,
        )
        _print_json(
            {
                "candidate_path": str(candidate_path),
                "baseline_score": evaluation.baseline_score,
                "candidate_score": evaluation.candidate_score,
                "heldout_examples": evaluation.heldout_examples,
                "gate_reason": evaluation.gate_reason,
            }
        )
        return 0

    if args.command == "tool-evolve":
        candidate_path, evaluation = run_daily_audit_tool_description_evolution(
            project_id=args.project_id,
            target_name=args.target_name,
            iterations=args.iterations,
            return_metadata=True,
        )
        _print_json(
            {
                "candidate_path": str(candidate_path),
                "baseline_score": evaluation.baseline_score,
                "candidate_score": evaluation.candidate_score,
                "heldout_examples": evaluation.heldout_examples,
                "gate_reason": evaluation.gate_reason,
            }
        )
        return 0

    if args.command == "code-evolve":
        candidate_path = run_daily_audit_code_evolution(
            project_id=args.project_id,
            target_path=args.target_path,
            iterations=args.iterations,
        )
        _print_json({"candidate_path": str(candidate_path)})
        return 0

    if args.command == "apply-skill":
        commit_sha = apply_evolved_skill_direct_merge(
            project_id=args.project_id,
            skill_name=args.skill_name,
            candidate_path=Path(args.candidate_path),
            repo_dir="",
            default_branch=args.default_branch,
        )
        _print_json({"commit_sha": commit_sha})
        return 0

    if args.command == "apply-prompt":
        commit_sha = apply_evolved_prompt_direct_merge(
            project_id=args.project_id,
            target_name=args.target_name,
            candidate_path=Path(args.candidate_path),
            repo_dir="",
            default_branch=args.default_branch,
        )
        _print_json({"commit_sha": commit_sha})
        return 0

    if args.command == "apply-tool":
        commit_sha = apply_evolved_tool_description_direct_merge(
            project_id=args.project_id,
            target_name=args.target_name,
            candidate_path=Path(args.candidate_path),
            repo_dir="",
            default_branch=args.default_branch,
        )
        _print_json({"commit_sha": commit_sha})
        return 0

    if args.command == "apply-code":
        commit_sha = apply_evolved_code_direct_merge(
            project_id=args.project_id,
            target_path=args.target_path,
            candidate_path=Path(args.candidate_path),
            repo_dir="",
            default_branch=args.default_branch,
        )
        _print_json({"commit_sha": commit_sha})
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
