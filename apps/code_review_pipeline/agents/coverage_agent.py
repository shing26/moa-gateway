"""代码审查流水线里的“测试覆盖率”分析 agent。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.agents.contract import AgentEnvelope
from app.agents.provider import LLMClient
from app.agents.workspace_tools import build_toolset, call_workspace_tool
from app.config import settings
from apps.code_review_pipeline.routing.llm_factory import build_code_review_llm

logger = logging.getLogger("moa.code_review.agents")

SYSTEM_PROMPT = """你是一个测试覆盖率分析助手。请分析以下代码 diff，输出严格的 JSON：

{
  "findings": [
    {
      "id": "test-001",
      "severity": "medium|low",
      "category": "test_coverage|test_quality|missing_scenario",
      "file": "相对路径",
      "line": 行号,
      "title": "问题标题",
      "description": "问题描述",
      "suggestion": "测试建议",
      "confidence": 0.0-1.0,
      "evidence": ["工具输出摘要"]
    }
  ],
  "summary": "测试评估，1-2 句话",
  "recommendation": "adequate|needs_improvement|insufficient"
}

检查重点：
1. 新增代码是否包含对应测试
2. 边界条件是否被覆盖
3. 异常处理是否有测试
4. 现有测试是否需要更新
5. 建议的测试场景

当提供了工作区和验证命令时，你必须先用 read_file 查看相关文件，再调用 run_tests
实际运行验证命令。只有在 run_tests 返回后，才能给出 recommendation；evidence
必须引用真实工具输出，禁止凭 diff 猜测测试是否通过。

