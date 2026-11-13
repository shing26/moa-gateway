from eval.l0 import check_run, L0Report
from eval.schema import EvalCase
import textwrap


_PATCH_TESTS_PASS = textwrap.dedent('''
diff --git a/tests/test_example.py b/tests/test_example.py
new file mode 100644
--- /dev/null
+++ b/tests/test_example.py
@@ -0,0,1 @@
+def test_example():
+    assert True
''')

_PATCH_TESTS_FAIL = textwrap.dedent('''
diff --git a/tests/test_example.py b/tests/test_example.py
new file mode 100644
--- /dev/null
+++ b/tests/test_example.py
@@ -0,0,1 @@
+def test_example():
-    assert True
''')

_PATCH_TESTS_FAIL_SECOND_SECTION = textwrap.dedent('''
diff --git a/tests/test_example.py b/tests/test_example.py
new file mode 100644
--- /dev/null
+++ b/tests/test_example.py
@@ -0,0,1 @@
+def test_example():
-    assert False
diff --git a/main.py b/main.py
new file mode 100644
--- /dev/null
+++ b/main.py
@@ -0,0,1 @@
+print("hello")
''')

_PATCH_MAIN_PASS = textwrap.dedent('''
diff --git a/main.py b/main.py
new file mode 100644
--- /dev/null
+++ b/main.py
@@ -0,0,1 @@
+print("hello")
''')

_PATCH_UNLISTED_PATH = textwrap.dedent('''
diff --git a/unlisted.py b/unlisted.py
new file mode 100644
--- /dev/null
+++ b/unlisted.py
@@ -0,0,1 @@
+print("hello")
''')

_PATCH_FORBIDDEN = textwrap.dedent('''
diff --git a/tests/test_forbidden.py b/test_forbidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_forbidden.py
@@ -0,0,1 @@
+def test_forbidden():
+    pass
''')


def _case(**kw):
    base = {'id': 'x', 'class_': 'business', 'source': 'self', 'base_sha': '0',
            'goal': '', 'allowed_paths': (), 'oracle_type': 'script',
            'forbidden_paths': (), 'verify_commands': (), 'oracle_checks': ()}
    base.update(kw)
    return EvalCase(**base)


def _rec(bundle=None, verification=None, attempts=None):
    return {'bundle': bundle, 'verification': verification,
            'attempts': attempts}


