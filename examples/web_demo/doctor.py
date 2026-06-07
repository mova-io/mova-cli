"""mdk-voice pre-flight doctor — run before any demo.

    python examples/web_demo/doctor.py

Checks the things that fail demos in their first 30 seconds: keys present and
readable, perms tight, Python deps importable, the demo port free. Prints one
green/yellow/red line per check + a final summary. Exits non-zero on any red.

Deliberately OFFLINE — no API calls (those cost money and are slow). Use
`python examples/live_smoke.py` for an end-to-end provider ping.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import stat
import sys
from pathlib import Path

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


# Each provider check: (env var, key-file basename, expected prefix or None,
# minimum sane size in bytes, required for the demo to start)
PROVIDERS = [
    ("OPENAI_API_KEY", "openai", ("sk-",), 40, True),
    ("CARTESIA_API_KEY", "cartesia", ("sk_car_",), 20, False),
    ("DEEPGRAM_API_KEY", "deepgram", None, 30, False),
]

# Python deps the demo needs (import name → install hint).
DEPS = [
    ("movate.voice", "pip install -e '.[voice]'"),
    ("fastapi", "pip install fastapi 'uvicorn[standard]'"),
    ("uvicorn", "pip install fastapi 'uvicorn[standard]'"),
    ("openai", "pip install -e '.[openai]'"),
    ("cartesia", "pip install -e '.[cartesia]'"),
    ("deepgram", "pip install -e '.[deepgram]'"),
]

DEMO_PORT = 8765


class Result:
    def __init__(self) -> None:
        self.reds = 0
        self.yellows = 0
        self.greens = 0

    def ok(self, msg: str) -> None:
        print(f"  {GREEN}✓{RESET} {msg}")
        self.greens += 1

    def warn(self, msg: str, hint: str = "") -> None:
        print(f"  {YELLOW}⚠{RESET} {msg}" + (f"  {DIM}{hint}{RESET}" if hint else ""))
        self.yellows += 1

    def fail(self, msg: str, hint: str = "") -> None:
        print(f"  {RED}✗{RESET} {msg}" + (f"  {DIM}{hint}{RESET}" if hint else ""))
        self.reds += 1


def _hr(title: str) -> None:
    print(f"\n{title}")
    print("─" * 64)


def check_keys(r: Result) -> None:
    _hr("API keys")
    home = Path.home()
    for env, name, prefixes, min_size, required in PROVIDERS:
        path = home / f".mdk_{name}_key"
        label = f"{name:<10} (~/.mdk_{name}_key)"
        if os.environ.get(env):
            r.ok(f"{label}: {env} set in environment")
            continue
        if not path.is_file():
            (r.fail if required else r.warn)(
                f"{label}: missing",
                f"create with: printf 'KEY' > {path} && chmod 600 {path}",
            )
            continue
        mode = path.stat().st_mode & 0o777
        if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
            r.warn(
                f"{label}: world/group-readable ({oct(mode)})",
                f"fix: chmod 600 {path}",
            )
            continue
        try:
            text = path.read_text().strip()
        except OSError as e:
            r.fail(f"{label}: cannot read ({e})")
            continue
        if not text:
            r.fail(f"{label}: file is empty")
            continue
        if len(text) < min_size:
            r.warn(
                f"{label}: looks short ({len(text)} bytes; expected ≥{min_size})",
                "may be truncated — copy from the provider dashboard again",
            )
            continue
        if prefixes and not any(text.startswith(p) for p in prefixes):
            wanted = " or ".join(prefixes)
            r.warn(
                f"{label}: prefix {text[:7]!r}... expected {wanted}",
                "may be wrong key — verify in the provider dashboard",
            )
            continue
        r.ok(f"{label}: {len(text)} bytes, perms 600, prefix matches")


def check_deps(r: Result) -> None:
    _hr("Python dependencies")
    for module, hint in DEPS:
        if importlib.util.find_spec(module) is None:
            r.fail(f"{module}: not importable", hint)
        else:
            r.ok(f"{module}: importable")


def check_python(r: Result) -> None:
    _hr("Python runtime")
    major, minor = sys.version_info[:2]
    if (major, minor) < (3, 11):
        r.fail(f"Python {major}.{minor} — need ≥3.11", "use uv venv --python 3.11")
    else:
        r.ok(f"Python {major}.{minor}.{sys.version_info[2]}")


def check_port(r: Result) -> None:
    _hr(f"Demo port (tcp/{DEMO_PORT})")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", DEMO_PORT))
        r.ok(f"port {DEMO_PORT} free")
    except OSError as e:
        r.fail(
            f"port {DEMO_PORT} in use ({e.strerror or e})",
            f"find + kill: lsof -ti tcp:{DEMO_PORT} | xargs kill -9",
        )
    finally:
        s.close()


def check_browser_hint() -> None:
    _hr("Browser (manual)")
    print(f"  {DIM}• Chrome/Edge/Safari on macOS — grant mic permission on first open{RESET}")
    print(
        f"  {DIM}• If using Safari: click anywhere on the page once before pressing 'talk'{RESET}"
    )
    print(f"  {DIM}• Localhost is treated as secure (no HTTPS needed for getUserMedia){RESET}")


def main() -> int:
    print(f"\nmdk-voice pre-flight — {Path.cwd()}")
    r = Result()
    check_python(r)
    check_deps(r)
    check_keys(r)
    check_port(r)
    check_browser_hint()
    print()
    print(f"{GREEN}✓ {r.greens}{RESET}  {YELLOW}⚠ {r.yellows}{RESET}  {RED}✗ {r.reds}{RESET}")
    if r.reds:
        print(f"\n{RED}NOT ready{RESET} — fix the ✗ items above, then re-run.")
        return 1
    if r.yellows:
        print(
            f"\n{YELLOW}Ready with warnings{RESET} — demo will run; "
            "address the ⚠ items when convenient."
        )
        return 0
    print(f"\n{GREEN}All green — ready to demo.{RESET}  Run:  python examples/web_demo/server.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
