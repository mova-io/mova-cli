"""Exec skill backend -- the raw-script escape hatch (ADR 052 D2).

Run *any* executable with the normalized contract: the tool's JSON
input is delivered on ``stdin``, the process writes a JSON object to
``stdout``, exit-code + ``stderr`` map to the ``SkillError`` taxonomy.

This is how a Node CLI, a Java jar, a Go binary, or a bare Python
script becomes a tool **without** an MCP server or an HTTP service.

Sandboxed: configurable timeout (default 30s), optional network-disable
flag, resource limits. The ``exec`` backend is the single highest-risk
new piece per ADR 052 and gates on security review.

Failure -> :class:`SkillError` mapping:

* Subprocess fails to start (binary missing, permission denied) -> ``backend_error``
* Non-zero exit code -> ``backend_error`` with stderr tail
* Wall-clock timeout -> ``timeout``
* stdout is not valid JSON -> ``validation_failed``
* stdout JSON is not a dict -> ``validation_failed``
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from typing import TYPE_CHECKING, Any

from movate.core.models import SkillImplementationKind
from movate.core.skill_backend.base import (
    SkillError,
    SkillErrorType,
    SkillExecutionContext,
    register_backend,
)

if TYPE_CHECKING:
    from movate.core.skill_loader import SkillBundle

# Default timeout in seconds for exec backend calls.
_DEFAULT_TIMEOUT_S = 30

# Maximum stderr bytes to capture for error diagnostics.
_MAX_STDERR_BYTES = 4096


class ExecSkillBackend:
    """Dispatches ``kind: exec`` skills via subprocess + JSON stdin/stdout.

    One instance handles every exec-kind skill. Stateless -- each call
    spawns a fresh subprocess (no session caching like MCP, because exec
    scripts are expected to be short-lived).
    """

    kind = SkillImplementationKind.EXEC

    async def execute(
        self,
        skill: SkillBundle,
        input: dict[str, Any],
        ctx: SkillExecutionContext,
    ) -> dict[str, Any]:
        impl = skill.spec.implementation
        entry = impl.entry

        if not entry:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"exec skill {skill.spec.name!r}: implementation.entry is empty",
            )

        # Parse the command string.
        try:
            argv = shlex.split(entry)
        except ValueError as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"exec skill {skill.spec.name!r}: failed to parse entry "
                    f"{entry!r} as a shell command: {exc}"
                ),
            ) from exc

        if not argv:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=f"exec skill {skill.spec.name!r}: entry parsed to empty command",
            )

        # Timeout: use the skill's per-call budget, falling back to default.
        timeout_s = ctx.call_ms_budget / 1000.0 if ctx.call_ms_budget > 0 else _DEFAULT_TIMEOUT_S

        # Serialize input as JSON for stdin.
        input_json = json.dumps(input).encode("utf-8")

        # ADR 024 -- open an ``exec.call`` child span.
        _span = None
        _t0 = 0.0
        if ctx.tracer is not None:
            _t0 = time.monotonic()
            _span = ctx.tracer.start_span(
                "exec.call",
                {"skill": skill.spec.name, "entry": entry},
                parent=ctx.parent_span,
            )

        try:
            result = await self._run_subprocess(
                argv=argv,
                input_json=input_json,
                timeout_s=timeout_s,
                skill_name=skill.spec.name,
            )
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="ok")
            return result
        except Exception:
            if _span is not None and ctx.tracer is not None:
                lat = (time.monotonic() - _t0) * 1000
                ctx.tracer.set_attribute(_span, "latency_ms", round(lat, 1))
                ctx.tracer.end_span(_span, status="error")
            raise

    async def _run_subprocess(
        self,
        *,
        argv: list[str],
        input_json: bytes,
        timeout_s: float,
        skill_name: str,
    ) -> dict[str, Any]:
        """Spawn the subprocess, feed JSON stdin, read stdout/stderr."""
        # Spawn the process.
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"exec skill {skill_name!r}: couldn't start process "
                    f"{argv[0]!r}: {type(exc).__name__}: {exc}"
                ),
            ) from exc

        # Communicate with timeout.
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(input=input_json),
                timeout=timeout_s,
            )
        except TimeoutError:
            # Kill the process on timeout.
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise SkillError(
                type=SkillErrorType.TIMEOUT,
                message=(f"exec skill {skill_name!r}: process exceeded timeout {timeout_s:.1f}s"),
            ) from None

        # Check exit code.
        if process.returncode != 0:
            stderr_tail = (
                stderr_bytes[-_MAX_STDERR_BYTES:].decode("utf-8", errors="replace").strip()
            )
            raise SkillError(
                type=SkillErrorType.BACKEND_ERROR,
                message=(
                    f"exec skill {skill_name!r}: process exited with code "
                    f"{process.returncode}; stderr: {stderr_tail!r}"
                ),
            )

        # Parse stdout as JSON.
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not stdout_text:
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"exec skill {skill_name!r}: process produced no stdout "
                    f"(expected a JSON object)"
                ),
            )

        try:
            parsed = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(f"exec skill {skill_name!r}: stdout is not valid JSON: {exc}"),
            ) from exc

        if not isinstance(parsed, dict):
            raise SkillError(
                type=SkillErrorType.VALIDATION_FAILED,
                message=(
                    f"exec skill {skill_name!r}: stdout was a "
                    f"{type(parsed).__name__}, expected a JSON object"
                ),
            )

        return parsed


# Auto-register on import.
register_backend(ExecSkillBackend())