def test_all_checks_pass():
    """All six checks pass on a record matching its case."""
    record = _rec(
        bundle={
            'patch': _PATCH_MAIN_PASS,
            'files': [
                {'path': 'main.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python', 'main.py'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}]
    )
    case = _case(allowed_paths=('main.py',), forbidden_paths=())
    report = check_run(record, case)
    assert report.all_passed is True
    for check in report.checks:
        assert check.passed is True
        assert check.detail


def test_changed_paths_match_fails_on_unlisted():
    """changed_paths_match fails on an unlisted path."""
    record = _rec(
        bundle={
            'patch': _PATCH_UNLISTED_PATH,
            'files': [
                {'path': 'unlisted.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python', 'main.py'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case(allowed_paths=('main.py',), forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert 'path not in allowed_paths' in check.detail


def test_changed_paths_match_null_bundle():
    """changed_paths_match fails without raising when bundle is null."""
    record = _rec(bundle=None, verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert 'bundle is null' in check.detail


def test_changed_paths_match_files_missing():
    """changed_paths_match fails when files is missing."""
    record = _rec(bundle={'patch': ''}, verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert 'files missing' in check.detail


def test_changed_paths_match_files_empty():
    """changed_paths_match fails when files is an empty list."""
    record = _rec(bundle={'patch': '', 'files': []}, verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'changed_paths_match')
    assert check.passed is False
    assert 'files is empty' in check.detail


def test_check_run_no_raise_none():
    """check_run does not raise for record None."""
    record = None
    case = _case()
    report = check_run(record, case)
    for check in report.checks:
        assert isinstance(check.passed, bool)


def test_check_run_no_raise_string_bundle():
    """check_run does not raise for bundle a plain string."""
    record = _rec(bundle='plain string', verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    for check in report.checks:
        assert isinstance(check.passed, bool)


def test_check_run_non_dict_step():
    """check_run does not raise for non-dict element inside steps list."""
    record = _rec(attempts=[{'steps': ['not a dict', {'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}])
    case = _case()
    report = check_run(record, case)
    read_ratio = next(c for c in report.checks if c.name == 'read_ratio')
    assert read_ratio.passed is True
    assert 'reads=1' in read_ratio.detail
    assert 'total=3' in read_ratio.detail


def test_verify_exit_zero_fails_exit_1():
    """verify_exit_zero fails on exit 1."""
    record = _rec(verification={'command': ['python', 'main.py'], 'exit_code': 1, 'output_hash': 'def', 'output_excerpt': ''})
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert 'exit_code is not 0' in check.detail


def test_verify_exit_zero_null():
    """verify_exit_zero fails when verification is null."""
    record = _rec(verification=None)
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert 'verification not a dict' in check.detail


def test_verify_exit_zero_key_absent():
    """verify_exit_zero fails when the verification KEY is absent."""
    rec = _rec(attempts=[])
    rec.pop('verification')
    record = rec
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_exit_zero')
    assert check.passed is False
    assert 'verification not a dict' in check.detail


def test_forbidden_paths_touched_fails():
    """forbidden_paths_touched fails when a forbidden path is present."""
    record = _rec(
        bundle={
            'patch': _PATCH_FORBIDDEN,
            'files': [
                {'path': 'tests/test_forbidden.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case(forbidden_paths=('tests/test_forbidden.py',))
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'forbidden_paths_touched')
    assert check.passed is False
    assert 'forbidden paths were touched' in check.detail


def test_forbidden_paths_touched_passes():
    """forbidden_paths_touched passes when none are declared."""
    record = _rec(
        bundle={
            'patch': _PATCH_MAIN_PASS,
            'files': [
                {'path': 'main.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case(forbidden_paths=())
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'forbidden_paths_touched')
    assert check.passed is True
    assert 'no forbidden paths are declared' in check.detail


def test_files_append_only_passes():
    """test_files_append_only passes when the tests/ section has only '+' and context lines."""
    record = _rec(
        bundle={
            'patch': _PATCH_TESTS_PASS,
            'files': [
                {'path': 'tests/test_example.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is True
    assert 'append-only' in check.detail


def test_files_append_only_fails_minus_line():
    """test_files_append_only fails when a '-assert' line appears in the section."""
    record = _rec(
        bundle={
            'patch': _PATCH_TESTS_FAIL,
            'files': [
                {'path': 'tests/test_example.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is False
    assert 'not append-only' in check.detail


def test_files_append_only_fail_second_section():
    """test_files_append_only fails with '-assert' as last line of non-final tests/ section."""
    record = _rec(
        bundle={
            'patch': _PATCH_TESTS_FAIL_SECOND_SECTION,
            'files': [
                {'path': 'tests/test_example.py', 'kind': 'added', 'before_hash': '', 'after_hash': 'abc', 'mode': '100644'}
            ]
        },
        verification={'command': ['python'], 'exit_code': 0, 'output_hash': 'def', 'output_excerpt': ''},
        attempts=[]
    )
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'test_files_append_only')
    assert check.passed is False
    assert 'not append-only' in check.detail


def test_read_ratio_passes():
    """read_ratio passes at reads=1,total=3."""
    record = _rec(attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}, {'tool': 'write_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is True
    assert 'reads=1' in check.detail
    assert 'total=3' in check.detail


def test_read_ratio_fails_half():
    """read_ratio fails at exactly half (reads=1,total=2)."""
    record = _rec(attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'write_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=1' in check.detail
    assert 'total=2' in check.detail


def test_read_ratio_fails_majority():
    """read_ratio fails when reads are the majority."""
    record = _rec(attempts=[{'steps': [{'tool': 'read_file'}, {'tool': 'read_file'}, {'tool': 'write_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=2' in check.detail
    assert 'total=3' in check.detail


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
    assert 'reads=0' in check.detail
    assert 'total=0' in check.detail


def test_read_ratio_fails_read_at_end():
    """read_ratio fails when reads are at the end of steps."""
    record = _rec(attempts=[{'steps': [{'tool': ''}, {'tool': ''}, {'tool': 'read_file'}]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'read_ratio')
    assert check.passed is False
    assert 'reads=1' in check.detail
    assert 'total=1' in check.detail


def test_verify_cycles_passes_five():
    """verify_cycles PASSES at exactly 5 total calls."""
    record = _rec(attempts=[{'steps': [
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
    ]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is True
    assert 'run_tests_calls=3' in check.detail
    assert 'run_unittest_calls=2' in check.detail


def test_verify_cycles_fails_six():
    """verify_cycles FAILS at exactly 6 total calls."""
    record = _rec(attempts=[{'steps': [
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
    ]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is False
    assert 'run_tests_calls=4' in check.detail
    assert 'run_unittest_calls=2' in check.detail


def test_verify_cycles_fails_both_at_five():
    """verify_cycles FAILS with 5 run_tests steps AND 5 run_unittest steps."""
    record = _rec(attempts=[{'steps': [
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_tests'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
        {'action': 'call_tool', 'tool': 'run_unittest'},
    ]}])
    case = _case()
    report = check_run(record, case)
    check = next(c for c in report.checks if c.name == 'verify_cycles')
    assert check.passed is False
    assert 'run_tests_calls=5' in check.detail
    assert 'run_unittest_calls=5' in check.detail


def test_realistic_null_bundle_attempt_empty():
    """A realistic record with null bundle and attempts=[] is graded without raising."""
    record = _rec(bundle=None, verification=None, attempts=[])
    case = _case()
    report = check_run(record, case)
    assert isinstance(report, L0Report)
    assert report.all_passed is False