注意：你只能引用 diff 中明确出现的行号，禁止推测或引用未修改的代码行。
只输出 JSON，不要其他内容。"""


class TestCoverageAgent:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm

    async def execute(self, envelope: AgentEnvelope) -> str:
        llm = self._llm or build_code_review_llm()
        system = SYSTEM_PROMPT
        user_input = envelope.user_raw_input or json.dumps({
            "diff": envelope.agent_local_slot.get("diff", ""),
            "changed_files": envelope.agent_local_slot.get("changed_files", []),
            "existing_tests": envelope.agent_local_slot.get("existing_tests", []),
        }, ensure_ascii=False)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ]
        workspace_error = str(
            envelope.agent_local_slot.get("workspace_error", "") or ""
        ).strip()
        if workspace_error:
            return _downgrade_unmeasured(
                await llm.chat(messages),
                reason=workspace_error,
            )

        workspace = _workspace_path(envelope)
        verify_command = _verify_command(envelope)
        if workspace is not None and not verify_command:
            return _downgrade_unmeasured(
                await llm.chat(messages),
                reason="未检测到可用的测试命令",
            )
        if workspace is None:
            return await llm.chat(messages)
        if not hasattr(llm, "chat_with_tools"):
            return _downgrade_unmeasured(
                await llm.chat(messages),
                reason="LLM 客户端不支持工具调用",
            )

        tools = build_toolset(
            workspace=workspace,
            verify_command=verify_command,
            names=("read_file", "run_tests"),
        )
        response, attempted_tools, executed_tools, tool_outputs = await _run_tool_loop(
            llm, messages, tools, workspace=workspace
        )
        model_observed_tests = "run_tests" in executed_tools
        if "run_tests" not in attempted_tools:
            # The command is a product invariant, not a model suggestion. Some
            # models answer the JSON prompt directly and never ask for the
            # tool; in that case run the fixed verification command ourselves
            # before accepting any coverage conclusion.
            run_tests_tool = tools.get("run_tests")
            if run_tests_tool is None:
                return _downgrade_unmeasured(response, reason="run_tests 工具未注册")
            outcome = await call_workspace_tool(
                run_tests_tool, {}, workspace=workspace
            )
            attempted_tools.add("run_tests")
            if outcome.kind == "ok":
                executed_tools.add("run_tests")
                tool_outputs["run_tests"] = outcome.detail
            else:
                return _downgrade_unmeasured(response, reason=outcome.detail)
        if "run_tests" not in executed_tools:
            return _downgrade_unmeasured(
                response,
                reason=tool_outputs.get("run_tests", ""),
            )
        return _ensure_tool_evidence(
            response,
            test_output=tool_outputs.get("run_tests", ""),
            model_observed_tests=model_observed_tests,
        )


async def _run_tool_loop(
    llm: LLMClient,
    messages: list[dict[str, Any]],
    tools: Any,
    *,
    workspace: Path,
) -> tuple[str, set[str], set[str], dict[str, str]]:
    schemas = tools.list_schemas()
    result = await llm.chat_with_tools(messages, schemas)
    attempted_tools: set[str] = set()
    executed_tools: set[str] = set()
    tool_outputs: dict[str, str] = {}

    for _ in range(settings.agent_max_steps):
        if not result.tool_calls:
            break
        for call in result.tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name", ""))
            if name == "run_tests" and name in attempted_tools:
                output = '{"error": "run_tests 已请求过，忽略重复调用"}'
                result.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", name),
                        "content": output,
                    }
                )
                continue
            attempted_tools.add(name)
            tool = tools.get(name)
            if tool is None:
                output = '{"error": "unknown tool"}'
            else:
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    output = f'{{"error": "参数不是合法 JSON: {exc}"}}'
                    tool_outputs.setdefault(name, output)
                else:
                    outcome = await call_workspace_tool(
                        tool, arguments, workspace=workspace
                    )
                    if outcome.kind == "ok":
                        executed_tools.add(name)
                        tool_outputs[name] = outcome.detail
                        output = outcome.detail
                    elif outcome.kind == "rejected":
                        output = f'{{"error": "参数被拒：{outcome.detail}"}}'
                        tool_outputs.setdefault(name, str(outcome.detail))
                    else:
                        output = f'{{"error": "{outcome.detail}"}}'
                        tool_outputs.setdefault(name, str(outcome.detail))
            result.messages.append(
                {"role": "tool", "tool_call_id": call.get("id", name), "content": output}
            )
        result = await llm.chat_with_tools(result.messages, schemas)

    if result.tool_calls:
        pending = ", ".join(
            str((call.get("function") or {}).get("name", ""))
            for call in result.tool_calls
        )
        return (
            result.content + f"\n[工具调用已达上限，待处理工具: {pending}]",
            attempted_tools,
            executed_tools,
            tool_outputs,
        )
    return result.content, attempted_tools, executed_tools, tool_outputs


def _workspace_path(envelope: AgentEnvelope) -> Path | None:
    raw = envelope.agent_local_slot.get("workspace_path")
    if not raw:
        return None
    return Path(str(raw))


def _verify_command(envelope: AgentEnvelope) -> list[str]:
    raw = envelope.agent_local_slot.get("verify_command")
    if isinstance(raw, (list, tuple)) and raw:
        return [str(token) for token in raw]
    return []


def _downgrade_unmeasured(response: str, *, reason: str = "") -> str:
    """没有实际跑测试时，不允许把覆盖率结论留在 adequate/needs_improvement。"""
    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []
    findings = [item for item in findings if isinstance(item, dict)]
    findings = [
        item for item in findings if item.get("id") != "test-coverage-not-measured"
    ]
    findings.append(
        {
            "id": "test-coverage-not-measured",
            "severity": "medium",
            "category": "test_coverage",
            "file": "",
            "line": 0,
            "title": "覆盖率结论未经过实际测试",
            "description": (
                f"未获得可信测试结果：{reason}。覆盖率结论不可信。"
                if reason
                else "提供了工作区，但 agent 没有成功调用 run_tests，覆盖率结论不可信。"
            ),
            "suggestion": "先运行验证命令，再根据真实测试输出给出覆盖率判断。",
            "confidence": 1.0,
            "evidence": [],
        }
    )
    data["findings"] = findings
    data["recommendation"] = "insufficient"
    summary = str(data.get("summary") or "").strip()
    prefix = "覆盖率结论未经过实际测试。"
    if reason:
        prefix += f"{reason}。"
    if not summary.startswith(prefix):
        data["summary"] = f"{prefix}{summary}".strip()
    data["need_human_review"] = True
    return json.dumps(data, ensure_ascii=False)


def _ensure_tool_evidence(
    response: str,
    *,
    test_output: str,
    model_observed_tests: bool,
) -> str:
    """Keep the real test observation even when the model returns empty JSON.

    A model is allowed to decide what the test result means, but it is not
    allowed to erase the fact that a command ran. Some OpenAI-compatible
    models return an empty final message after a tool round; parsing that
    message as ``findings=[]`` would silently turn "tests ran" into "nothing
    found". The runner-owned output is therefore attached once, to the first
    finding, or represented by an explicit execution finding when the model
    produced no usable findings.
    """
    output = (test_output or "").strip()
    if not output:
        return _downgrade_unmeasured(
            response,
            reason="run_tests 未返回可验证输出",
        )

    try:
        data = json.loads(response)
    except (TypeError, json.JSONDecodeError):
        data = None

    parse_failed = not isinstance(data, dict)
    if not isinstance(data, dict):
        data = {
            "findings": [],
            "summary": "验证命令已执行，但模型没有返回可解析的覆盖率结论。",
            "recommendation": "insufficient",
        }

    findings = data.get("findings")
    if not isinstance(findings, list):
        findings = []
    findings = [item for item in findings if isinstance(item, dict)]

    if not findings:
        findings.append(
            {
                "id": "test-execution",
                "severity": "low",
                "category": "test_coverage",
                "file": "",
                "line": 0,
                "title": "验证命令已执行",
                "description": "覆盖率 agent 已实际运行验证命令，下面是原始输出。",
                "suggestion": "结合测试输出继续判断覆盖缺口。",
                "confidence": 1.0,
            }
        )

    # The model may already have copied the runner output into several
    # findings. Remove exact copies first, then attach the authoritative copy
    # once so the report does not grow with duplicate command output.
    for finding in findings:
        evidence = finding.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        finding["evidence"] = [
            item for item in evidence if str(item).strip() != output
        ]

    first_evidence = findings[0].get("evidence")
    if not isinstance(first_evidence, list):
        first_evidence = []
    first_evidence.append(f"command:{output}")
    findings[0]["evidence"] = first_evidence

    data["findings"] = findings
    data["tool_evidence"] = [f"command:{output}"]
    needs_review = parse_failed or bool(data.get("need_human_review", False))
    if not _test_run_passed(output):
        data["recommendation"] = "insufficient"
        needs_review = True
        summary = str(data.get("summary") or "").strip()
        prefix = "验证命令未通过。"
        if not summary.startswith(prefix):
            data["summary"] = f"{prefix}{summary}".strip()
    elif not model_observed_tests:
        data["recommendation"] = "insufficient"
        needs_review = True
        summary = str(data.get("summary") or "").strip()
        prefix = "验证命令由运行器补执行，模型未观察测试输出。"
        if not summary.startswith(prefix):
            data["summary"] = f"{prefix}{summary}".strip()
    elif str(data.get("recommendation") or "").strip() not in {
        "adequate",
        "needs_improvement",
        "insufficient",
    }:
        data["recommendation"] = "insufficient"
        needs_review = True

    data["need_human_review"] = needs_review
    return json.dumps(data, ensure_ascii=False)


def _test_run_passed(output: str) -> bool:
    """Read the exit code emitted by the fixed test runner.

    The model-facing recommendation is not trusted when the runner itself
    reports a non-zero exit. Keeping this check next to evidence merging makes
    a failed command unable to survive as ``adequate`` just because the model
    answered before the command result was appended.
    """
    match = re.search(r"退出码\s+(-?\d+)", output or "")
    return bool(match and int(match.group(1)) == 0)
