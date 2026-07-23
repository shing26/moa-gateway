from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class EvalResult:
    score: float
    need_human_review: bool
    issues: tuple[str, ...] = ()


class Evaluator(Protocol):
    async def score(self, output_text: str, intent: str) -> EvalResult: ...


# ---- AST static guardrail ----

_DANGEROUS_CALLS: set[str] = {
    "exec", "eval", "compile", "__import__",
}

_DANGEROUS_MODULES: set[str] = {
    "os", "subprocess", "shutil", "socket", "ctypes",
    "pickle", "shelve", "sqlite3", "telnetlib",
}

_DANGEROUS_METHODS: set[str] = {
    "system", "popen", "run", "call", "check_output",
    "rmtree", "remove", "unlink", "chmod", "chown",
}

_WRITE_MODES: set[str] = {"w", "wb", "a", "ab", "x", "xb"}


def _extract_call_names(node: ast.Call) -> list[str]:
    names: list[str] = []
    match node.func:
        case ast.Name(id=name):
            names.append(name)
        case ast.Attribute(value=ast.Name(id=obj), attr=attr):
            names.append(f"{obj}.{attr}")
            names.append(attr)
        case ast.Attribute(value=ast.Attribute(), attr=attr):
            names.append(attr)
    return names


def _check_ast_for_dangerous(text: str) -> list[str]:
    issues: list[str] = []
    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError:
        return issues

    for node in ast.walk(tree):
        match node:
            case ast.Call():
                names = _extract_call_names(node)
                for name in names:
                    if name in _DANGEROUS_CALLS or name.lower() in _DANGEROUS_CALLS:
                        issues.append(f"dangerous_call:{name}")
                    if name in _DANGEROUS_METHODS or name.lower() in _DANGEROUS_METHODS:
                        issues.append(f"dangerous_method:{name}")
                if any(n == "open" for n in names) and node.args:
                    for arg in node.args:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value in _WRITE_MODES:
                            issues.append(f"write_mode_open:{arg.value}")
            case ast.Import():
                for alias in node.names:
                    if alias.name in _DANGEROUS_MODULES or alias.name.split(".")[0] in _DANGEROUS_MODULES:
                        issues.append(f"dangerous_import:{alias.name}")
            case ast.ImportFrom():
                if node.module and (node.module in _DANGEROUS_MODULES or node.module.split(".")[0] in _DANGEROUS_MODULES):
                    issues.append(f"dangerous_import_from:{node.module}")

    return issues


class RuleEvaluator:
    MAX_RETRY = 2
    MAX_LENGTH = 20000

    async def score(self, output_text: str, intent: str) -> EvalResult:
        issues: list[str] = []
        text = output_text or ""

        if not text.strip():
            issues.append("empty_output")
            return EvalResult(score=0.0, need_human_review=True, issues=tuple(issues))

        if len(text) > self.MAX_LENGTH:
            issues.append("output_too_long")

        if self._looks_json(text):
            try:
                json.loads(text)
            except json.JSONDecodeError:
                issues.append("invalid_json_payload")

        if "TODO" in text or "FIXME" in text:
            issues.append("contains_unfinished_marker")

        ast_issues = _check_ast_for_dangerous(text)
        issues.extend(ast_issues)

        score = 1.0
        if ast_issues:
            score = 0.0
        elif issues:
            score = 0.3

        need_human_review = bool(issues)
        return EvalResult(score=score, need_human_review=need_human_review, issues=tuple(issues))

    @staticmethod
    def _looks_json(text: str) -> bool:
        text = text.strip()
        return text.startswith("{") or text.startswith("[")


class ASTGuardrail:
    @staticmethod
    def check(output_text: str) -> list[str]:
        return _check_ast_for_dangerous(output_text)


__all__ = ["ASTGuardrail", "EvalResult", "Evaluator", "RuleEvaluator"]
