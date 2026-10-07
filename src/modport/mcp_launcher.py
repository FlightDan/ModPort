"""Launch host-owned MCP modules without importing project workspace code."""
from __future__ import annotations

from pathlib import Path
import os
import sys


def trusted_mcp_command(module: str, source_file: str, session: Path) -> list[str]:
    if module not in {"modport.opencode_shell_mcp", "modport.rework_mcp"}:
        raise ValueError("unsupported host MCP module")
    source_root = Path(source_file).resolve().parents[1]
    # OpenCode starts local MCPs in the project workspace. Python -m places
    # that directory before PYTHONPATH, allowing a project-owned modport/
    # package to run with host MCP privileges. -I removes cwd and Python env
    # paths; this fixed code then inserts only the installed/current ModPort
    # source root before importing the trusted entry point.
    launcher = ("import sys; "
                f"sys.path.insert(0, {str(source_root)!r}); "
                f"from {module} import main; raise SystemExit(main())")
    if os.name == 'nt':
        # Windows has no env -i. Strip credentials before importing host tools.
        clean = ("import os; "
                 "e={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}; "
                 "e['PATH']=os.defpath; os.environ.clear(); os.environ.update(e); ")
        return [sys.executable, '-I', '-c', clean + launcher, '--session', str(session)]
    return ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", sys.executable,
            "-I", "-c", launcher, "--session", str(session)]
