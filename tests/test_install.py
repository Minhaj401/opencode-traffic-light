#!/usr/bin/env python3
"""Exercise setup and service commands without touching the host's services."""
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.opencode.traffic-light"


def stub(command, args):
    home = Path(os.environ["HOME"])
    with (home / "commands.jsonl").open("a") as log:
        log.write(json.dumps([command, *args]) + "\n")
    state_path = home / "launch-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    if command == "uname":
        print(os.environ["TEST_OS"])
    elif command == "xcrun":
        if args == ["--find", "swiftc"]:
            print("/mock/swiftc")
        else:
            if os.environ.get("FAIL_COMPILE"):
                return 1
            assert args[0] == "swiftc" and "-parse-as-library" in args
            output = Path(args[args.index("-o") + 1])
            output.write_text("#!/bin/sh\nexit 0\n")
            output.chmod(0o755)
    elif command == "plutil":
        path = Path(args[-1])
        if args[0] == "-create":
            value = {}
        else:
            with path.open("rb") as file:
                value = plistlib.load(file)
            if args[0] == "-lint":
                return 0
            assert args[0] == "-insert", args
            item = args[3]
            if args[2] == "-bool":
                item = item == "true"
            elif args[2] == "-json":
                item = json.loads(item)
            else:
                assert args[2] == "-string", args
            if args[1].startswith("ProgramArguments."):
                value["ProgramArguments"].insert(int(args[1].split(".")[1]), item)
            else:
                value[args[1]] = item
        with path.open("wb") as file:
            plistlib.dump(value, file)
    elif command == "launchctl":
        action = args[0]
        assert args[1].startswith("gui/") or action == "kickstart", args
        if action == "print":
            if not state.get("loaded"):
                return 113
            print("state = running\n\tpid = 12345" if state.get("running") else "state = not running")
        elif action == "bootstrap":
            assert len(args) == 3
            with Path(args[2]).open("rb") as file:
                definition = plistlib.load(file)
                assert definition["Label"] == LABEL
            if state.get("disabled") or state.get("loaded"):
                return 5
            state.update(loaded=True, running=definition.get("RunAtLoad", False))
            if os.environ.get("TEST_BOOTSTRAP_RACE"):
                state_path.write_text(json.dumps(state))
                return 5
        elif action == "bootout":
            if not state.get("loaded"):
                return 113
            state.update(loaded=False, running=False)
        elif action == "kickstart":
            if not state.get("loaded"):
                return 113
            state["running"] = True
        elif action in ("enable", "disable"):
            state["disabled"] = action == "disable"
        else:
            raise AssertionError(args)
        state_path.write_text(json.dumps(state))
    elif command == "python3":
        if args and args[0] == "-c" and "import gi" in args[1]:
            assert "import cairo" in args[1] and "gi.require_foreign('cairo')" in args[1]
            return 1 if os.environ.get("TEST_INSTALL_DEPS") else 0
        return subprocess.call([sys.executable, *args])
    elif command == "systemctl":
        if "is-active" in args:
            if not state.get("running"):
                return 3
            print("active")
        elif "show" in args:
            print("12345")
        elif args[1] in ("start", "restart", "enable"):
            state["running"] = True
        elif args[1] in ("stop", "disable"):
            state["running"] = False
        state_path.write_text(json.dumps(state))
    elif command == "journalctl":
        pass
    elif command == "sudo":
        if os.environ.get("TEST_INSTALL_DEPS"):
            assert args in (["apt", "update"], ["apt", "install", "-y", "python3-gi",
                           "python3-cairo", "python3-gi-cairo", "gir1.2-gtk-3.0"])
            return 0
        raise AssertionError("Tests must not install packages")
    else:
        raise AssertionError(command)
    return 0


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="traffic-light-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home & <user>"
        self.home.mkdir()
        self.repo = self.root / 'checkout & <widget> "quoted" % $HOME'
        self.repo.mkdir()
        for name in ("setup.sh", "traffic-light", "traffic-light.js", "traffic-light-launcher.js",
                     "package.json", "opencode-traffic-light.py", "opencode-traffic-light.service"):
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, self.repo / name)
        # Packaging checks use a minimal executable, independent of frontend changes.
        (self.repo / "opencode-traffic-light.swift").write_text(
            "@main struct WidgetFixture { static func main() {} }\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("uname", "xcrun", "plutil", "launchctl", "systemctl", "journalctl", "python3", "sudo", "apt"):
            path = self.bin / name
            path.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} "
                            f"{shlex.quote(str(Path(__file__).resolve()))} --stub {name} \"$@\"\n")
            path.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), TEST_OS="Darwin",
                        PATH=f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin")
        for key in ("OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                    "FAIL_COMPILE", "TEST_BOOTSTRAP_RACE", "TEST_INSTALL_DEPS"):
            self.env.pop(key, None)
        self.config = self.home / ".config/opencode"
        self.plist = self.home / f"Library/LaunchAgents/{LABEL}.plist"

    def run_script(self, name, *args, check=True, timeout=20):
        result = subprocess.run(["sh", str(self.repo / name), *args], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def commands(self):
        path = self.home / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def launch_commands(self):
        return [args[1:] for args in self.commands() if args[0] == "launchctl"]

    def clear_commands(self):
        (self.home / "commands.jsonl").write_text("")

    def test_macos_fresh_setup_and_repeat(self):
        self.run_script("setup.sh")
        with self.plist.open("rb") as file:
            plist = plistlib.load(file)
        executable = self.home / "Library/Application Support/opencode-traffic-light/opencode-traffic-light"
        self.assertTrue(executable.is_file())
        self.assertEqual(plist["Label"], LABEL)
        self.assertEqual(plist["ProgramArguments"], [str(executable), "--exit-on-disconnect"])
        self.assertFalse(plist.get("KeepAlive", False))
        self.assertFalse(plist["RunAtLoad"])
        self.assertEqual(plist["LimitLoadToSessionType"], "Aqua")
        self.assertEqual(plist["StandardOutPath"], str(self.home / "Library/Logs/opencode-traffic-light/stdout.log"))
        self.assertEqual(plist["StandardErrorPath"], str(self.home / "Library/Logs/opencode-traffic-light/stderr.log"))
        self.assertEqual((self.home / ".local/bin/traffic-light").resolve(), (self.repo / "traffic-light").resolve())
        self.assertEqual((self.config / "plugins/traffic-light.js").read_bytes(), (ROOT / "traffic-light.js").read_bytes())
        self.assertEqual((self.config / "plugins/traffic-light-launcher.js").read_bytes(),
                         (ROOT / "traffic-light-launcher.js").read_bytes())
        self.assertFalse((self.config / "opencode.json").exists())
        self.assertFalse((self.config / "opencode.jsonc").exists())
        self.run_script("setup.sh")
        self.assertEqual(len(list((self.config / "plugins").glob("*.js"))), 2)
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        self.assertNotIn("kickstart", [args[0] for args in self.launch_commands()])
        self.run_script("traffic-light", "autostart")
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")
        self.assertNotIn("systemctl", [args[0] for args in self.commands()])
        self.assertNotIn("sudo", [args[0] for args in self.commands()])
        if sys.platform == "darwin":
            subprocess.run(["/usr/bin/plutil", "-lint", str(self.plist)], check=True, capture_output=True)

    def test_existing_config_untouched(self):
        self.config.mkdir(parents=True)
        raw = '// custom config\n{ "plugin": ["something-else"], "model": "provider/model", }\n'
        path = self.config / "opencode.jsonc"
        path.write_text(raw)
        self.run_script("setup.sh")
        self.assertEqual(path.read_text(), raw)
        self.assertTrue((self.config / "plugins/traffic-light.js").exists())

    def test_legacy_registration_updated_without_duplicate(self):
        for filename in ("opencode.json", "opencode.jsonc"):
            with self.subTest(filename=filename):
                self.config.mkdir(parents=True, exist_ok=True)
                path = self.config / filename
                raw = '{"plugin":["./plugins/traffic-light/traffic-light.js"]}\n'
                path.write_text(raw)
                self.run_script("setup.sh")
                self.assertEqual(path.read_text(), raw)
                self.assertTrue((self.config / "plugins/traffic-light/traffic-light.js").exists())
                self.assertTrue((self.config / "plugins/traffic-light/package.json").exists())
                self.assertFalse((self.config / "plugins/traffic-light.js").exists())
                self.assertTrue((self.config / "plugins/traffic-light-launcher.js").exists())
                path.unlink()

    def test_custom_config_directory(self):
        self.env["OPENCODE_CONFIG_DIR"] = str(self.home / "custom config")
        self.run_script("setup.sh")
        self.assertTrue((Path(self.env["OPENCODE_CONFIG_DIR"]) / "plugins/traffic-light.js").exists())
        self.assertFalse(self.config.exists())

    def test_xdg_config_directory(self):
        self.env["TEST_OS"] = "Linux"
        self.env["XDG_CONFIG_HOME"] = str(self.home / "xdg config")
        self.run_script("setup.sh")
        self.assertTrue((Path(self.env["XDG_CONFIG_HOME"]) / "opencode/plugins/traffic-light.js").exists())
        self.assertTrue((Path(self.env["XDG_CONFIG_HOME"]) / "systemd/user/opencode-traffic-light.service").exists())
        self.assertFalse(self.config.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Requires Apple's native build tools")
    def test_real_macos_compiler_and_plist_tools(self):
        (self.bin / "xcrun").unlink()
        (self.bin / "plutil").unlink()
        self.run_script("setup.sh", timeout=120)
        with self.plist.open("rb") as file:
            plist = plistlib.load(file)
        executable = Path(plist["ProgramArguments"][0])
        self.assertTrue(os.access(executable, os.X_OK))
        self.assertGreater(executable.stat().st_size, 10000)
        subprocess.run(["/usr/bin/plutil", "-lint", str(self.plist)], check=True, capture_output=True)

    def test_compile_failure_stops_install(self):
        self.env["FAIL_COMPILE"] = "1"
        result = self.run_script("setup.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(self.launch_commands(), [])

    def test_compile_failure_preserves_previous_install(self):
        self.run_script("setup.sh")
        binary = self.home / "Library/Application Support/opencode-traffic-light/opencode-traffic-light"
        previous = binary.read_bytes()
        self.clear_commands()
        self.env["FAIL_COMPILE"] = "1"
        self.assertNotEqual(self.run_script("setup.sh", check=False).returncode, 0)
        self.assertEqual(binary.read_bytes(), previous)
        self.assertEqual(list(binary.parent.glob(".widget.*")), [])
        self.assertEqual(self.launch_commands(), [])

    def test_macos_controls(self):
        self.run_script("setup.sh")
        self.run_script("traffic-light", "start")
        target = f"gui/{os.getuid()}/{LABEL}"
        domain = f"gui/{os.getuid()}"
        self.clear_commands()
        self.run_script("traffic-light", "stop")
        self.assertIn(["bootout", target], self.launch_commands())
        self.assertNotIn(["disable", target], self.launch_commands())
        self.assertEqual(self.run_script("traffic-light").stdout.strip(), "stopped")
        self.run_script("traffic-light", "stop")
        self.clear_commands()
        self.run_script("traffic-light", "start")
        self.assertIn(["bootstrap", domain, str(self.plist)], self.launch_commands())
        self.assertNotIn(["enable", target], self.launch_commands())
        self.clear_commands()
        self.run_script("traffic-light", "start")
        self.assertNotIn("bootstrap", [args[0] for args in self.launch_commands()])
        self.run_script("traffic-light", "restart")
        self.assertIn(["kickstart", "-k", target], self.launch_commands())
        self.run_script("traffic-light", "disable")
        self.assertIn(["disable", target], self.launch_commands())
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        self.assertNotEqual(self.run_script("traffic-light", "start", check=False).returncode, 0)
        self.run_script("traffic-light", "enable")
        self.assertIn(["enable", target], self.launch_commands())
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")

    def test_loaded_but_quit_is_stopped_and_can_restart(self):
        self.run_script("setup.sh")
        self.run_script("traffic-light", "start")
        state_path = self.home / "launch-state.json"
        state = json.loads(state_path.read_text())
        state["running"] = False
        state_path.write_text(json.dumps(state))
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        self.run_script("traffic-light", "start")
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")

    def test_macos_logs_and_missing_install(self):
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        self.assertNotEqual(self.run_script("traffic-light", "start", check=False).returncode, 0)
        self.assertIn("setup.sh", self.run_script("traffic-light", "start", check=False).stderr)
        self.run_script("traffic-light", "logs")
        self.run_script("setup.sh")
        log_dir = self.home / "Library/Logs/opencode-traffic-light"
        (log_dir / "stdout.log").write_text("widget output\n")
        (log_dir / "stderr.log").write_text("widget error\n")
        result = self.run_script("traffic-light", "logs")
        self.assertIn("widget output", result.stdout)
        self.assertIn("widget error", result.stdout)

    def test_linux_setup_and_controls(self):
        self.env["TEST_OS"] = "Linux"
        self.run_script("setup.sh")
        unit = (self.home / ".config/systemd/user/opencode-traffic-light.service").read_text()
        self.assertNotIn("/home/minhaj", unit)
        self.assertIn(str(self.repo).replace('"', '\\"').replace("%", "%%").replace("$", "$$"), unit)
        self.assertIn(str(self.bin / "python3"), unit)
        self.assertFalse(self.plist.exists())
        self.assertIn("Restart=no", unit)
        self.assertIn("--exit-on-disconnect", unit)
        self.assertIn(["systemctl", "--user", "disable", "--now", "opencode-traffic-light.service"], self.commands())
        self.assertNotIn(["systemctl", "--user", "enable", "--now", "opencode-traffic-light.service"], self.commands())
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        expected = {"start": ["start"], "stop": ["stop"], "restart": ["restart"],
                    "enable": ["start"], "disable": ["disable", "--now"]}
        for command, args in expected.items():
            self.clear_commands()
            self.run_script("traffic-light", command)
            self.assertIn(["systemctl", "--user", *args, "opencode-traffic-light.service"], self.commands())
        self.run_script("traffic-light", "logs")
        self.assertIn(["journalctl", "--user", "-u", "opencode-traffic-light.service", "-n", "30", "--no-pager"], self.commands())
        self.run_script("traffic-light", "enable")
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")
        self.assertEqual(self.launch_commands(), [])

    def test_linux_installs_missing_cairo_and_introspection_bindings(self):
        self.env["TEST_OS"] = "Linux"
        self.env["TEST_INSTALL_DEPS"] = "1"
        self.run_script("setup.sh")
        self.assertIn(["sudo", "apt", "update"], self.commands())
        self.assertIn(["sudo", "apt", "install", "-y", "python3-gi", "python3-cairo",
                       "python3-gi-cairo", "gir1.2-gtk-3.0"], self.commands())
        self.assertEqual(self.launch_commands(), [])

    def test_disabled_autostart_and_setup_preserve_preference(self):
        for platform in ("Darwin", "Linux"):
            with self.subTest(platform=platform):
                self.env["TEST_OS"] = platform
                self.run_script("setup.sh")
                self.run_script("traffic-light", "disable")
                marker = self.home / ".local/state/opencode-traffic-light/disabled"
                self.assertTrue(marker.exists())
                self.clear_commands()
                self.run_script("traffic-light", "autostart")
                self.assertFalse(any(args[0] in ("launchctl", "systemctl") for args in self.commands()))
                self.run_script("setup.sh")
                self.assertTrue(marker.exists())
                self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
                self.run_script("traffic-light", "autostart")
                self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
                self.run_script("traffic-light", "enable")
                self.assertFalse(marker.exists())
                self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")
                self.run_script("traffic-light", "stop")
                self.run_script("traffic-light", "autostart")
                self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")

    def test_custom_disabled_marker_directory(self):
        self.env["XDG_STATE_HOME"] = str(self.home / "custom state")
        self.run_script("setup.sh")
        self.run_script("traffic-light", "disable")
        marker = Path(self.env["XDG_STATE_HOME"]) / "opencode-traffic-light/disabled"
        self.assertTrue(marker.exists())
        self.run_script("traffic-light", "autostart")
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "stopped")
        self.run_script("traffic-light", "enable")
        self.assertFalse(marker.exists())

    def test_concurrent_macos_bootstrap(self):
        self.run_script("setup.sh")
        self.env["TEST_BOOTSTRAP_RACE"] = "1"
        self.run_script("traffic-light", "autostart")
        self.assertEqual(self.run_script("traffic-light", "status").stdout.strip(), "running (12345)")

    def test_unsupported_os_and_unknown_command(self):
        self.env["TEST_OS"] = "FreeBSD"
        for script in ("setup.sh", "traffic-light"):
            self.assertNotEqual(self.run_script(script, check=False).returncode, 0)
        self.assertFalse(self.config.exists())
        self.env["TEST_OS"] = "Darwin"
        self.assertNotEqual(self.run_script("traffic-light", "typo", check=False).returncode, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--stub":
        sys.exit(stub(sys.argv[2], sys.argv[3:]))
    unittest.main()
