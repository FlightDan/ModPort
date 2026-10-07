import json
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import unittest

from modport.client_harness import CLIENT_STAGES, STATE_PREFIX, client_harness_support_files, parse_client_states, preflight_command, validate_client_state
from modport.diagnostics import classify_characterization_failure


def state(**updates):
    return {"schema_version": 1, "kind": "client_diagnostic", "execution_id": "exec-1", "sequence": 0, "elapsed_ms": 0, "stage_elapsed_ms": 0, "stage": "startup", "screen": "", "overlay": "", "world_present": False, "player_present": False, "last_test": "", "error_code": "", "detail": "", **updates}


class ClientHarnessTests(unittest.TestCase):
    def test_strict_diagnostic_identity_order_and_world_state(self):
        log = STATE_PREFIX + json.dumps(state())
        self.assertEqual(len(parse_client_states(log, execution_id="exec-1")), 1)
        for bad in (state(execution_id="old"), state(sequence=True), state(stage="world_ready"), state(kind="runtime_evidence")):
            with self.assertRaises(ValueError):
                validate_client_state(bad, execution_id="exec-1")
        with self.assertRaises(ValueError):
            parse_client_states(log + '\n' + log)
        with self.assertRaises(ValueError):
            parse_client_states(STATE_PREFIX + '[]')

    def test_phase_timeout_and_unknown_screen(self):
        for error in ("stage_timeout", "unknown_screen", "resource_reload_failed"):
            log = STATE_PREFIX + json.dumps(state(stage="onboarding", error_code=error))
            result = classify_characterization_failure(log, phase="baseline", exit_code=1, timed_out=False, execution_id="exec-1")
            self.assertEqual(result["error_code"], error)
            self.assertEqual(result["last_milestone"], "onboarding")
            self.assertEqual(result["evidence_counts"]["authenticated"], 0)

    def test_preflight_is_argv_and_support_is_self_contained(self):
        command = preflight_command("support/preflight.py", game_directory="game;touch /tmp/not-executed")
        self.assertEqual(command[-1], "--game-directory=game;touch /tmp/not-executed")
        self.assertEqual(preflight_command("-c")[1], "./-c")
        with self.assertRaises(ValueError):
            preflight_command("../escape.py")
        files = client_harness_support_files()
        compile(files["preflight.py"], "preflight.py", "exec")
        self.assertIn("onClose()", files["modport/harness/Minecraft1201Startup.java"])

    def test_preflight_missing_display_and_existing_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "preflight.py"
            script.write_text(client_harness_support_files()["preflight.py"])
            game = root / "existing game"
            game.mkdir()
            options = game / "options.txt"
            options.write_text("onboardAccessibility:false\n")
            result = subprocess.run([sys.executable, str(script), "--game-directory", str(game)],
                                    env={"PATH": ""}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout.removeprefix("MODPORT_CLIENT_PREFLIGHT "))
            self.assertEqual(record["display"]["status"], "unavailable")
            self.assertEqual(record["audio_devices"]["status"], "unknown")
            self.assertTrue(record["game_directory"]["options_present"])
            self.assertFalse(record["acceptance_evidence"])
            self.assertEqual(options.read_text(), "onboardAccessibility:false\n")

    @unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "Java compiler/runtime unavailable for support fixture")
    def test_real_java_recorder_and_typed_adapter_fixture(self):
        # These are small API doubles, not Minecraft acceptance tests or project code.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = client_harness_support_files()
            sources = {key: value for key, value in files.items() if key.endswith('.java')}
            sources.update({
                'net/minecraft/client/gui/screens/Screen.java': 'package net.minecraft.client.gui.screens; public class Screen {}',
                'net/minecraft/client/gui/screens/TitleScreen.java': 'package net.minecraft.client.gui.screens; public class TitleScreen extends Screen {}',
                'net/minecraft/client/gui/screens/AccessibilityOnboardingScreen.java': 'package net.minecraft.client.gui.screens; import net.minecraft.client.Minecraft; public class AccessibilityOnboardingScreen extends Screen { private final Minecraft mc; public AccessibilityOnboardingScreen(Minecraft mc) {this.mc=mc;} public void onClose() {mc.screen=new TitleScreen();mc.options.onboardAccessibility=false;} }',
                'net/minecraft/client/Minecraft.java': 'package net.minecraft.client; import net.minecraft.client.gui.screens.Screen; public class Minecraft {public Screen screen; public Object level,player; public Options options=new Options(); public Object getOverlay(){return null;} public static class Options {public boolean onboardAccessibility=true;}}',
                'Fixture.java': '''import modport.harness.*; import net.minecraft.client.*; import net.minecraft.client.gui.screens.*; import java.util.*;
public class Fixture {
 static ClientDiagnostics recorder(long deadline) {
  Map<String,Long> limits=new HashMap<>();
  for(String s: new String[]{STAGES}) limits.put(s,deadline);
  return new ClientDiagnostics("exec-1",System.out,10000,limits);
 }
 public static void main(String[] args) throws Exception {
  Minecraft mc=new Minecraft(); ClientDiagnostics d=recorder(10000);
  mc.screen=new AccessibilityOnboardingScreen(mc);
  if(Minecraft1201Startup.reachTitle(mc,d)) throw new AssertionError();
  if(!Minecraft1201Startup.reachTitle(mc,d)||mc.options.onboardAccessibility) throw new AssertionError();
  mc.screen=new Screen(); Minecraft1201Startup.reachTitle(mc,d);
  if(!d.stopped()||!(mc.screen instanceof Screen)) throw new AssertionError();
  ClientDiagnostics existing=recorder(10000); mc.screen=new TitleScreen(); mc.options.onboardAccessibility=false;
  if(!Minecraft1201Startup.reachTitle(mc,existing)) throw new AssertionError();
  ClientDiagnostics timeout=recorder(1); Thread.sleep(5); timeout.observe("", "", false, false);
  if(!timeout.stopped()) throw new AssertionError();
 }
}'''.replace('STAGES', ','.join(json.dumps(s) for s in CLIENT_STAGES))})
            for name, source in sources.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
            built = subprocess.run(['javac', *sources], cwd=root, capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stderr)
            result = subprocess.run(['java', '-cp', str(root), 'Fixture'], cwd=root, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [validate_client_state(json.loads(line[len(STATE_PREFIX):])) for line in result.stdout.splitlines()]
            self.assertEqual([r['stage'] for r in records[:2]], ['onboarding', 'title'])
            self.assertEqual(records[2]['error_code'], 'unknown_screen')
            self.assertEqual(records[-1]['error_code'], 'stage_timeout')


class ClientLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('launch.py', 'preflight.py'):
            (self.root / name).write_text(client_harness_support_files()[name])
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.environment = {'PATH': str(self.bin), 'MODPORT_EXECUTION_ID': 'launch-1', 'FAKE_PID': str(self.root / 'display.pid')}

    def fake_display(self):
        source = '''import os,sys,time
open(os.environ['FAKE_PID'],'w').write(str(os.getpid()))
fd=int(sys.argv[sys.argv.index('-displayfd')+1])
os.write(fd,b'77\\n');os.close(fd)
while True: time.sleep(1)
'''
        path = self.bin / 'Xvfb'
        path.write_text('#!' + sys.executable + '\n' + source)
        path.chmod(0o755)

    def argv(self, code, *options):
        return [sys.executable, str(self.root / 'launch.py'), *options, '--', sys.executable, '-c', code]

    def run_wrapper(self, code, *options):
        return subprocess.run(self.argv(code, *options), env=self.environment, capture_output=True, text=True, timeout=10)

    def test_existing_display_and_failure_propagation(self):
        from modport.client_harness import parse_environment_report
        self.environment['DISPLAY'] = ':4321'
        existing_display = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        def clean_existing():
            existing_display.terminate()
            existing_display.wait(timeout=5)
        self.addCleanup(clean_existing)
        result = self.run_wrapper('import os,sys; assert os.environ["DISPLAY"]==":4321"; assert os.stat(os.environ["XDG_RUNTIME_DIR"]).st_mode & 0o777 == 0o700; sys.exit(7)')
        self.assertEqual(result.returncode, 7, result.stderr)
        value = parse_environment_report(result.stdout, 'launch-1')
        self.assertFalse(value['display_owned'])
        self.assertTrue(value['xdg_runtime_private'])
        self.assertFalse((self.root / 'display.pid').exists())
        self.assertIsNone(existing_display.poll())

    def test_missing_xvfb_is_environment_error(self):
        from modport.client_harness import parse_environment_report
        result = self.run_wrapper('raise AssertionError("must not execute")')
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertEqual(parse_environment_report(result.stdout, 'launch-1')['error_code'], 'xvfb_unavailable')

    def assert_display_gone(self):
        import os
        pid = int((self.root / 'display.pid').read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_owned_display_cleanup_and_diagnostic(self):
        from modport.client_harness import parse_environment_report
        self.fake_display()
        result = self.run_wrapper('import os; assert os.environ["DISPLAY"]==":77"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(parse_environment_report(result.stdout, 'launch-1')['display_owned'])
        self.assert_display_gone()

    def test_timeout_and_signal_cleanup(self):
        import signal
        import time
        self.fake_display()
        result = self.run_wrapper('import time; time.sleep(60)', '--timeout', '0.4')
        self.assertEqual(result.returncode, 124, result.stderr)
        self.assert_display_gone()
        (self.root / 'display.pid').unlink()
        workload_pid = self.root / 'workload.pid'
        process = subprocess.Popen(self.argv('import os,time; open(' + repr(str(workload_pid)) + ', "w").write(str(os.getpid())); time.sleep(60)'), env=self.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not workload_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(workload_pid.exists())
        process.send_signal(signal.SIGTERM)
        output, error = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 143, error + output)
        self.assert_display_gone()
        import os
        with self.assertRaises(ProcessLookupError):
            os.kill(int(workload_pid.read_text()), 0)

    def test_environment_parser_rejects_stale_or_evidence(self):
        from modport.client_harness import parse_environment_report
        for value in ({'schema_version': 1, 'kind': 'client_environment_diagnostic', 'execution_id': 'old', 'acceptance_evidence': False}, {'schema_version': 1, 'kind': 'client_environment_diagnostic', 'execution_id': 'launch-1', 'acceptance_evidence': True}):
            with self.assertRaises(ValueError):
                parse_environment_report('MODPORT_CLIENT_PREFLIGHT ' + json.dumps(value), 'launch-1')
