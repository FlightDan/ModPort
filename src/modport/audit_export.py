"""Run report generation in a separate, memory-limited process."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


DEFAULT_MEMORY_MIB = 768
DEFAULT_TIMEOUT_SECONDS = 600


class AuditExportError(ValueError):
    pass


def export_isolated(root, outdir=None, *, memory_mib=DEFAULT_MEMORY_MIB,
                    timeout=DEFAULT_TIMEOUT_SECONDS):
    if type(memory_mib) is not int or memory_mib < 128:
        raise ValueError("audit memory limit must be at least 128 MiB")
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("audit timeout must be positive")
    root = Path(root).resolve()
    destination = Path(outdir).resolve() if outdir else root / "audit-report"
    with tempfile.TemporaryDirectory(prefix="modport-audit-export-") as temporary:
        receipt = Path(temporary) / "result.json"
        command = [sys.executable, "-I", str(Path(__file__).resolve()), "--run-dir", str(root),
                   "--output-dir", str(destination), "--memory-mib", str(memory_mib),
                   "--result-path", str(receipt)]
        # Report generation needs no provider credentials. Select this exact
        # deployed package rather than a potentially older installed ModPort.
        environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL")
                       if key in os.environ}
        environment["PYTHONNOUSERSITE"] = "1"
        if os.name == "nt":
            from .platform_runtime import capture_process
            for name in ("SystemRoot", "WINDIR"):
                if name in os.environ:
                    environment[name] = os.environ[name]
            result = capture_process(command, cwd=Path(__file__).resolve().parent.parent,
                environment=environment, timeout=timeout, max_output_bytes=2048,
                memory_limit_bytes=memory_mib * 1024 * 1024)
            if result.timed_out:
                raise AuditExportError("audit export exceeded its time limit")
            if result.returncode != 0 or result.drain_incomplete:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise AuditExportError(
                    f"audit export exited {result.returncode} within a {memory_mib} MiB "
                    f"memory limit: {detail}")
            value = json.loads(receipt.read_text(encoding="utf-8"))
            return {key: Path(path) for key, path in value["paths"].items()}
        with (Path(temporary) / "stderr").open("w+b") as stderr:
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=stderr, env=environment,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    timeout=timeout, check=False, close_fds=True)
            except subprocess.TimeoutExpired as error:
                raise AuditExportError("audit export exceeded its time limit") from error
            if result.returncode != 0:
                stderr.seek(0, os.SEEK_END)
                stderr.seek(max(0, stderr.tell() - 2048))
                detail = stderr.read(2048).decode("utf-8", errors="replace").strip()
                raise AuditExportError(
                    f"audit export exited {result.returncode} within a {memory_mib} MiB "
                    f"memory limit: {detail}")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        return {key: Path(path) for key, path in value["paths"].items()}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--memory-mib", required=True, type=int)
    parser.add_argument("--result-path", required=True)
    args = parser.parse_args(argv)
    ceiling = args.memory_mib * 1024 * 1024
    if args.memory_mib < 128:
        raise ValueError("audit memory limit must be at least 128 MiB")
    # The isolated interpreter loads only this trusted package, after its
    # address-space ceiling is installed and before any audit data is opened.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if os.name == "nt":
        from modport.platform_runtime import require_windows_job_memory_limit
        ceiling = require_windows_job_memory_limit(ceiling)
    else:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        for inherited in (soft, hard):
            if inherited != resource.RLIM_INFINITY:
                ceiling = min(ceiling, inherited)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    from modport.telemetry import export_report
    paths = export_report(args.run_dir, args.output_dir)
    Path(args.result_path).write_text(json.dumps({
        "paths": {key: str(path) for key, path in paths.items()},
        "memory_limit_bytes": ceiling,
    }) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
