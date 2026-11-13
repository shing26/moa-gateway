import textwrap

from eval.l0 import check_run, L0Report
from eval.schema import EvalCase


def _case(**kw):
    base = {'id': 'x', 'class_': 'business', 'source': 'self', 'base_sha': '0',
            'goal': '', 'allowed_paths': (), 'oracle_type': 'script',
            'forbidden_paths': (), 'verify_commands': (), 'oracle_checks': ()}
    base.update(kw)
    return EvalCase(**base)


def _rec(bundle=None, verification=None, attempts=None):
    return {'bundle': bundle, 'verification': verification,
            'attempts': attempts}


_ALL_PASS_PATCH = textwrap.dedent('''
    diff --git a/foo.py b/foo.py
    index 1234567..abcdefg 100644
    --- a/foo.py
    +++ b/foo.py
    @@ -1,3 +1,4 @@
     # comment
    +new line
    context
''').lstrip()

_ALL_PASS_PATCH_TESTS = textwrap.dedent('''
    diff --git a/tests/test_foo.py b/tests/test_foo.py
    index 1234567..abcdefg 100644
    --- a/tests/test_foo.py
    +++ b/tests/test_foo.py
    @@ -1,3 +1,4 @@
     # comment
    +new test line
    context
''').lstrip()


def test_all_checks_pass():
    """All six checks pass on a record matching its case."""
    record = _rec(
        bundle={
            'files': [
                {'path': 'foo.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}
            ],
            'patch': _ALL_PASS_PATCH,
        },
        verification={'command': ['python -m pytest'], 'exit_code': 0, 'output_hash': 'hash', 'output_excerpt': 'ok'},
        attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}],
    )
    case = _case(allowed_paths=('foo.py',), forbidden_paths=())
    report = check_run(record, case)
    assert report.all_passed is True
    for check in report.checks:
        assert check.passed is True
    # read_ratio must have detail with numbers
    read_check = next(c for c in report.checks if c.name == 'read_ratio')
    assert 'reads=1' in read_check.detail and 'total=3' in read_check.detail


