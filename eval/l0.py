from dataclasses import dataclass

from eval.schema import EvalCase


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
            checks.append(L0Check('changed_paths_match', False, 'bundle is null'))
        else:
            files = bundle.get('files')
            if not isinstance(files, list):
                checks.append(L0Check('changed_paths_match', False, 'files missing'))
            elif len(files) == 0:
                checks.append(L0Check('changed_paths_match', False, 'files is empty'))
            else:
                paths = []
                for entry in files:
                    if isinstance(entry, dict):
                        path = entry.get('path')
                        if isinstance(path, str):
                            paths.append(path)
                if all(p in case.allowed_paths for p in paths):
                    checks.append(L0Check('changed_paths_match', True, ''))
                else:
                    checks.append(L0Check('changed_paths_match', False, ''))
    except Exception:
        checks.append(L0Check('changed_paths_match', False, ''))

    # Check 2: verify_exit_zero
    try:
        verification = record.get('verification')
        if not isinstance(verification, dict):
            checks.append(L0Check('verify_exit_zero', False, ''))
        else:
            exit_code = verification.get('exit_code')
            if exit_code == 0:
                checks.append(L0Check('verify_exit_zero', True, ''))
            else:
                checks.append(L0Check('verify_exit_zero', False, ''))
    except Exception:
        checks.append(L0Check('verify_exit_zero', False, ''))

    # Check 3: forbidden_paths_touched
    try:
        bundle = record.get('bundle')
        if not isinstance(bundle, dict):
            changed_paths = []
        else:
            files = bundle.get('files')
            if not isinstance(files, list):
                changed_paths = []
            else:
                changed_paths = []
                for entry in files:
                    if isinstance(entry, dict):
                        path = entry.get('path')
                        if isinstance(path, str):
                            changed_paths.append(path)
        if len(case.forbidden_paths) == 0:
            checks.append(L0Check('forbidden_paths_touched', True, 'no forbidden paths are declared'))
        elif any(p in case.forbidden_paths for p in changed_paths):
            checks.append(L0Check('forbidden_paths_touched', False, ''))
        else:
            checks.append(L0Check('forbidden_paths_touched', True, ''))
    except Exception:
        checks.append(L0Check('forbidden_paths_touched', False, ''))

    # Check 4: test_files_append_only
    try:
        bundle = record.get('bundle')
        if not isinstance(bundle, dict):
            files = None
        else:
            files = bundle.get('files')
        patch = bundle.get('patch') if isinstance(bundle, dict) else None

        if not isinstance(files, list) or len(files) == 0:
            checks.append(L0Check('test_files_append_only', True, 'no tests/ path changed'))
        else:
            test_paths = []
            for entry in files:
                if isinstance(entry, dict):
                    path = entry.get('path')
                    if isinstance(path, str) and path.startswith('tests/'):
                        test_paths.append(path)

            if len(test_paths) == 0:
                checks.append(L0Check('test_files_append_only', True, 'no tests/ path changed'))
            elif not isinstance(patch, str):
                checks.append(L0Check('test_files_append_only', False, ''))
            else:
                has_violation = False
                for path in test_paths:
                    section = _patch_section(patch, path)
                    for line in section:
                        if line.startswith('-') and not line.startswith('---'):
                            has_violation = True
                            break
                    if has_violation:
                        break
                if has_violation:
                    checks.append(L0Check('test_files_append_only', False, ''))
                else:
                    checks.append(L0Check('test_files_append_only', True, ''))
    except Exception:
        checks.append(L0Check('test_files_append_only', False, ''))

    # Check 5: read_ratio
    try:
        attempts = record.get('attempts')
        if not isinstance(attempts, list):
            checks.append(L0Check('read_ratio', False, 'reads=0, total=0'))
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
                    total += 1
                    if isinstance(tool, str) and tool:
                        if tool in ('read_file', 'search'):
                            reads += 1
            if 2 * reads < total:
                checks.append(L0Check('read_ratio', True, f'reads={reads}, total={total}'))
            else:
                checks.append(L0Check('read_ratio', False, f'reads={reads}, total={total}'))
    except Exception:
        checks.append(L0Check('read_ratio', False, 'reads=0, total=0'))

    # Check 6: verify_cycles
    try:
        attempts = record.get('attempts')
        if not isinstance(attempts, list):
            checks.append(L0Check('verify_cycles', False, 'run_tests_calls=0, run_unittest_calls=0'))
        else:
            run_tests_calls = 0
            run_unittest_calls = 0
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
                            run_tests_calls += 1
                        elif tool == 'run_unittest':
                            run_unittest_calls += 1
            if run_tests_calls + run_unittest_calls <= 5:
                checks.append(L0Check('verify_cycles', True, f'run_tests_calls={run_tests_calls}, run_unittest_calls={run_unittest_calls}'))
            else:
                checks.append(L0Check('verify_cycles', False, f'run_tests_calls={run_tests_calls}, run_unittest_calls={run_unittest_calls}'))
    except Exception:
        checks.append(L0Check('verify_cycles', False, 'run_tests_calls=0, run_unittest_calls=0'))

    return L0Report(checks=tuple(checks))
