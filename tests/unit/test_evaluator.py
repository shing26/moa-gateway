import pytest

from app.evaluator.evaluator import ASTGuardrail, RuleEvaluator, EvalResult


@pytest.mark.asyncio
async def test_evaluator_valid_output():
    evaluator = RuleEvaluator()
    result = await evaluator.score("All checks passed.", intent="coding")
    assert not result.need_human_review
    assert result.score == 1.0


@pytest.mark.asyncio
async def test_evaluator_invalid_json_flags_review():
    evaluator = RuleEvaluator()
    result = await evaluator.score("{broken json", intent="coding")
    assert result.need_human_review
    assert "invalid_json_payload" in result.issues


@pytest.mark.asyncio
async def test_evaluator_empty_output():
    evaluator = RuleEvaluator()
    result = await evaluator.score("", intent="general")
    assert result.score == 0.0
    assert "empty_output" in result.issues


@pytest.mark.asyncio
async def test_evaluator_too_long():
    evaluator = RuleEvaluator()
    long_text = "x" * 21000
    result = await evaluator.score(long_text, intent="general")
    assert result.score == 0.3  # minor issue
    assert "output_too_long" in result.issues


@pytest.mark.asyncio
async def test_evaluator_todo_marker():
    evaluator = RuleEvaluator()
    result = await evaluator.score("Some code with TODO item", intent="coding")
    assert result.need_human_review
    assert "contains_unfinished_marker" in result.issues


# ---- AST guardrail tests ----

def test_ast_detect_dangerous_call_exec():
    issues = ASTGuardrail.check("exec('print(1)')")
    assert "dangerous_call:exec" in issues


def test_ast_detect_dangerous_call_eval():
    issues = ASTGuardrail.check("eval('1+1')")
    assert "dangerous_call:eval" in issues


def test_ast_detect_dangerous_call_compile():
    issues = ASTGuardrail.check("compile('x=1', '', 'exec')")
    assert "dangerous_call:compile" in issues


def test_ast_detect_dangerous_import_os():
    issues = ASTGuardrail.check("import os")
    assert "dangerous_import:os" in issues


def test_ast_detect_dangerous_import_subprocess():
    issues = ASTGuardrail.check("import subprocess")
    assert "dangerous_import:subprocess" in issues


def test_ast_detect_dangerous_import_from_os():
    issues = ASTGuardrail.check("from os import path, system")
    assert "dangerous_import_from:os" in issues


def test_ast_detect_dangerous_method_system():
    issues = ASTGuardrail.check("os.system('ls')")
    assert "dangerous_method:system" in issues or "dangerous_call:os.system" in issues


def test_ast_detect_dangerous_method_popen():
    issues = ASTGuardrail.check("subprocess.Popen(['ls'])")
    assert any('dangerous_method' in i for i in issues) or any('dangerous_call' in i for i in issues)


def test_ast_detect_write_mode_open():
    issues = ASTGuardrail.check("open('/tmp/x', 'w')")
    assert "write_mode_open:w" in issues


def test_ast_detect_write_mode_open_append():
    issues = ASTGuardrail.check("open('/tmp/x', 'a')")
    assert "write_mode_open:a" in issues


def test_ast_clean_code_passes():
    issues = ASTGuardrail.check("def hello():\n    return 'world'\n")
    assert issues == []


def test_ast_natural_text_skipped():
    issues = ASTGuardrail.check("The os module is used for operating system interfaces.")
    assert issues == []  # Not valid Python -> skipped


def test_ast_multiple_issues():
    issues = ASTGuardrail.check("import os; os.system('ls'); exec('x')")
    assert len(issues) >= 2


@pytest.mark.asyncio
async def test_evaluator_ast_integration_dangerous():
    evaluator = RuleEvaluator()
    result = await evaluator.score("import subprocess\nsubprocess.run(['rm', '-rf', '/'])", intent="coding")
    assert result.score == 0.0  # dangerous -> score 0.0
    assert result.need_human_review
    assert any("dangerous" in i for i in result.issues)


@pytest.mark.asyncio
async def test_evaluator_ast_integration_clean():
    evaluator = RuleEvaluator()
    result = await evaluator.score("def add(a, b): return a + b", intent="coding")
    assert result.score == 1.0
    assert not result.need_human_review
