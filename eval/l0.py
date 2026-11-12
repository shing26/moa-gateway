from eval.schema import EvalCase
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class L0Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class L0Report:
    checks: tuple[L0Check, ...]

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)


def _patch_section(patch: str, file_path: str) -> list[str]:
    lines = patch.split(chr(10))
    i = None
    for idx, line in enumerate(lines):
        if line.startswith('diff --git ') and file_path in line:
            i = idx
            break
    if i is None:
        return []
    body = []
    j = i + 1
    while j < len(lines) and not lines[j].startswith('diff --git '):
        body.append(lines[j])
        j += 1
    return body


def check_run(record: dict, case: EvalCase) -> L0Report:
    checks: list[L0Check] = []

    # Check 1: changed_paths_match
    try:
        bundle = record.get('bundle')
        if not isinstance(bundle, dict):
            checks.append(L0Check(
                name='changed_paths_match',
                passed=False,
                detail='bundle is null'
            ))
        else:
            files = bundle.get('files')
            if not isinstance(files, list):
                checks.append(L0Check(
                    name='changed_paths_match',
                    passed=False,
                    detail='files missing'
                ))
            elif len(files) == 0:
                checks.append(L0Check(
                    name='changed_paths_match',
                    passed=False,
                    detail='files is empty'
                ))
            else:
                allowed = set(case.allowed_paths)
                all_ok = True
                for entry in files:
                    if not isinstance(entry, dict):
                        all_ok = False
                        break
                    path = entry.get('path')
                    if path is None or path not in allowed:
                        all_ok = False
                        break
                if all_ok:
                    checks.append(L0Check(
                        name='changed_paths_match',
                        passed=True,
                        detail='all changed paths are in allowed_paths'
                    ))
                else:
                    checks.append(L0Check(
                        name='changed_paths_match',
                        passed=False,
                        detail='path not in allowed_paths'
                    ))
    except Exception:
        checks.append(L0Check(
            name='changed_paths_match',
            passed=False,
            detail='unexpected error'
        ))

    # Check 2: verify_exit_zero
    try:
        verification = record.get('verification')
        if not isinstance(verification, dict):
            checks.append(L0Check(
                name='verify_exit_zero',
                passed=False,
                detail='verification not a dict'
            ))
        else:
            exit_code = verification.get('exit_code')
            if exit_code != 0:
                checks.append(L0Check(
                    name='verify_exit_zero',
                    passed=False,
                    detail='exit_code is not 0'
                ))
            else:
                checks.append(L0Check(
                    name='verify_exit_zero',
                    passed=True,
                    detail='exit_code is 0'
                ))
    except Exception:
        checks.append(L0Check(
            name='verify_exit_zero',
            passed=False,
            detail='unexpected error'
        ))

    # Check 3: forbidden_paths_touched
    try:
        bundle = record.get('bundle')
        forbidden = set(case.forbidden_paths)
        if not forbidden:
            checks.append(L0Check(
                name='forbidden_paths_touched',
                passed=True,
                detail='no forbidden paths are declared'
            ))
        elif not isinstance(bundle, dict):
            checks.append(L0Check(
                name='forbidden_paths_touched',
                passed=False,
                detail='bundle is null'
            ))
        else:
            files = bundle.get('files')
            if not isinstance(files, list):
                checks.append(L0Check(
                    name='forbidden_paths_touched',
                    passed=False,
                    detail='files missing'
                ))
            else:
                forbidden_found = False
                for entry in files:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get('path')
                    if path in forbidden:
                        forbidden_found = True
                        break
                if forbidden_found:
                    checks.append(L0Check(
                        name='forbidden_paths_touched',
                        passed=False,
                        detail='forbidden paths were touched'
                    ))
                else:
                    checks.append(L0Check(
                        name='forbidden_paths_touched',
                        passed=True,
                        detail='no forbidden paths were touched'
                    ))
    except Exception:
        checks.append(L0Check(
            name='forbidden_paths_touched',
            passed=False,
            detail='unexpected error'
        ))

    # Check 4: test_files_append_only
    try:
        bundle = record.get('bundle')
        if not isinstance(bundle, dict):
            checks.append(L0Check(
                name='test_files_append_only',
                passed=False,
                detail='bundle is null'
            ))
        else:
            files = bundle.get('files')
            if not isinstance(files, list):
                checks.append(L0Check(
                    name='test_files_append_only',
                    passed=False,
                    detail='files missing'
                ))
            else:
                test_paths_changed = []
                for entry in files:
                    if not isinstance(entry, dict):
                        continue
                    path = entry.get('path')
                    if isinstance(path, str) and path.startswith('tests/'):
                        test_paths_changed.append(path)

                if not test_paths_changed:
                    checks.append(L0Check(
                        name='test_files_append_only',
                        passed=True,
                        detail='no tests/ path changed'
                    ))
                else:
                    patch = bundle.get('patch')
                    if not isinstance(patch, str):
                        checks.append(L0Check(
                            name='test_files_append_only',
                            passed=False,
                            detail='patch missing'
                        ))
                    else:
                        all_append_only = True
                        for test_path in test_paths_changed:
                            lines = _patch_section(patch, test_path)
                            for line in lines:
                                if line.startswith('-') and not line.startswith('---'):
                                    all_append_only = False
                                    break
                            if not all_append_only:
                                break

                        if all_append_only:
                            checks.append(L0Check(
                                name='test_files_append_only',
                                passed=True,
                                detail='all test changes are append-only'
                            ))
                        else:
                            checks.append(L0Check(
                                name='test_files_append_only',
                                passed=False,
                                detail='test file changes not append-only'
                            ))
    except Exception:
        checks.append(L0Check(
            name='test_files_append_only',
            passed=False,
            detail='unexpected error'
        ))

    # Check 5: read_ratio
    try:
        attempts = record.get('attempts')
        if not isinstance(attempts, list):
            checks.append(L0Check(
                name='read_ratio',
                passed=False,
                detail='attempts not a list'
            ))
        else:
            total = 0
            reads = 0
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                steps = attempt.get('steps')
                if not isinstance(steps, list):
                    continue
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    tool = step.get('tool')
                    if isinstance(tool, str) and tool:
                        total += 1
                        if tool in ('read_file', 'search'):
                            reads += 1

            if total == 0:
                checks.append(L0Check(
                    name='read_ratio',
                    passed=False,
                    detail='reads=0, total=0'
                ))
            elif 2 * reads < total:
                checks.append(L0Check(
                    name='read_ratio',
                    passed=True,
                    detail=f'reads={reads}, total={total}'
                ))
            else:
                checks.append(L0Check(
                    name='read_ratio',
                    passed=False,
                    detail=f'reads={reads}, total={total}'
                ))
    except Exception:
        checks.append(L0Check(
            name='read_ratio',
            passed=False,
            detail='unexpected error'
        ))

    # Check 6: verify_cycles
    try:
        attempts = record.get('attempts')
        if not isinstance(attempts, list):
            checks.append(L0Check(
                name='verify_cycles',
                passed=False,
                detail='attempts not a list'
            ))
        else:
            n = 0
            m = 0
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    continue
                steps = attempt.get('steps')
                if not isinstance(steps, list):
                    continue
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    tool = step.get('tool')
                    if isinstance(tool, str):
                        if tool == 'run_tests':
                            n += 1
                        elif tool == 'run_unittest':
                            m += 1

            if n + m <= 5:
                checks.append(L0Check(
                    name='verify_cycles',
                    passed=True,
                    detail=f'run_tests_calls={n}, run_unittest_calls={m}'
                ))
            else:
                checks.append(L0Check(
                    name='verify_cycles',
                    passed=False,
                    detail=f'run_tests_calls={n}, run_unittest_calls={m}'
                ))
    except Exception:
        checks.append(L0Check(
            name='verify_cycles',
            passed=False,
            detail='unexpected error'
        ))

    return L0Report(checks=tuple(checks))
