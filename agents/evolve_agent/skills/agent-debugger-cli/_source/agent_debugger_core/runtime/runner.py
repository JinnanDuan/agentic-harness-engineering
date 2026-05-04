from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from agent_debugger_core.cli.llm_resolver import resolve_llm_settings
from agent_debugger_core.runtime.bootstrap import ensure_tools_importable


RUNTIME_DIR = Path(__file__).parent
AGENT_CONFIG_PATH = RUNTIME_DIR / "agent_config.yaml"

ALLOWED_ISSUE_TYPES = {"工具错误", "幻觉", "循环", "不合规", "截断"}
BUDGET_MARKER = "[Note: Maximum iteration limit reached]"

ASK_SCHEMA_RETRY_SUFFIX = (
    "\n\nYour last complete_task payload did not match the ask schema: "
    'expected `{"mode": "ask", "answer": "<your reply>"}` '
    "(a JSON object whose string fields go inside complete_task `result`). "
    'Do NOT use `"mode": "check"`, `issues`, or `response` here — those belong to QC/check mode only. '
    "Even if the Question lists ROOT CAUSE / numbered items, put the entire reply in the single "
    "string field `answer`.\n"
    "Re-emit a valid `ask` payload that matches the schema exactly."
)


def _runner_io_log_enabled() -> bool:
    return (os.environ.get("ADB_RUNNER_IO_LOG") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _runner_io_dump(label: str, text: Any, *, max_chars: int = 120_000) -> None:
    """Debug helper: log model-facing input / agent.run() raw output to stderr.

    Enable with ``ADB_RUNNER_IO_LOG=1``. When ``evolve.py`` runs ``adb ask`` with
    ``capture_output=True``, this stderr shows up under ``--- stderr ---`` in
    ``adb_diag/<task>.log``.
    """
    if not _runner_io_log_enabled():
        return
    _runner_diag_dump(label, text, max_chars=max_chars, prefix="[ADB_RUNNER_IO]")


def _runner_diag_dump(
    label: str,
    text: Any,
    *,
    max_chars: int = 120_000,
    prefix: str = "[ADB_RUNNER_DIAG]",
) -> None:
    """Always emit to stderr (truncated). Used on failures so ``adb_diag`` captures context."""
    s = text if isinstance(text, str) else repr(text)
    n = len(s)
    if n <= max_chars:
        body = s
    else:
        half = max_chars // 2
        body = (
            s[:half]
            + f"\n... [{n - max_chars} chars omitted middle] ...\n"
            + s[-half:]
        )
    print(f"{prefix} {label} ({n} chars)", file=sys.stderr, flush=True)
    print(body, file=sys.stderr, flush=True)


def _dump_failure_context(
    *,
    reason: str,
    user_message: str,
    run_output: Any,
    payload: Any | None = None,
    max_chars: int = 120_000,
) -> None:
    _runner_diag_dump(
        f"{reason}: user_message (agent.run message=…)",
        user_message,
        max_chars=max_chars,
    )
    _runner_diag_dump(
        f"{reason}: raw agent.run() return",
        run_output,
        max_chars=max_chars,
    )
    if payload is not None:
        dumped = (
            json.dumps(payload, ensure_ascii=False, indent=2)
            if isinstance(payload, dict)
            else repr(payload)
        )
        _runner_diag_dump(
            f"{reason}: parsed inner payload (complete_task result)",
            dumped,
            max_chars=max_chars,
        )


class RunnerError(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, fallback_text: str):
        super().__init__(fallback_text)
        self.fallback_text = fallback_text


@dataclass
class RunnerResult:
    mode: str
    answer: Optional[str] = None
    issues: List[dict] = field(default_factory=list)
    response: Optional[str] = None
    iterations: int = 0
    budget_exceeded: bool = False


def _build_user_message(trace_paths: List[Path], mode: str, question: Optional[str]) -> str:
    lines = ["Analyze the following normalized trace file(s):"]
    for p in trace_paths:
        lines.append(f"- {p}")
    lines.append("")
    if mode == "ask":
        lines.append(f"Question: {question or 'Why is this trace so slow?'}")
        lines.append(
            "Output contract for this turn: use complete_task with JSON for **ask** mode only — "
            '`{"mode": "ask", "answer": "<your full analysis as one string>"}`. '
            "Put every section (including any ROOT CAUSE / PASS vs FAIL bullets) inside `answer`. "
            'Do NOT emit `mode: "check"`, `issues`, or `response`.'
        )
    else:
        lines.append(
            "Task: produce a QC report. Return a JSON payload with "
            '`mode="check"`, `issues=[...]`, and `response="..."`.'
        )
    return "\n".join(lines)


def _build_agent(llm_settings: dict) -> Any:
    """Construct nexau.Agent from agent_config.yaml with LLM settings patched in."""
    import os as _os
    ensure_tools_importable()
    from nexau import Agent, AgentConfig  # noqa: WPS433 — deferred import

    # Populate env vars that agent_config.yaml references via ${env.LLM_*}.
    # AgentConfig.from_yaml substitutes env at load time, so we must set these
    # *before* the load. Caller-supplied llm_settings win; we still overwrite
    # the config fields below for belt-and-suspenders.
    _os.environ["LLM_MODEL"] = llm_settings["model"]
    _os.environ["LLM_BASE_URL"] = llm_settings["base_url"]
    _os.environ["LLM_API_KEY"] = llm_settings["api_key"]
    _os.environ.setdefault("LLM_API_TYPE", "openai_chat_completion")

    config = AgentConfig.from_yaml(config_path=AGENT_CONFIG_PATH)
    config.llm_config.model = llm_settings["model"]
    config.llm_config.base_url = llm_settings["base_url"]
    config.llm_config.api_key = llm_settings["api_key"]
    if "reasoning" in llm_settings:
        config.llm_config.reasoning = llm_settings["reasoning"]
    return Agent(config=config)


def _parse_inner_payload(raw: Any) -> dict:
    if not isinstance(raw, str):
        raise RunnerError("complete_task `result` is not a string")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            raise RunnerError(f"complete_task result is not JSON: {raw[:200]}")
        return json.loads(m.group(0))


def _parse_run_output(run_output: Any) -> dict:
    """Parse nexau.Agent.run() string output into the inner JSON payload.

    Raises BudgetExceeded when the agent hit max_iterations without calling
    complete_task; the exception carries the last-assistant text so callers
    can surface it as a [budget-exceeded] fallback.
    """
    s = str(run_output or "").strip()
    if not s:
        raise BudgetExceeded("")

    if BUDGET_MARKER in s:
        raise BudgetExceeded(s.replace(BUDGET_MARKER, "").strip())

    # nexau.Agent.run() may return a plain JSON string OR a JSON-encoded
    # JSON string (double-encoded). Unwrap up to 2 times until we see an object.
    outer = s
    for _ in range(2):
        try:
            outer = json.loads(outer)
        except json.JSONDecodeError:
            raise RunnerError(f"agent.run() output is not JSON: {s[:200]}")
        if isinstance(outer, dict):
            break
        if not isinstance(outer, str):
            raise RunnerError(f"agent.run() output is not a JSON object: {s[:200]}")
    else:
        raise RunnerError(f"agent.run() output is not a JSON object: {s[:200]}")

    status = outer.get("status")
    if status != "TASK_COMPLETED" or not outer.get("task_completed"):
        raise RunnerError(
            f"agent ended without completing task (status={status!r}): {s[:200]}"
        )

    output = outer.get("output") or {}
    raw_result = output.get("result") if isinstance(output, dict) else None
    return _parse_inner_payload(raw_result)


def _validate_check_payload(payload: dict) -> None:
    if payload.get("mode") != "check":
        raise RunnerError(f"expected mode=check, got {payload.get('mode')!r}")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise RunnerError("check payload missing `issues` list")
    for i, it in enumerate(issues):
        if not isinstance(it, dict):
            raise RunnerError(f"issue #{i} is not an object")
        for k in ("issue_type", "summary", "evidence", "message_index"):
            if k not in it:
                raise RunnerError(f"issue #{i} missing `{k}`")
        if it["issue_type"] not in ALLOWED_ISSUE_TYPES:
            raise RunnerError(
                f"issue #{i} has invalid issue_type={it['issue_type']!r}; "
                f"must be one of {sorted(ALLOWED_ISSUE_TYPES)}"
            )
        if not isinstance(it["message_index"], int):
            raise RunnerError(f"issue #{i}.message_index must be int")


def _resolve_ask_answer(payload: dict) -> tuple[Optional[str], bool]:
    """Extract text for ask mode. Returns (answer, coerced_from_check).

    Models sometimes emit check-shaped JSON (`mode=check`, `response=...`) while the CLI
    invoked ask mode; accept `response` as the answer string when present.
    """
    ans = payload.get("answer")
    if isinstance(ans, str):
        return ans, False
    if payload.get("mode") == "check":
        resp = payload.get("response")
        if isinstance(resp, str):
            return resp, True
    return None, False


def _run_with_retry(agent, user_message: str, *, attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            return agent.run(message=user_message)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise RunnerError(f"llm: {last}")


def run_agent(
    *,
    trace_paths: List[Path],
    mode: str,
    question: Optional[str] = None,
) -> RunnerResult:
    if mode not in ("ask", "check"):
        raise RunnerError(f"unknown mode: {mode}")
    llm_settings = resolve_llm_settings()
    agent = _build_agent(llm_settings)
    user_message = _build_user_message(trace_paths, mode, question)
    _runner_io_dump(f"run_agent mode={mode} user_message (to agent.run)", user_message)

    run_output = _run_with_retry(agent, user_message)
    _runner_io_dump(
        f"run_agent mode={mode} raw agent.run() return",
        run_output,
    )

    try:
        payload = _parse_run_output(run_output)
    except RunnerError as err:
        _dump_failure_context(
            reason=f"run_agent parse error ({err})",
            user_message=user_message,
            run_output=run_output,
        )
        raise
    except BudgetExceeded as be:
        fallback_text = be.fallback_text
        if mode == "ask":
            return RunnerResult(
                mode="ask",
                answer=f"[budget-exceeded] {fallback_text}".strip(),
                budget_exceeded=True,
            )
        return RunnerResult(
            mode="check",
            issues=[],
            response=f"[budget-exceeded] {fallback_text}".strip(),
            budget_exceeded=True,
        )

    if mode == "ask":
        answer, coerced = _resolve_ask_answer(payload)
        if isinstance(answer, str):
            if coerced:
                dumped = json.dumps(payload, ensure_ascii=False, indent=2)
                _runner_diag_dump(
                    "ask mode: accepted check-shaped payload; using `response` as `answer`",
                    dumped,
                    max_chars=80_000,
                )
            return RunnerResult(mode="ask", answer=answer)

        _dump_failure_context(
            reason="ask payload invalid (need string `answer` or check-shaped `response`)",
            user_message=user_message,
            run_output=run_output,
            payload=payload,
        )
        retry_msg = user_message + ASK_SCHEMA_RETRY_SUFFIX
        _runner_io_dump(
            "run_agent ask retry user_message (after schema rejection)",
            retry_msg,
        )
        run_output = _run_with_retry(agent, retry_msg)
        _runner_io_dump(
            "run_agent ask retry raw agent.run() return",
            run_output,
        )
        try:
            payload = _parse_run_output(run_output)
        except RunnerError as err:
            _dump_failure_context(
                reason=f"ask retry parse error ({err})",
                user_message=retry_msg,
                run_output=run_output,
            )
            raise
        except BudgetExceeded as be:
            return RunnerResult(
                mode="ask",
                answer=f"[budget-exceeded] {be.fallback_text}".strip(),
                budget_exceeded=True,
            )
        answer, coerced = _resolve_ask_answer(payload)
        if isinstance(answer, str):
            if coerced:
                dumped = json.dumps(payload, ensure_ascii=False, indent=2)
                _runner_diag_dump(
                    "ask mode (retry): accepted check-shaped payload; using `response` as `answer`",
                    dumped,
                    max_chars=80_000,
                )
            return RunnerResult(mode="ask", answer=answer)
        _dump_failure_context(
            reason="ask payload still invalid after retry (need string `answer`)",
            user_message=retry_msg,
            run_output=run_output,
            payload=payload,
        )
        raise RunnerError("ask payload missing string `answer`")

    try:
        _validate_check_payload(payload)
    except RunnerError as first_err:
        _dump_failure_context(
            reason=f"check payload rejected ({first_err})",
            user_message=user_message,
            run_output=run_output,
            payload=payload,
        )
        retry_msg = (
            user_message
            + "\n\nYour last complete_task payload was rejected: "
            + str(first_err)
            + "\nRe-emit a valid `check` payload that matches the schema exactly."
        )
        _runner_io_dump(
            "run_agent check retry user_message (after schema rejection)",
            retry_msg,
        )
        run_output = _run_with_retry(agent, retry_msg)
        _runner_io_dump(
            "run_agent check retry raw agent.run() return",
            run_output,
        )
        try:
            payload = _parse_run_output(run_output)
        except RunnerError as err:
            _dump_failure_context(
                reason=f"check retry parse error ({err})",
                user_message=retry_msg,
                run_output=run_output,
            )
            raise
        except BudgetExceeded as be:
            return RunnerResult(
                mode="check",
                issues=[],
                response=f"[budget-exceeded] {be.fallback_text}".strip(),
                budget_exceeded=True,
            )
        try:
            _validate_check_payload(payload)
        except RunnerError as err:
            _dump_failure_context(
                reason=f"check payload still invalid after retry ({err})",
                user_message=retry_msg,
                run_output=run_output,
                payload=payload,
            )
            raise

    return RunnerResult(
        mode="check",
        issues=payload["issues"],
        response=str(payload.get("response", "") or ""),
    )
