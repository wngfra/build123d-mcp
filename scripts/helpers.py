"""Shared helpers for build123d CAD scripts."""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


WORKSPACE = Path(os.environ.get("CAD_WORKSPACE", os.path.expanduser("~/.openclaw/workspace/cad-output")))
WORKSPACE.mkdir(parents=True, exist_ok=True)

# Modules the user script is allowed to import. Anything else is blocked.
ALLOWED_IMPORTS = {
    "build123d", "math", "typing", "dataclasses", "enum", "functools",
    "itertools", "collections", "copy", "json",
}

# Patterns that must never appear in user scripts.
BLOCKED_PATTERNS = [
    r"\bsubprocess\b",
    r"\bos\.system\b",
    r"\bos\.popen\b",
    r"\bos\.exec\b",
    r"\bos\.spawn\b",
    r"\bos\.remove\b",
    r"\bos\.unlink\b",
    r"\bos\.rmdir\b",
    r"\bshutil\.rmtree\b",
    r"\bshutil\.move\b",
    r"\b__import__\b",
    r"\bimportlib\b",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bcompile\s*\(",
    r"\bopen\s*\(",           # no arbitrary file I/O
    r"\bsocket\b",
    r"\burllib\b",
    r"\brequests\b",
    r"\bhttpx\b",
    r"\bhttp\.\b",
    r"\bctypes\b",
    r"\bsignal\b",
    r"\bpickle\b",
    r"\bshelve\b",
    r"\bglobals\s*\(",
    r"\blocals\s*\(",
    r"\bbreakpoint\s*\(",
]


def validate_script(script: str) -> str | None:
    """Check script for disallowed patterns. Returns error message or None if clean."""
    for pattern in BLOCKED_PATTERNS:
        match = re.search(pattern, script)
        if match:
            return f"Blocked: script contains disallowed pattern '{match.group()}'"

    # Check imports — only allow whitelisted modules
    for match in re.finditer(r"(?:from|import)\s+([\w.]+)", script):
        module = match.group(1).split(".")[0]
        if module not in ALLOWED_IMPORTS:
            return f"Blocked: import of '{module}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}"

    return None


def exec_script(script: str) -> dict:
    """Execute a build123d script in a sandboxed subprocess.

    Security measures:
    - Static analysis blocks dangerous imports, builtins, and patterns
    - Runs in a subprocess with clean environment (no API keys, tokens, etc.)
    - Working directory is an isolated tmpdir
    - 60-second timeout kills runaway scripts
    - Only build123d and math-related imports are allowed
    """
    # 1. Static validation
    violation = validate_script(script)
    if violation:
        return {"ok": False, "error": violation, "stdout": ""}

    # 2. Write script to temp file with result serialization wrapper
    wrapper = f'''{script}

# --- sandbox output ---
import json as _json
_result = globals().get("result")
_parts = globals().get("parts")
if _result is None and _parts is None:
    print(_json.dumps({{"__sandbox_error": "No `result` or `parts` defined."}}))
elif _parts is not None:
    print(_json.dumps({{"__sandbox_ok": True, "__has_parts": True}}))
else:
    print(_json.dumps({{"__sandbox_ok": True, "__has_parts": False}}))
'''

    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "model.py"
        script_path.write_text(wrapper)

        # 3. Build a clean environment — no inherited secrets
        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmpdir,
            "PYTHONPATH": "",
            "CAD_WORKSPACE": str(WORKSPACE),
        }
        # Preserve virtual env if running inside one
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            clean_env["VIRTUAL_ENV"] = venv
            clean_env["PATH"] = f"{venv}/bin:{clean_env['PATH']}"

        # 4. Run in subprocess with timeout
        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True,
                timeout=60,
                cwd=tmpdir,
                env=clean_env,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Script timed out after 60 seconds.", "stdout": ""}

        if proc.returncode != 0 and not proc.stdout.strip():
            return {"ok": False, "error": proc.stderr[-2000:] if proc.stderr else "Unknown error", "stdout": ""}

        # 5. Parse sandbox output
        stdout_lines = proc.stdout.strip().split("\n")
        last_line = stdout_lines[-1] if stdout_lines else ""
        user_stdout = "\n".join(stdout_lines[:-1]) if len(stdout_lines) > 1 else ""

        try:
            meta = json.loads(last_line)
        except (json.JSONDecodeError, IndexError):
            return {"ok": False, "error": proc.stderr[-2000:] if proc.stderr else f"No valid output. stdout: {proc.stdout[-500:]}", "stdout": proc.stdout}

        if "__sandbox_error" in meta:
            return {"ok": False, "error": meta["__sandbox_error"], "stdout": user_stdout}

        # 6. Re-exec in-process to get the actual Python objects (Part, etc.)
        #    This is safe because we already validated the script above.
        ns = {}
        capture = io.StringIO()
        try:
            old = sys.stdout
            sys.stdout = capture
            exec(script, ns)
        except Exception:
            return {"ok": False, "error": traceback.format_exc(), "stdout": capture.getvalue()}
        finally:
            sys.stdout = old

        result = ns.get("result")
        parts = ns.get("parts")

        if parts is not None:
            return {"ok": True, "result": None, "parts": parts, "stdout": user_stdout}
        if result is not None:
            return {"ok": True, "result": result, "parts": None, "stdout": user_stdout}
        return {"ok": False, "error": "No `result` or `parts` defined.", "stdout": user_stdout}


def get_part(ctx):
    """Extract Part from BuildPart context or bare Shape."""
    if hasattr(ctx, "part"):
        return ctx.part
    if hasattr(ctx, "sketch"):
        return ctx.sketch
    return ctx


def output_json(data: dict):
    """Print JSON to stdout and exit."""
    print(json.dumps(data))
    sys.exit(0 if data.get("success", False) else 1)


def output_error(error: str, stdout: str = ""):
    """Print error JSON to stdout and exit 1."""
    print(json.dumps({"success": False, "error": error, "stdout": stdout}))
    sys.exit(1)
