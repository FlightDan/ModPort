"""Execution-bound evidence for the host's native verification interfaces."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
import tempfile

from .contract_inputs import validate_baseline_gradle_tasks
from .evidence import seal_ref
from .goal_planning import relative_path, validate_goal
from .goal_validation import _check, contained_file
from .handlers import BaselineContractVerificationHandler, ClientSmokeHandler
from .repair_evidence import snapshot_repair_evidence


_EXECUTION_ID = re.compile(r"[A-Za-z0-9_.:-]+")
_SOURCE_PATHS = (
    "src/modport/goal_validation.py",
    "src/modport/goal_planning.py",
    "src/modport/contract_inputs.py",
    "src/modport/handlers.py",
)


def _interface_text(execution_id: str) -> str:
    sections = (
        ("src/modport/goal_validation.py:contained_file", contained_file),
        ("src/modport/goal_validation.py:_check", _check),
        ("src/modport/goal_planning.py:relative_path", relative_path),
        ("src/modport/goal_planning.py:validate_goal", validate_goal),
        ("src/modport/contract_inputs.py:validate_baseline_gradle_tasks",
         validate_baseline_gradle_tasks),
        ("src/modport/handlers.py:BaselineContractVerificationHandler",
         BaselineContractVerificationHandler),
        ("src/modport/handlers.py:ClientSmokeHandler", ClientSmokeHandler),
    )
    introduction = (
        "Current deployed host verification interface. Custom handler overrides may differ.\n"
        f"Execution ID: {execution_id}\n"
        "This is host API/source evidence, not successful project verification.\n"
        "For a native goal check whose type is gradle_regression, the host automatically "
        "adds /workspace/.modport/characterization.init.gradle with --init-script when "
        ".modport/characterization.init.gradle is present in the fresh candidate snapshot.\n"
        "The tasks field contains Gradle task names only. Tasks are not flags, paths, init-script "
        "arguments, or command fragments; host options must not be repeated there.\n"
        "The imported deployed Python objects below are rendered with inspect.getsource. "
        "They define the exact task DSL, report/path checks, sandbox launch, execution nonce, "
        "and client-launch behavior available to this execution.\n"
    )
    return introduction + "".join(
        f"\n===== BEGIN {label} =====\n{inspect.getsource(source)}"
        f"===== END {label} =====\n"
        for label, source in sections
    )


def publish_host_interface(root, execution_id):
    """Publish and content-snapshot the deployed host interface for one execution.

    The execution-local source artifact is write-once. Replaying the same
    publication is allowed only when its bytes are identical; a changed source
    body under an existing execution identity fails closed.
    """
    if (not isinstance(execution_id, str)
            or _EXECUTION_ID.fullmatch(execution_id) is None
            or execution_id in {".", ".."}):
        raise ValueError("host interface requires a safe nonempty execution ID")
    root = Path(root).absolute()
    if not root.is_dir() or root.is_symlink() or root.resolve() != root:
        raise ValueError("host interface root must be a contained real directory")
    relative = Path("artifacts") / "executions" / execution_id / "host-interface.txt"
    target = root / relative
    parent = root
    for part in relative.parent.parts:
        parent = parent / part
        if parent.exists() or parent.is_symlink():
            if (parent.is_symlink() or not parent.is_dir()
                    or parent.resolve() != parent.absolute()):
                raise ValueError("unsafe host interface support path")
        else:
            parent.mkdir()
    if (target.parent.is_symlink() or target.parent.resolve() != target.parent.absolute()
            or not target.parent.resolve().is_relative_to(root)):
        raise ValueError("unsafe host interface support path")

    data = _interface_text(execution_id).encode("utf-8")
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".host-interface-")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if (target.is_symlink() or not target.is_file()
                    or target.resolve() != target.absolute()
                    or target.read_bytes() != data):
                raise ValueError("host interface artifact already differs for this execution")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)

    ref = seal_ref(root, {
        "path": relative.as_posix(),
        "media_type": "text/plain",
        "metadata": {
            "source_path": relative.as_posix(),
            "source_paths": list(_SOURCE_PATHS),
            "source_capture": "inspect.getsource",
        },
    }, execution_id=execution_id)
    return snapshot_repair_evidence(root, ref)
