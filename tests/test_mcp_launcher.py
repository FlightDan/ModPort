"""Host MCPs must not import an untrusted project's shadow package."""
from pathlib import Path
import json
import subprocess
import tempfile
import time
import unittest

from modport.evidence import atomic_json
from modport.opencode_shell_mcp import prepare_sandbox_tool
from modport.rework_tools import opencode_tool_config


class TrustedMcpLauncherTests(unittest.TestCase):
    def test_project_modport_package_cannot_shadow_host_mcp_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "run"
            workspace = root / "worktree"
            shadow = workspace / "modport"
            shadow.mkdir(parents=True)
            marker = root / "shadow-imported"
            payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
            (shadow / "__init__.py").write_text(payload)
            for name in ("opencode_shell_mcp", "rework_mcp"):
                (shadow / (name + ".py")).write_text(payload)

            sandbox = prepare_sandbox_tool(root, workspace, "shadow-probe", 10)
            rework_session = root / "artifacts" / "rework-shadow-session.json"
            atomic_json(rework_session, {
                "run_id": "run", "reviewer_execution_id": "reviewer:1",
                "deadline_epoch": time.time() + 10, "drain_pending_on_eof": True,
                "targets": [],
            })
            rework = opencode_tool_config(rework_session, 10)
            request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18"}}) + "\n"
            for command in (sandbox["modport_sandbox"]["command"],
                            rework["modport_rework"]["command"]):
                with self.subTest(command=command[-2]):
                    result = subprocess.run(command, cwd=workspace, input=request,
                                            capture_output=True, text=True, timeout=8)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("result", json.loads(result.stdout.splitlines()[0]))
                    self.assertFalse(marker.exists(), "project package ran in host MCP")


if __name__ == "__main__":
    unittest.main()
