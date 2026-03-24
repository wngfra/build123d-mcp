"""Shared helpers for build123d CAD scripts.

Security: user-supplied scripts NEVER execute in the parent process.
All execution happens in a sandboxed subprocess with:
  - Static validation blocks dangerous imports, builtins, and patterns
  - Clean environment (no API keys, tokens, secrets)
  - Isolated tmpdir as cwd
  - Timeout (default 120s for complex geometry)
  - Result communicated via a JSON file, not stdout parsing

The parent constructs a full Python script (user code + operation), writes it
to a temp file, runs it in a subprocess, reads the result JSON file.
No geometry objects cross the process boundary.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
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
    r"\bopen\s*\(",
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


# Builtin names that must never be called or referenced in user code.
_DANGEROUS_BUILTINS = {
    "eval", "exec", "compile", "open", "breakpoint",
    "__import__", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "memoryview",
    "exit", "quit", "help", "input", "print",
}

# Attribute names that are blocked regardless of the object they appear on.
_DANGEROUS_ATTRS = {
    "__subclasses__", "__bases__", "__mro__", "__class__",
    "__globals__", "__code__", "__func__", "__self__",
    "__builtins__", "__import__", "__loader__", "__spec__",
    "system", "popen", "exec", "spawn", "remove", "unlink",
    "rmdir", "rmtree",
}


def _ast_validate(script: str) -> str | None:
    """Walk the AST of *script* and reject dangerous constructs.

    This catches obfuscation tricks that regex alone would miss, such as
    ``getattr(__builtins__, 'ev'+'al')`` or dunder-chain escapes.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError:
        # Syntax errors will be caught at runtime; let them through.
        return None

    for node in ast.walk(tree):
        # --- imports ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name.split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    return f"Blocked: import of '{mod}' is not allowed"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod = node.module.split(".")[0]
                if mod not in ALLOWED_IMPORTS:
                    return f"Blocked: import of '{mod}' is not allowed"

        # --- dangerous builtin calls ---
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DANGEROUS_BUILTINS:
                return f"Blocked: call to builtin '{func.id}' is not allowed"

        # --- dangerous attribute access ---
        elif isinstance(node, ast.Attribute):
            if node.attr in _DANGEROUS_ATTRS:
                return f"Blocked: access to attribute '{node.attr}' is not allowed"

        # --- bare Name references to dangerous builtins (not just calls) ---
        elif isinstance(node, ast.Name):
            if node.id == "__import__":
                return "Blocked: reference to '__import__' is not allowed"

    return None


def validate_script(script: str) -> str | None:
    """Check script for disallowed patterns. Returns error message or None if clean.

    Applies two layers of defence:
      1. Regex pattern matching (fast, catches obvious cases)
      2. AST walk (catches obfuscation / attribute-chain tricks)
    """
    # Layer 1 — regex
    for pattern in BLOCKED_PATTERNS:
        match = re.search(pattern, script)
        if match:
            return f"Blocked: script contains disallowed pattern '{match.group()}'"

    # Check imports — only allow whitelisted modules (regex pass)
    for match in re.finditer(r"(?:from|import)\s+([\w.]+)", script):
        module = match.group(1).split(".")[0]
        if module not in ALLOWED_IMPORTS:
            return f"Blocked: import of '{module}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}"

    # Layer 2 — AST
    ast_err = _ast_validate(script)
    if ast_err:
        return ast_err

    return None


def _extract_user_script(full_script: str) -> str | None:
    """Extract user code between marker comments, or return None if no markers."""
    m = re.search(
        r"# --- user script ---\n(.*?)# --- end user script ---",
        full_script, re.DOTALL,
    )
    return m.group(1) if m else None


# Preamble injected at the top of every sandboxed script to restrict
# __builtins__ at runtime, closing the gap that static analysis alone
# cannot fully cover.
_BUILTINS_PREAMBLE = """\
import builtins as _b
_SAFE_BUILTINS = {
    'abs','all','any','bin','bool','bytearray','bytes','callable','chr',
    'classmethod','complex','dict','divmod','enumerate','filter','float',
    'format','frozenset','hasattr','hash','hex','id','int','isinstance',
    'issubclass','iter','len','list','map','max','min','next','object',
    'oct','ord','pow','property','range','repr','reversed','round','set',
    'slice','sorted','staticmethod','str','sum','super','tuple','type',
    'zip','True','False','None','NotImplemented','Ellipsis',
    '__name__','__build_class__','__spec__',
    'ArithmeticError','AssertionError','AttributeError','BaseException',
    'BlockingIOError','BrokenPipeError','BufferError','BytesWarning',
    'ChildProcessError','ConnectionAbortedError','ConnectionError',
    'ConnectionRefusedError','ConnectionResetError','DeprecationWarning',
    'EOFError','EnvironmentError','Exception','FileExistsError',
    'FileNotFoundError','FloatingPointError','FutureWarning',
    'GeneratorExit','IOError','ImportError','IndexError',
    'InterruptedError','IsADirectoryError','KeyError','KeyboardInterrupt',
    'LookupError','MemoryError','ModuleNotFoundError','NameError',
    'NotADirectoryError','NotImplementedError','OSError','OverflowError',
    'PendingDeprecationWarning','PermissionError','ProcessLookupError',
    'RecursionError','ReferenceError','ResourceWarning','RuntimeError',
    'RuntimeWarning','StopAsyncIteration','StopIteration','SyntaxError',
    'SyntaxWarning','SystemError','SystemExit','TimeoutError','TypeError',
    'UnboundLocalError','UnicodeDecodeError','UnicodeEncodeError',
    'UnicodeError','UnicodeTranslationError','UnicodeWarning',
    'UserWarning','ValueError','Warning','ZeroDivisionError',
}
_restricted = {k: getattr(_b, k) for k in _SAFE_BUILTINS if hasattr(_b, k)}
__builtins__ = _restricted
del _b, _SAFE_BUILTINS, _restricted
"""


def run_sandboxed(full_script: str, timeout: int = 120) -> dict:
    """Run a complete Python script in a sandboxed subprocess.

    The script must write JSON to the path in env var _RESULT_PATH.
    User code (between marker comments) is validated before execution.
    """
    user_code = _extract_user_script(full_script)
    if user_code is not None:
        violation = validate_script(user_code)
        if violation:
            return {"success": False, "error": violation}
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "run.py"
        result_path = Path(tmpdir) / "result.json"
        script_path.write_text(_BUILTINS_PREAMBLE + full_script)

        clean_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmpdir,
            "TMPDIR": tmpdir,
            "PYTHONDONTWRITEBYTECODE": "1",
            "_RESULT_PATH": str(result_path),
            "_WORKSPACE": str(WORKSPACE),
        }
        venv = os.environ.get("VIRTUAL_ENV")
        if venv:
            clean_env["VIRTUAL_ENV"] = venv
            clean_env["PATH"] = f"{venv}/bin:{clean_env['PATH']}"

        try:
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                capture_output=True, text=True,
                timeout=timeout, cwd=tmpdir, env=clean_env,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": f"Script timed out after {timeout}s."}

        if result_path.exists():
            try:
                return json.loads(result_path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                return {"success": False, "error": f"Bad result file: {e}", "stderr": proc.stderr[-2000:]}

        return {
            "success": False,
            "error": proc.stderr[-2000:] if proc.stderr else "No result produced.",
            "stdout": proc.stdout[-2000:],
        }


def output_json(data: dict):
    print(json.dumps(data))
    sys.exit(0 if data.get("success", False) else 1)
