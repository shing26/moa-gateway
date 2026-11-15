"""验证后门禁（ADR-030）：强制 lint + 测试强度。

仓库 ruff 配置忽略了 F401/F841/RUF100，`ruff check .` 对死代码永远绿灯；
这里用 `--isolated` 强制启用这三个码。门禁只判定、不修复、不让管线崩溃：
任何 skip 必须带机器可读的 reason 落进 audit，不允许静默 fail-open。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_LINT_CODES = ("F401", "F841", "RUF100")
# pytest.raises / unittest 的 assertRaises 都算有效断言意图
_RAISES_ATTRS = frozenset({"raises", "assertRaises"})


def is_test_file(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return "tests/" in normalized or Path(normalized).name.startswith("test_")


@dataclass(frozen=True)
class GateViolation:
    path: str
    detail: str
    line: int = 0
    column: int = 0


@dataclass(frozen=True)
class GateReport:
    name: str
    status: Literal["passed", "failed", "skipped"]
    violations: tuple[GateViolation, ...] = ()
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "violations": [
                {
                    "path": item.path,
                    "detail": item.detail,
                    "line": item.line,
                    "column": item.column,
                }
                for item in self.violations
            ],
            "skipped_reason": self.skipped_reason,
        }


def _normalize(path_str: str) -> str:
    return path_str.replace("\\", "/")


def _resolve_key(filename: str, *, cwd: Path) -> str:
    """ruff 的 JSON filename 可能是相对或绝对路径；统一解析成绝对 posix 键。"""
    candidate = Path(filename)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve().as_posix()
    except OSError:
        return candidate.as_posix()


def _run_ruff(
    paths: Sequence[str | Path],
    *,
    cwd: Path,
) -> tuple[int, list[dict[str, Any]] | None, str]:
    """返回 (exit code, json 行, stderr)。exit code 不是 0/1 或 JSON 不可解析时 rows 为 None。"""
    argv = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--isolated",
        "--select",
        ",".join(_LINT_CODES),
        "--output-format",
        "json",
        *[str(item) for item in paths],
    ]
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    rows: list[dict[str, Any]] | None = None
    if proc.returncode in (0, 1):
        try:
            parsed = json.loads(proc.stdout) if proc.stdout.strip() else []
            rows = parsed if isinstance(parsed, list) else None
        except json.JSONDecodeError:
            rows = None
    return proc.returncode, rows, proc.stderr


def _row_code(row: dict[str, Any]) -> str:
    # 语法错误行没有 rule code；给个稳定键，让它也参与基线对比
    return str(row.get("code") or "syntax-error")


def _violation_from_row(row: dict[str, Any], path: str) -> GateViolation:
    location = row.get("location") or {}
    return GateViolation(
        path=path,
        detail=f"{_row_code(row)}: {row.get('message', '')}",
        line=int(location.get("row") or 0),
        column=int(location.get("column") or 0),
    )


def _git_show_text(workspace: Path, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def forced_lint_gate(
    workspace: Path,
    added: Sequence[str],
    modified: Sequence[str],
) -> GateReport:
    """强制 lint：新增文件任何 finding 都算违规；修改文件以 HEAD 为基线，
    按 (文件, code) 的 finding 数量对比——只拦新增，不追旧账（旧行会因编辑漂移）。"""
    if importlib.util.find_spec("ruff") is None:
        return GateReport(
            name="forced_lint",
            status="skipped",
            skipped_reason="ruff is not available",
        )
    workspace = Path(workspace)
    files = [*added, *modified]
    if not files:
        return GateReport(
            name="forced_lint",
            status="skipped",
            skipped_reason="no python files changed",
        )

    exit_code, rows, stderr = _run_ruff(files, cwd=workspace)
    if rows is None:
        return GateReport(
            name="forced_lint",
            status="skipped",
            skipped_reason=f"ruff exit code {exit_code}: {stderr.strip()[:200]}",
        )

    added_keys = {
        _resolve_key(item, cwd=workspace): _normalize(item) for item in added
    }
    modified_keys = {
        _resolve_key(item, cwd=workspace): _normalize(item) for item in modified
    }
    violations: list[GateViolation] = []
    work_counts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        file = added_keys.get(
            _resolve_key(str(row.get("filename", "")), cwd=workspace)
        )
        if file is not None:
            violations.append(_violation_from_row(row, file))
        else:
            file = modified_keys.get(
                _resolve_key(str(row.get("filename", "")), cwd=workspace)
            )
            if file is not None:
                work_counts.setdefault((file, _row_code(row)), []).append(row)

    base_counts: dict[tuple[str, str], int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        aliases: dict[str, str] = {}
        alias_names: list[str] = []
        for index, path in enumerate(modified):
            head_text = _git_show_text(workspace, path)
            if head_text is None:
                # modified 文件必然在 HEAD 里；取不到基线视为基线为 0（fail-closed）
                continue
            alias = f"base_{index}_{Path(path).name}"
            (tmp_dir / alias).write_text(head_text, encoding="utf-8")
            alias_names.append(alias)
            aliases[_resolve_key(alias, cwd=tmp_dir)] = _normalize(path)
        if alias_names:
            base_code, base_rows, base_stderr = _run_ruff(alias_names, cwd=tmp_dir)
            if base_rows is None:
                return GateReport(
                    name="forced_lint",
                    status="skipped",
                    skipped_reason=(
                        f"ruff exit code {base_code} on HEAD baseline: "
                        f"{base_stderr.strip()[:200]}"
                    ),
                )
            for row in base_rows:
                key = _resolve_key(str(row.get("filename", "")), cwd=tmp_dir)
                original = aliases.get(key, "")
                cell = (original, _row_code(row))
                base_counts[cell] = base_counts.get(cell, 0) + 1

    for (file, code), items in sorted(work_counts.items()):
        baseline = base_counts.get((file, code), 0)
        if len(items) > baseline:
            # 数量多于 HEAD 基线才算新增；少或相等放过，容忍"挪动"旧问题
            extra = len(items) - baseline
            violation = _violation_from_row(items[0], file)
            violations.append(
                GateViolation(
                    path=file,
                    detail=(
                        f"{violation.detail} "
                        f"(+{extra} vs HEAD baseline {baseline})"
                    ),
                    line=violation.line,
                    column=violation.column,
                )
            )

    return GateReport(
        name="forced_lint",
        status="failed" if violations else "passed",
        violations=tuple(violations),
    )


def _has_assertion_intent(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _RAISES_ATTRS
        ):
            return True
    return False


def _vacuous_tests(path: str, tree: ast.AST) -> list[GateViolation]:
    """收集无 assert 且无 raises 检查的 test 函数；只看模块级或类内的定义。"""
    missing: list[GateViolation] = []

    def visit(node: ast.AST, prefix: str, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                if (
                    child.name.startswith("test_")
                    and not in_function
                    and not _has_assertion_intent(child)
                ):
                    missing.append(
                        GateViolation(
                            path=path,
                            detail=qualname,
                            line=child.lineno,
                        )
                    )
                visit(child, f"{qualname}.", True)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.", False)
            else:
                visit(child, prefix, in_function)

    visit(tree, "", False)
    return missing


def test_strength_gate(
    added: Sequence[str],
    modified: Sequence[str],
    workspace: Path,
) -> GateReport:
    """测试强度：每个 test 函数至少要有一个 assert 或 raises 式检查，空转测试不出口。"""
    candidates = [
        item for item in [*added, *modified] if is_test_file(item)
    ]
    if not candidates:
        return GateReport(
            name="test_strength",
            status="skipped",
            skipped_reason="no test files changed",
        )
    workspace = Path(workspace)
    violations: list[GateViolation] = []
    for rel_path in candidates:
        target = workspace / rel_path
        try:
            tree = ast.parse(
                target.read_text(encoding="utf-8"),
                filename=rel_path,
            )
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            violations.append(
                GateViolation(path=rel_path, detail=f"failed to parse: {exc}")
            )
            continue
        violations.extend(_vacuous_tests(rel_path, tree))
    return GateReport(
        name="test_strength",
        status="failed" if violations else "passed",
        violations=tuple(violations),
    )
