import ast
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "prusa_connect_rtsp/main.py"
RUN = ROOT / "prusa_connect_rtsp/run.sh"


def load_camera(mqtt_v2=True):
    tree = ast.parse(MAIN.read_text())
    bootstrap = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "frame_count"
            for target in node.targets
        ):
            break
        bootstrap.append(node)
    cv2 = MagicMock()
    cv2.VideoCapture.return_value.read.return_value = (True, MagicMock())
    cv2.imencode.return_value = (True, MagicMock())
    requests = MagicMock()
    requests.RequestException = ConnectionError
    mqtt = types.ModuleType("paho.mqtt.client")
    mqtt.Client = MagicMock()
    if mqtt_v2:
        mqtt.CallbackAPIVersion = types.SimpleNamespace(VERSION1=1)
    paho = types.ModuleType("paho")
    paho.__path__ = []
    package = types.ModuleType("paho.mqtt")
    package.__path__ = []
    package.client = mqtt
    paho.mqtt = package
    modules = {
        "cv2": cv2, "requests": requests, "paho": paho,
        "paho.mqtt": package, "paho.mqtt.client": mqtt,
    }
    env = {
        "RTSP_URL": "rtsp://camera/stream", "TOKEN": "test-token",
        "FINGERPRINT": "a" * 40, "MQTT_HOST": "broker",
    }
    namespace = {}
    with patch.dict(sys.modules, modules), patch.dict(os.environ, env, clear=True):
        with patch("signal.signal"), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(ast.Module(body=bootstrap, type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace, cv2, requests, mqtt


class CameraTests(unittest.TestCase):
    def test_mqtt_versions(self):
        for version in (True, False):
            with self.subTest(v2=version):
                _, _, _, mqtt = load_camera(version)
                if version:
                    mqtt.Client.assert_called_once_with(callback_api_version=1)
                else:
                    mqtt.Client.assert_called_once_with()

    def test_name_registration_and_failure_logging(self):
        ns, _, requests, _ = load_camera()
        requests.put.return_value.status_code = 204
        ns["set_prusa_camera_name"]("Printer 1")
        requests.put.assert_called_once_with(
            "https://connect.prusa3d.com/c/info",
            json={"config": {"name": "Printer 1"}},
            headers={"fingerprint": "a" * 40, "token": "test-token"},
            timeout=15,
        )
        requests.put.side_effect = ConnectionError("offline")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ns["set_prusa_camera_name"]("Printer 1")
        self.assertIn("Could not register camera name: offline", output.getvalue())
        requests.put.side_effect = None
        requests.put.return_value.status_code = 401
        with contextlib.redirect_stdout(output):
            ns["set_prusa_camera_name"]("Printer 1")
        self.assertIn("HTTP 401", output.getvalue())

    def test_snapshot_endpoint_and_ffmpeg_preserved(self):
        ns, cv2, requests, _ = load_camera()
        ns["capture_frame_from_camera"]()
        cv2.VideoCapture.assert_called_once_with(
            "rtsp://camera/stream", cv2.CAP_FFMPEG,
            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000,
             cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
        )
        ns["send_frame_to_prusa"](b"jpg")
        self.assertEqual(
            requests.Session.return_value.put.call_args.args[0],
            "https://connect.prusa3d.com/c/snapshot",
        )

    def run_loop(self, ns):
        tree = ast.parse(MAIN.read_text())
        start = next(i for i, node in enumerate(tree.body) if
                     isinstance(node, ast.Assign) and any(
                         isinstance(t, ast.Name) and t.id == "frame_count" for t in node.targets))
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(ast.Module(body=tree.body[start:], type_ignores=[]), str(MAIN), "exec"), ns)

    def test_startup_retries_indefinitely_and_stops(self):
        ns, _, _, _ = load_camera()
        ns["capture_frame_from_camera"] = MagicMock(return_value=None)
        event = MagicMock()
        event.is_set.side_effect = lambda: event.wait.call_count >= 15
        ns["shutdown_requested"] = event
        self.run_loop(ns)
        self.assertEqual(ns["capture_frame_from_camera"].call_count, 15)
        self.assertEqual([c.args[0] for c in event.wait.call_args_list],
                         [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 60, 60, 60])
        ns["mqtt_client"].disconnect.assert_called_once()

    def test_upload_backoff_and_recovery_preserved(self):
        ns, _, _, _ = load_camera()
        ns["capture_frame_from_camera"] = MagicMock()
        ns["set_prusa_camera_name"] = MagicMock()
        ns["send_frame_to_prusa"] = MagicMock(side_effect=[
            (True, 401, "offline"), (True, 401, "offline"),
            (True, 401, "offline"), (True, 401, "offline"),
            (True, 200, "ok"),
        ])
        event = MagicMock()
        event.is_set.side_effect = lambda: event.wait.call_count >= 5
        ns["shutdown_requested"] = event
        self.run_loop(ns)
        self.assertEqual([c.args[0] for c in event.wait.call_args_list], [5, 5, 10, 20, 5])
        self.assertFalse(ns["backoff_active"])

    def test_upload_backoff_is_capped(self):
        ns, _, _, _ = load_camera()
        ns["capture_frame_from_camera"] = MagicMock()
        ns["set_prusa_camera_name"] = MagicMock()
        ns["send_frame_to_prusa"] = MagicMock(return_value=(True, 401, "offline"))
        event = MagicMock()
        event.is_set.side_effect = lambda: event.wait.call_count >= 15
        ns["shutdown_requested"] = event
        self.run_loop(ns)
        delays = [c.args[0] for c in event.wait.call_args_list]
        self.assertEqual(max(delays), 300)
        self.assertEqual(delays[-1], 300)

    def test_signal_interrupts_wait(self):
        ns, _, _, _ = load_camera()
        ns["handle_shutdown"](15, None)
        self.assertTrue(ns["shutdown_requested"].is_set())
        self.assertTrue(ns["shutdown_requested"].wait(300))


class ShellTests(unittest.TestCase):
    def run_shell(self, body, env=None):
        text = RUN.read_text()
        functions = text[text.index("wait_for_process()"):text.index("# Test Python")]
        result = subprocess.run(
            ["bash", "-c", "set -e\n"
             "bashio::log.info() { echo \"$*\"; }\n"
             "bashio::log.warning() { echo \"$*\"; }\n"
             "sha1sum() { shasum -a 1; }\n"
             "STOPPING=false\nPIDS=()\n" + functions + body],
            env={**os.environ, **(env or {})}, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_fingerprint_migration_rename_and_override(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "Old_Name.txt").write_text("a" * 40 + "\n")
            self.run_shell("""
load_fingerprint token Old_Name ""
test "$FINGERPRINT" = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
load_fingerprint token New_Name ""
test "$FINGERPRINT" = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
load_fingerprint second Other_Name bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
load_fingerprint second Renamed ""
test "$FINGERPRINT" = bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
load_fingerprint fresh New_Camera ""
test "${#FINGERPRINT}" = 40
first="$FINGERPRINT"
load_fingerprint fresh Renamed ""
test "$FINGERPRINT" = "$first"
""", {"FINGERPRINT_DIR": directory})

    def test_storage_failure_is_reported(self):
        self.run_shell("""
if load_fingerprint token Camera override; then exit 1; fi
""", {"FINGERPRINT_DIR": "/nonexistent/prusa-test"})

    def test_camera_restart_and_graceful_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory, "python3")
            executable.write_text(f"""#!{sys.executable}
import os, signal, time
from pathlib import Path
log = Path(os.environ["TEST_LOG"])
previous = log.read_text() if log.exists() else ""
with log.open("a") as f:
    f.write("start " + os.environ["CAMERA_NAME"] + "\\n")
if not previous:
    raise SystemExit(42)
def stop(*_):
    time.sleep(0.1)
    with log.open("a") as f:
        f.write("finished\\n")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
with log.open("a") as f:
    f.write("ready\\n")
while True:
    time.sleep(0.01)
""")
            executable.chmod(0o700)
            log = Path(directory, "events")
            self.run_shell("""
sleep() { command sleep 0.05; }
export CAMERA_NAME=Printer
run_camera &
PIDS+=($!)
for (( i=0; i<100; i++ )); do
    if grep -q ready "$TEST_LOG" 2>/dev/null; then break; fi
    command sleep 0.02
done
grep -q ready "$TEST_LOG"
shutdown
wait_for_process "${PIDS[0]}"
grep -q finished "$TEST_LOG"
test "$(grep -c start "$TEST_LOG")" = 2
""", {"PATH": directory + ":" + os.environ["PATH"], "TEST_LOG": str(log)})

    def test_shutdown_during_restart_delay(self):
        self.run_shell("""
python3() { return 42; }
export CAMERA_NAME=Printer
run_camera &
PIDS+=($!)
command sleep 0.2
shutdown
wait_for_process "${PIDS[0]}"
""")

    def test_signal_interrupting_parent_wait(self):
        self.run_shell("""
python3() { return 42; }
export CAMERA_NAME=Printer
run_camera &
PIDS+=($!)
parent=$$
(command sleep 0.2; kill -TERM "$parent") &
timer=$!
wait_for_process "${PIDS[0]}"
wait "$timer"
test "$STOPPING" = true
""")

    def test_camera_environment_is_isolated(self):
        output = self.run_shell("""
python3() { echo "token=$TOKEN"; return 0; }
export CAMERA_NAME=First TOKEN=first
run_camera &
PIDS+=($!)
export CAMERA_NAME=Second TOKEN=second
run_camera &
PIDS+=($!)
command sleep 0.2
shutdown
for pid in "${PIDS[@]}"; do wait_for_process "$pid"; done
""")
        self.assertIn("[First] token=first", output)
        self.assertIn("[Second] token=second", output)
        self.assertNotIn("[First] token=second", output)


if __name__ == "__main__":
    unittest.main()
