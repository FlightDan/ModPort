"""Portable diagnostic support to stage into a credential-free client sandbox.

No generated project code is executed by this module. Minecraft page actions
require a version-specific Java adapter; diagnostics never constitute evidence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re
from pathlib import PurePosixPath
from typing import Any

STATE_PREFIX = "MODPORT_CLIENT_STATE "
CLIENT_STAGES = ("startup", "resource_loading", "onboarding", "title", "world_creation", "world_ready", "characterization", "resource_reload", "finished")


def requires_client_display(contract: Mapping[str, Any], tasks: Sequence[str],
                            task_graph_output: str = "") -> bool:
    """Keep display provisioning independent of evidence validity.

    A Gradle Test task can depend on runClient even when the declaration which
    normally identifies client execution is incomplete. The actual dry-run
    graph and authored behavior sides still describe the workload in that case.
    """
    if any(task.split(":")[-1] == "runClient" for task in tasks):
        return True
    behaviors = contract.get("behaviors", ())
    if isinstance(behaviors, list) and any(
        isinstance(behavior, Mapping) and behavior.get("side") in {"client", "both"}
        for behavior in behaviors
    ):
        return True
    return re.search(r"^:(?:[A-Za-z0-9_.-]+:)*runClient SKIPPED[ \t]*$",
                     task_graph_output, flags=re.MULTILINE) is not None


def validate_client_state(record: Mapping[str, Any], *, execution_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("client state must be an object")
    if type(record.get("schema_version")) is not int or record.get("schema_version") != 1 or record.get("kind") != "client_diagnostic":
        raise ValueError("unsupported client diagnostic protocol")
    if record.get("stage") not in CLIENT_STAGES:
        raise ValueError("unknown client lifecycle stage")
    if not isinstance(record.get("execution_id"), str) or not record["execution_id"]:
        raise ValueError("missing client execution identity")
    if execution_id is not None and record["execution_id"] != execution_id:
        raise ValueError("stale client execution identity")
    for key in ("sequence", "elapsed_ms", "stage_elapsed_ms"):
        if type(record.get(key)) is not int or record[key] < 0:
            raise ValueError(f"invalid client diagnostic {key}")
    if record["stage_elapsed_ms"] > record["elapsed_ms"]:
        raise ValueError("stage elapsed time exceeds overall time")
    for key in ("screen", "overlay", "last_test", "error_code", "detail"):
        if not isinstance(record.get(key), str):
            raise ValueError(f"invalid client diagnostic {key}")
    for key in ("world_present", "player_present"):
        if type(record.get(key)) is not bool:
            raise ValueError(f"invalid client diagnostic {key}")
    if record["stage"] in {"world_ready", "characterization", "resource_reload"} and not (record["world_present"] and record["player_present"]):
        raise ValueError("world milestone without world and player")
    return dict(record)


def parse_client_states(log_text: str, *, execution_id: str | None = None) -> list[dict[str, Any]]:
    """Read exact standalone marker lines; reject stale/reordered observations."""
    states: list[dict[str, Any]] = []
    for line in log_text.splitlines():
        if not line.startswith(STATE_PREFIX):
            continue
        try:
            record = validate_client_state(json.loads(line[len(STATE_PREFIX):]), execution_id=execution_id)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed client diagnostic JSON") from exc
        if states:
            prior = states[-1]
            if record["execution_id"] != prior["execution_id"] or record["sequence"] <= prior["sequence"] or record["elapsed_ms"] < prior["elapsed_ms"]:
                raise ValueError("reordered or mixed client diagnostics")
        states.append(record)
    return states


def preflight_command(script_path: str, *, game_directory: str = ".modport/run-client") -> tuple[str, ...]:
    """Return argv for the sandbox executor (no shell, no host-side execution)."""
    path = PurePosixPath(script_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("preflight script must be sandbox-relative")
    return ("python3", "./" + str(path), "--game-directory=" + game_directory)


_PREFLIGHT = r'''"""Run only via the host credential-free sandbox executor."""
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


def probe(argv):
    if not shutil.which(argv[0]):
        return {"status": "unknown", "reason": "probe_tool_missing"}
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return {"status": "available" if result.returncode == 0 else "unavailable", "exit_code": result.returncode, "output": (result.stdout + result.stderr)[-8192:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "unknown", "reason": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-directory", required=True)
    args = parser.parse_args()
    directory = Path(args.game_directory)
    report = {"schema_version": 1, "kind": "client_environment_diagnostic",
              "java": probe(["java", "-version"]),
              "display": probe(["xdpyinfo"]) if os.environ.get("DISPLAY") else {"status": "unavailable", "reason": "DISPLAY_missing"},
              "opengl": probe(["glxinfo", "-B"]) if os.environ.get("DISPLAY") else {"status": "unknown", "reason": "display_missing"},
              "xvfb": {"status": "available" if shutil.which("Xvfb") else "unavailable", "started_by_preflight": False},
              "glfw": {"status": "unknown", "reason": "requires_actual_game_lwjgl_initialization"},
              "audio_devices": probe(["aplay", "-l"]),
              "audio_output_verified": False,
              "game_directory": {"path": str(directory), "exists": directory.is_dir(), "options_present": (directory / "options.txt").is_file()},
              "acceptance_evidence": False}
    if os.name == "nt":
        # Win32 display/audio availability is established by the actual game,
        # not Unix utility presence or a guessed desktop-session capability.
        report.update(
            display={"status": "unknown", "reason": "requires_actual_native_window_initialization"},
            opengl={"status": "unknown", "reason": "requires_actual_game_lwjgl_initialization"},
            xvfb={"status": "not_applicable", "started_by_preflight": False},
            audio_devices={"status": "unknown", "reason": "requires_actual_native_audio_initialization"},
            platform="windows")
    print("MODPORT_CLIENT_PREFLIGHT " + json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
'''

_JAVA = r'''package modport.harness;

import java.io.PrintStream;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;

/** Diagnostic-only recorder. Invoke on the client thread; host owns hard kill. */
public final class ClientDiagnostics {
    private static final String[] STAGES = {"startup", "resource_loading", "onboarding", "title", "world_creation", "world_ready", "characterization", "resource_reload", "finished"};
    private final String execution;
    private final PrintStream output;
    private final long started = System.nanoTime();
    private final long overallMs;
    private final Map<String, Long> deadlines;
    private long stageStarted = started;
    private long sequence;
    private String stage = "startup";
    private String lastTest = "";
    private boolean stopped;

    public ClientDiagnostics(String execution, PrintStream output, long overallMs, Map<String, Long> deadlines) {
        if (execution == null || execution.isEmpty() || overallMs <= 0) throw new IllegalArgumentException("execution and positive overall deadline required");
        this.execution = execution; this.output = output; this.overallMs = overallMs;
        this.deadlines = new HashMap<>(deadlines);
        for (String name : STAGES) if (!this.deadlines.containsKey(name) || this.deadlines.get(name) == null || this.deadlines.get(name) <= 0) throw new IllegalArgumentException("positive deadline required: " + name);
    }
    public void enter(String next) {
        if (!Arrays.asList(STAGES).contains(next)) throw new IllegalArgumentException("unknown stage");
        if (!stage.equals(next)) { stage = next; stageStarted = System.nanoTime(); }
    }
    /** Call only AFTER the separate evidence writer flushes a genuine test record. */
    public void completedTest(String test) { lastTest = test; }
    public boolean stopped() { return stopped; }
    public void observe(String screen, String overlay, boolean world, boolean player) {
        if (stopped) return;
        long now = System.nanoTime();
        long elapsed = (now - started) / 1000000L;
        long stageElapsed = (now - stageStarted) / 1000000L;
        String error = elapsed >= overallMs ? "overall_timeout" : stageElapsed >= deadlines.get(stage) ? "stage_timeout" : "";
        emit(screen, overlay, world, player, error, "", elapsed, stageElapsed);
        if (!error.isEmpty()) stopped = true;
    }
    public void fail(String error, String detail, String screen, String overlay, boolean world, boolean player) {
        if (stopped) return;
        long now = System.nanoTime();
        emit(screen, overlay, world, player, error, detail, (now-started)/1000000L, (now-stageStarted)/1000000L);
        stopped = true;
    }
    private void emit(String screen, String overlay, boolean world, boolean player, String error, String detail, long elapsed, long stageElapsed) {
        output.println("MODPORT_CLIENT_STATE {\"schema_version\":1,\"kind\":\"client_diagnostic\",\"execution_id\":"+q(execution)+",\"sequence\":"+(sequence++)+",\"elapsed_ms\":"+elapsed+",\"stage_elapsed_ms\":"+stageElapsed+",\"stage\":"+q(stage)+",\"screen\":"+q(screen)+",\"overlay\":"+q(overlay)+",\"world_present\":"+world+",\"player_present\":"+player+",\"last_test\":"+q(lastTest)+",\"error_code\":"+q(error)+",\"detail\":"+q(detail)+"}");
        output.flush();
    }
    private static String q(String value) {
        if (value == null) value = "";
        StringBuilder b = new StringBuilder("\"");
        for (int i=0; i<value.length(); i++) {
            char c=value.charAt(i);
            if (c=='"' || c=='\\') b.append('\\').append(c);
            else if (c<32) b.append(String.format("\\u%04x", (int)c));
            else b.append(c);
        }
        return b.append('"').toString();
    }
}
'''

_ADAPTER = r'''package modport.harness;

import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.AccessibilityOnboardingScreen;
import net.minecraft.client.gui.screens.TitleScreen;

/** Mojmap 1.20.1 onboarding fragment; call from the client thread before world creation.
 * Compile against the locked target. This fragment is NOT a full acceptance harness.
 */
public final class Minecraft1201Startup {
    public static boolean reachTitle(Minecraft mc, ClientDiagnostics diagnostics) {
        if (diagnostics.stopped()) return false;
        String screen = mc.screen == null ? "" : mc.screen.getClass().getName();
        String overlay = mc.getOverlay() == null ? "" : mc.getOverlay().getClass().getName();
        if (mc.getOverlay() != null) {
            diagnostics.enter("resource_loading");
        } else if (mc.screen instanceof AccessibilityOnboardingScreen) {
            diagnostics.enter("onboarding");
            diagnostics.observe(screen, overlay, mc.level != null, mc.player != null);
            if (diagnostics.stopped()) return false;
            ((AccessibilityOnboardingScreen) mc.screen).onClose();
            if (!(mc.screen instanceof TitleScreen) || mc.options.onboardAccessibility) {
                diagnostics.fail("onboarding_transition_failed", "normal close did not reach title and clear option", mc.screen == null ? "" : mc.screen.getClass().getName(), "", false, false);
            }
            return false;
        } else if (mc.screen instanceof TitleScreen) {
            diagnostics.enter("title");
            diagnostics.observe(screen, overlay, mc.level != null, mc.player != null);
            return !diagnostics.stopped();
        } else if (mc.screen != null) {
            diagnostics.fail("unknown_screen", "no authorized startup action for this screen", screen, overlay, mc.level != null, mc.player != null);
            return false;
        }
        diagnostics.observe(screen, overlay, mc.level != null, mc.player != null);
        return false;
    }
}
'''

_PROTOCOL = '''# Client harness diagnostic support (revision 1)

Run launch.py only through the host credential-free sandbox:
python3 /modport-support/launch.py --timeout 900 -- <Gradle argv...>
Optional --display-timeout defaults to 10 seconds; --game-directory defaults to
.modport/run-client. The wrapper preserves an existing DISPLAY. Otherwise it starts
its own Xvfb with -displayfd and -nolisten tcp, creates a private XDG runtime dir,
and runs preflight.py in that same environment before launching the workload.
It cleans only child process groups that it creates. Workload exit codes propagate;
environment failure is 69, launcher failure 70, deadline 124, SIGTERM 143, SIGINT 130.
MODPORT_CLIENT_PREFLIGHT reports include MODPORT_EXECUTION_ID from the sandbox.
The host must maintain its own hard deadline outside the wrapper.

Run preflight.py only through the host sandbox, before the Gradle client task.
No shell is needed. It does not create/delete displays or alter game settings.
A missing DISPLAY before harness startup is not a fatal verdict: the harness may
start its own Xvfb. The report separately notes Xvfb availability, without starting it.
On Windows, the host mounts its native Python runtime read-only and launches
this wrapper inside the credential-free AppContainer Job. The wrapper uses
native Gradle/JDK argv and inherits that containment for every child. It does
not require Xvfb or DISPLAY. Native window, OpenGL and audio initialization
remain unknown until the actual game reports observations; the host verifies
Job descendant cleanup after the wrapper exits.
Probe absence means unknown capability; audio availability never proves sound
correctness. glxinfo is a GLX observation, not actual Minecraft GLFW acceptance.

Compile ClientDiagnostics.java with the generated test harness. Supply the host
execution_id (MODPORT_EXECUTION_ID, injected by host) explicitly; do not substitute
an old nonce. Supply positive millisecond deadlines for every CLIENT_STAGES entry
and an overall deadline below the host hard timeout. Call observe on each lifecycle
transition and periodically on client ticks. A stopped recorder requires the caller
to stop test work and request orderly termination. The host must still enforce a
hard deadline because blocked client threads cannot report their own timeouts.

Minecraft1201Startup.java is a Mojmap 1.20.1 adapter fragment based on the frozen
ScalingHealth characterization source. It handles the actual onboarding onClose
API and already-onboarded TitleScreen path without writing options directly.
Compile and verify against locked versions before use; other versions require a
separate typed adapter. This is not a generic Python screen automation mechanism.
Do not call the startup fragment after initiating world creation. The remaining
harness must create a nonce-specific isolated world, wait for actual level AND
player, emit world_ready, run assertions with incrementally flushed evidence, and
observe resource_reload completion. Record failed futures as resource_reload_failed.
Unknown screens are terminal diagnostics; never close them automatically.

Each System.out line begins exactly MODPORT_CLIENT_STATE followed by JSON. Fields:
schema_version=1, kind=client_diagnostic, execution_id, strictly increasing sequence,
elapsed_ms and stage_elapsed_ms (nonnegative integers), stage, screen, overlay,
world_present and player_present (booleans), last_test, error_code, detail (strings).
Stages: startup, resource_loading, onboarding, title, world_creation, world_ready,
characterization, resource_reload, finished. Optional onboarding may be skipped.
The host validates identity/order; the messages remain untrusted diagnostic data.
They cannot close obligations, authenticate tests, or substitute for runtime witness
records. Flush actual evidence after EACH passed test under the CURRENT nonce,
then call completedTest. Preserve successful partial evidence on failure but do
not reuse it as fresh evidence in a subsequent execution. Screenshots, when the
client responds, are separate artifacts; an unresponsive client needs host logs.

Required real sandbox regressions before claiming client acceptance: fresh options,
already-onboarded options, unknown page, absent display, no audio device, failed
resource reload, stage/overall timeout, cancellation. Parser and Java fixture tests
alone do not demonstrate any Minecraft runtime acceptance.
'''


_LAUNCH = r'''"""Credential-free sandbox launch wrapper. Never run migrated code on the host."""
import argparse
import math
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time

PREFIX = "MODPORT_CLIENT_PREFLIGHT "
owned = []
cancelled = 0


def interrupted(signum, frame):
    global cancelled
    cancelled = signum


def spawn(argv, **kwargs):
    if os.name == "nt":
        # Children retain the outer AppContainer token and host-owned Job.
        # The host verifies Job cleanup after this wrapper exits.
        process = subprocess.Popen(argv, **kwargs)
    else:
        process = subprocess.Popen(argv, start_new_session=True, **kwargs)
    owned.append(process)
    return process


def stop_owned():
    if os.name == "nt":
        for process in reversed(owned):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        # This cleans immediate children; descendant cleanup is verified by
        # the outer host AppContainer Job, never inferred from these waits.
        return
    # Every group was created by this wrapper with start_new_session=True.
    # Never signal the parent sandbox group or any existing display process.
    for process in reversed(owned):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(p.poll() is None for p in owned):
        time.sleep(0.02)
    for process in reversed(owned):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def report(error, environment=None):
    value = environment or {"schema_version": 1, "kind": "client_environment_diagnostic"}
    value.update(execution_id=os.environ.get("MODPORT_EXECUTION_ID", ""),
                 error_code=error, acceptance_evidence=False)
    print(PREFIX + json.dumps(value, sort_keys=True), flush=True)


def wait_for(process, deadline):
    while process.poll() is None:
        if cancelled or time.monotonic() >= deadline:
            return None
        time.sleep(0.02)
    return process.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--display-timeout", type=float, default=10)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--game-directory", default=".modport/run-client")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not math.isfinite(args.timeout) or not math.isfinite(args.display_timeout) or args.timeout <= 0 or args.display_timeout <= 0:
        parser.error("command and positive deadlines required")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    deadline = time.monotonic() + args.timeout
    environment = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="modport-xdg-") as runtime:
        os.chmod(runtime, 0o700)
        environment["XDG_RUNTIME_DIR"] = runtime
        try:
            if os.name != "nt" and not environment.get("DISPLAY", "").strip():
                executable = shutil.which("Xvfb")
                if executable is None:
                    report("xvfb_unavailable")
                    return 69
                read_fd, write_fd = os.pipe()
                try:
                    display = spawn([executable, "-displayfd", str(write_fd), "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
                                    env=environment, pass_fds=(write_fd,), stdout=subprocess.DEVNULL)
                finally:
                    os.close(write_fd)
                number = b""
                display_deadline = min(deadline, time.monotonic() + args.display_timeout)
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(read_fd, selectors.EVENT_READ)
                        while b"\n" not in number and len(number) < 32 and not cancelled and time.monotonic() < display_deadline:
                            if display.poll() is not None:
                                break
                            if selector.select(0.05):
                                chunk = os.read(read_fd, 32)
                                if not chunk:
                                    break
                                number += chunk
                finally:
                    os.close(read_fd)
                if cancelled:
                    report("execution_cancelled")
                    return 128 + cancelled
                if not number.endswith(b"\n") or not number.strip().isdigit() or display.poll() is not None:
                    report("display_start_failed")
                    return 69
                environment["DISPLAY"] = ":" + number.strip().decode("ascii")
            # File capture avoids a stdout pipe deadlock and keeps probes under
            # the same process-group cancellation and overall deadline.
            with tempfile.TemporaryFile(mode="w+") as output:
                preflight = spawn([sys.executable, str(Path(__file__).with_name("preflight.py")),
                                   "--game-directory=" + args.game_directory], env=environment,
                                  stdout=output, stderr=subprocess.STDOUT)
                result = wait_for(preflight, min(deadline, time.monotonic() + 45))
                if result is None:
                    report("execution_cancelled" if cancelled else "preflight_timeout")
                    return 128 + cancelled if cancelled else 124
                output.seek(0)
                lines = output.read().splitlines()
                reports = [json.loads(line[len(PREFIX):]) for line in lines if line.startswith(PREFIX)]
                if result or len(reports) != 1:
                    report("preflight_failed")
                    return 70
                observed = reports[0]
                observed["display_owned"] = os.name != "nt" and len(owned) > 1
                observed["xdg_runtime_private"] = True
                if os.name == "nt":
                    observed["process_cleanup_scope"] = "outer_host_appcontainer_job"
                if observed.get("display", {}).get("status") == "unavailable":
                    report("display_unavailable", observed)
                    return 69
                report("", observed)
            if cancelled:
                return 128 + cancelled
            if time.monotonic() >= deadline:
                return 124
            workload = spawn(command, env=environment)
            result = wait_for(workload, deadline)
            if result is None:
                report("execution_cancelled" if cancelled else "workload_timeout", observed)
                return 128 + cancelled if cancelled else 124
            return result if result >= 0 else 128 - result
        except (OSError, ValueError) as exc:
            report("launch_failed", {"schema_version": 1, "kind": "client_environment_diagnostic", "detail": type(exc).__name__})
            return 70
        finally:
            stop_owned()


if __name__ == "__main__":
    sys.exit(main())
'''


def parse_environment_report(log_text: str, execution_id: str | None = None) -> dict[str, Any] | None:
    """Return the latest identity-bound diagnostic, never acceptance evidence."""
    result = None
    for line in log_text.splitlines():
        if not line.startswith("MODPORT_CLIENT_PREFLIGHT "):
            continue
        try:
            value = json.loads(line[len("MODPORT_CLIENT_PREFLIGHT "):])
        except json.JSONDecodeError as exc:
            raise ValueError("malformed environment diagnostic") from exc
        if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("kind") != "client_environment_diagnostic" or value.get("acceptance_evidence") is not False:
            raise ValueError("invalid environment diagnostic")
        if execution_id is not None and value.get("execution_id") != execution_id:
            raise ValueError("stale environment diagnostic")
        result = value
    return result


def client_harness_support_files(*, workflow_version: int = 0) -> dict[str, str]:
    """Relative paths and trusted text for host staging or an agent support bundle."""
    files = {
        "preflight.py": _PREFLIGHT,
        "launch.py": _LAUNCH,
        "modport/harness/ClientDiagnostics.java": _JAVA,
        "modport/harness/Minecraft1201Startup.java": _ADAPTER,
        "PROTOCOL.md": _PROTOCOL,
    }
    if workflow_version >= 34:
        from .target_session import target_session_support_files
        files.update(target_session_support_files())
    return files