def test_changed_paths_match_fails_on_unlisted():
    """changed_paths_match fails on an unlisted path."""
    record = _rec(
        bundle={'files': [{'path': 'bad.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}]},
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[{'steps': []}],
    )
    case = _case(allowed_paths=('allowed.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert 'path not in allowed_paths' in check.detail


def test_changed_paths_match_fails_bundle_null():
    """changed_paths_match fails when bundle is null."""
    record = _rec(bundle=None, verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''}, attempts=[])
    case = _case(allowed_paths=('foo.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert check.detail == 'bundle is null'


def test_changed_paths_match_fails_files_missing():
    """changed_paths_match fails when files is missing."""
    record = _rec(bundle={'patch': ''}, verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''}, attempts=[])
    case = _case(allowed_paths=('foo.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert check.detail == 'files missing'


def test_changed_paths_match_fails_files_empty():
    """changed_paths_match fails when files is an empty list."""
    record = _rec(bundle={'files': [], 'patch': ''}, verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''}, attempts=[])
    case = _case(allowed_paths=('foo.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert check.detail == 'files is empty'


def test_check_run_no_raise_record_none():
    """check_run does not raise for record None."""
    case = _case()
    report = check_run(None, case)
    assert isinstance(report, L0Report)


def test_check_run_no_raise_bundle_string():
    """check_run does not raise for bundle a plain string."""
    record = _rec(bundle='not a dict', verification={'exit_code': 0}, attempts=[])
    case = _case()
    report = check_run(record, case)
    assert isinstance(report, L0Report)


def test_check_run_no_raise_steps_non_dict():
    """check_run does not raise for non-dict element inside steps list."""
    record = _rec(
        attempts=[{'steps': ['not a dict', {'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}],
    )
    case = _case()
    report = check_run(record, case)
    assert isinstance(report, L0Report)
    # read_ratio must have reads=1, total=3 in detail
    read_check = next(c for c in report.checks if c.name == 'read_ratio')
    assert 'reads=1' in read_check.detail and 'total=3' in read_check.detail


def test_verify_exit_zero_fails_exit_1():
    """verify_exit_zero fails on exit 1."""
    record = _rec(verification={'command': [], 'exit_code': 1, 'output_hash': '', 'output_excerpt': ''}, attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert 'exit_code is not 0' in check.detail


def test_verify_exit_zero_fails_null():
    """verify_exit_zero fails when verification is null."""
    record = _rec(verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert check.detail == 'verification not a dict'


def test_verify_exit_zero_fails_key_absent():
    """verify_exit_zero fails when verification KEY is absent."""
    rec = _rec(attempts=[])
    rec.pop('verification')
    case = _case()
    report = check_run(rec, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert check.detail == 'verification not a dict'


def test_forbidden_paths_touched_fails():
    """forbidden_paths_touched fails when a forbidden path is present."""
    record = _rec(
        bundle={'files': [{'path': 'bad.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}]},
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[],
    )
    case = _case(allowed_paths=(), forbidden_paths=('bad.py',))
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'forbidden_paths_touched')
    assert check.passed is False
    assert 'forbidden paths were touched' in check.detail


def test_forbidden_paths_touched_no_forbidden():
    """forbidden_paths_touched passes when none are declared."""
    record = _rec(
        bundle={'files': [{'path': 'good.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}]},
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[],
    )
    case = _case(allowed_paths=('good.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'forbidden_paths_touched')
    assert check.passed is True
    assert 'no forbidden paths are declared' in check.detail


def test_test_files_append_only_passes():
    """test_files_append_only passes when tests/ section has only '+' and context lines."""
    record = _rec(
        bundle={
            'files': [{'path': 'tests/test_foo.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}],
            'patch': _ALL_PASS_PATCH_TESTS,
        },
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[],
    )
    case = _case(allowed_paths=('tests/test_foo.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is True
    assert 'append-only' in check.detail


def test_test_files_append_only_fails_assert():
    """test_files_append_only fails when a '-assert' line appears in the section."""
    patch = textwrap.dedent('''
        diff --git a/tests/test_foo.py b/tests/test_foo.py
        index 1234567..abcdefg 100644
        --- a/tests/test_foo.py
        +++ b/tests/test_foo.py
        @@ -1,3 +1,4 @@
         # comment
        -assert False
        +new test line
    ''').lstrip()
    record = _rec(
        bundle={'files': [{'path': 'tests/test_foo.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}], 'patch': patch},
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[],
    )
    case = _case(allowed_paths=('tests/test_foo.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is False
    assert 'not append-only' in check.detail


def test_test_files_append_only_fails_assert_last_line():
    """test_files_append_only fails when '-assert' is the last line of a non-final tests/ section."""
    patch = 'diff --git a/tests/test_foo.py b/tests/test_foo.py\n-assert False\ndiff --git a/main.py b/main.py\n@@ -0,0 +1 @@\n+content'
    record = _rec(
        bundle={'files': [{'path': 'tests/test_foo.py', 'kind': 'modified', 'before_hash': 'abc', 'after_hash': 'def', 'mode': '100644'}, {'path': 'main.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'ghi', 'mode': '100644'}], 'patch': patch},
        verification={'exit_code': 0, 'output_hash': '', 'output_excerpt': ''},
        attempts=[],
    )
    case = _case(allowed_paths=('tests/test_foo.py', 'main.py'), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is False


def test_read_ratio_passes():
    """read_ratio passes at reads=1,total=3."""
    record = _rec(
        attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}],
    )
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is True
    assert 'reads=1' in check.detail and 'total=3' in check.detail


def test_read_ratio_fails_half():
    """read_ratio fails at exactly half (reads=1,total=2)."""
    record = _rec(attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=1, total=2' in check.detail


def test_read_ratio_fails_majority():
    """read_ratio fails when reads are the majority."""
    record = _rec(attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'read_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=2, total=2' in check.detail


def test_read_ratio_fails_zero_steps():
    """read_ratio fails on zero steps."""
    record = _rec(attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=0, total=0' in check.detail


def test_read_ratio_fails_empty_tools():
    """read_ratio fails with empty tools."""
    record = _rec(attempts=[{'steps': [{'tool': ''}, {'tool': ''}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=0, total=0' in check.detail


def test_read_ratio_fails_empty_tools_plus_read():
    """read_ratio fails on [{'tool': ''}, {'tool': ''}, {'tool': 'read_file'}]."""
    record = _rec(attempts=[{'steps': [{'tool': ''}, {'tool': ''}, {'tool': 'read_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=1, total=1' in check.detail


def test_verify_cycles_passes_five():
    """verify_cycles PASSES at exactly 5 total calls."""
    record = _rec(attempts=[{'steps': [{'action': 'call_tool', 'tool': 'run_tests'}, {'action': 'call_tool', 'tool': 'run_unittest'}] * 2 + [{'action': 'call_tool', 'tool': 'run_tests'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is True
    assert 'run_tests_calls=3' in check.detail and 'run_unittest_calls=2' in check.detail


def test_verify_cycles_fails_six():
    """verify_cycles FAILS at exactly 6 total calls."""
    record = _rec(attempts=[{'steps': [{'action': 'call_tool', 'tool': 'run_tests'}] * 6}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is False
    assert 'run_tests_calls=6' in check.detail


def test_verify_cycles_fails_sum_exceeded():
    """verify_cycles FAILS with 5 run_tests steps AND 5 run_unittest steps (sum=10)."""
    record = _rec(attempts=[{'steps': [{'action': 'call_tool', 'tool': 'run_tests'}] * 5 + [{'action': 'call_tool', 'tool': 'run_unittest'}] * 5}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is False
    assert 'run_tests_calls=5' in check.detail and 'run_unittest_calls=5' in check.detail


def test_realistic_null_bundle():
    """A realistic record with a null bundle is graded without raising and does not report all_passed."""
    record = _rec(
        bundle=None,
        verification={'exit_code': 0, 'output_hash': 'hash', 'output_excerpt': 'ok', 'command': []},
        attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'search'}, {'tool': 'read_file'}, {'tool': 'read_file'}, {'tool': 'search'}]}],
    )
    case = _case(id='realistic', allowed_paths=('foo.py',), forbidden_paths=())
    report = check_run(record, case)
    assert isinstance(report, L0Report)
    assert report.all_passed is False
