#!/usr/bin/env python3
"""Hardware-free regression checks: python3 -m unittest discover -s firmware/tests."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import zipfile

FIRMWARE = Path(__file__).resolve().parents[1]
BOARDS = ("e200", "e310v2")


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="antsdr-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")
        # Keep the fixture independent of the caller's build environment.
        for key in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "JOBS"):
            self.env.pop(key, None)

    def write(self, path, contents, executable=False):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(contents).lstrip())
        if executable:
            path.chmod(0o755)
        return path

    def run_command(self, args, success=True):
        result = subprocess.run(args, cwd=self.root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result


class BuildRulesTest(SandboxTest):
    def setUp(self):
        super().setUp()
        shutil.copyfile(FIRMWARE / "Makefile", self.root / "Makefile")
        self.settings = self.write("settings.sh", f'export PATH="{self.bin}:$PATH"\n')
        self.write("bin/xsdk", """
            #!/bin/sh
            mkdir -p build/sdk/fsbl/Release
            cp build/system_top.hdf build/sdk/fsbl/Release/fsbl.elf
        """, executable=True)
        self.write("scripts/create_fsbl_project.tcl", "# mocked SDK input\n")
        self.write("fake_build.py", '''
            import os
            from pathlib import Path
            import sys
            import time
            import zipfile

            action = sys.argv[1]
            with open('../events', 'a') as log:
                log.write(action + ':' + os.environ.get('MAKEFLAGS', '') + '\\n')
            if action.endswith('_defconfig'):
                time.sleep(0.05)
                Path('.config').write_text('configured')
            elif action == 'olddefconfig':
                assert Path('.config').read_text() == 'configured'
                Path('.config').write_text('ready')
            elif action == 'uImage':
                assert Path('.config').read_text() == 'ready'
                time.sleep(0.05)
                Path('arch/arm/boot/uImage').write_text('kernel')
            elif action.endswith('.dtb'):
                assert Path('.config').read_text() == 'ready'
                assert Path('arch/arm/boot/uImage').read_text() == 'kernel'
                Path('arch/arm/boot/dts/' + action).write_text('dtb')
            elif action == 'uboot':
                assert Path('.config').exists()
                Path('u-boot').write_text('uboot')
        ''')
        self.write("linux/Makefile", """
            .PHONY: zynq_e200_defconfig zynq_e310v2_defconfig olddefconfig uImage zynq-e200.dtb zynq-e310v2.dtb
            zynq_e200_defconfig zynq_e310v2_defconfig olddefconfig uImage zynq-e200.dtb zynq-e310v2.dtb:
            \tpython3 ../fake_build.py $@
        """)
        self.write("u-boot-xlnx/Makefile", """
            .PHONY: all zynq_e200_defconfig zynq_e310v2_defconfig
            all:
            \tpython3 ../fake_build.py uboot
            zynq_e200_defconfig zynq_e310v2_defconfig:
            \tpython3 ../fake_build.py $@
        """)
        for board in BOARDS:
            self.write(f"linux/arch/arm/boot/dts/zynq-{board}.dts", "// fixture\n")

    def make(self, board, *targets, jobs=2, success=True):
        return self.run_command([
            "make", f"-j{jobs}", f"TARGET={board}", "CROSS_COMPILE=",
            "HAVE_VIVADO=0", f"VIVADO_SETTINGS={self.settings}",
            "VERSION=test", "LATEST_TAG=test", "UBOOT_VERSION=test", *targets,
        ], success=success)

    def test_parallel_kernel_dtb_and_job_limit(self):
        for board in BOARDS:
            for jobs in (1, 2):
                with self.subTest(board=board, jobs=jobs):
                    (self.root / "events").unlink(missing_ok=True)
                    for path in ("linux/.config", "linux/arch/arm/boot/uImage"):
                        (self.root / path).unlink(missing_ok=True)
                    result = self.make(board, "build/device_tree.dtb", "build/uImage",
                                       "build/u-boot.elf", jobs=jobs)
                    self.assertNotIn("jobserver unavailable", result.stdout)
                    self.assertNotIn("resetting jobserver", result.stdout)
                    events = (self.root / "events").read_text().splitlines()
                    actions = [line.split(":", 1)[0] for line in events]
                    self.assertEqual(actions.count("olddefconfig"), 1)
                    self.assertLess(actions.index("uImage"), actions.index(f"zynq-{board}.dtb"))
                    for event in events:
                        self.assertIn(f"-j{jobs}", event)

    def export_hdf(self, board, age=1_000_000_000, payload=None):
        path = self.root / f"fpga/antsdr_{board}/artifacts/system_top.hdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"antsdr_{board}.bit", payload or board)
        os.utime(path, (age, age))
        return path

    def test_board_switch_refreshes_bitstream_and_fsbl(self):
        for board in BOARDS:
            self.export_hdf(board)
        for board in ("e200", "e310v2", "e200"):
            with self.subTest(board=board):
                self.make(board, "build/system_top.bit", "build/fsbl.elf")
                self.assertEqual((self.root / "build/system_top.bit").read_text(), board)
                expected = (self.root / f"fpga/antsdr_{board}/artifacts/system_top.hdf").read_bytes()
                self.assertEqual((self.root / "build/system_top.hdf").read_bytes(), expected)
                self.assertEqual((self.root / "build/fsbl.elf").read_bytes(), expected)
        before = (self.root / "build/fsbl.elf").stat().st_mtime_ns
        self.make("e200", "build/system_top.bit", "build/fsbl.elf")
        self.assertEqual((self.root / "build/fsbl.elf").stat().st_mtime_ns, before)
        self.export_hdf("e200", payload="updated")
        self.make("e200", "build/system_top.bit", "build/fsbl.elf")
        self.assertEqual((self.root / "build/system_top.bit").read_text(), "updated")

    def test_auto_fpga_build_inherits_jobs_and_propagates_failure(self):
        for board in BOARDS:
            directory = f"fpga/antsdr_{board}"
            self.write(f"{directory}/Makefile", """
                .PHONY: all
                all:
                \tprintf '%s\\n' '$(MAKEFLAGS)' > flags
                \tmkdir -p artifacts
                \tprintf hdf > artifacts/system_top.hdf
                \ttest -z "$$FAIL_FPGA"
            """)
            with self.subTest(board=board):
                self.env["FAIL_FPGA"] = "1"
                self.make(board, "build/system_top.hdf", success=False)
                self.assertFalse((self.root / "build/system_top.hdf").exists())
                (self.root / directory / "artifacts/system_top.hdf").unlink()
                self.env.pop("FAIL_FPGA")
                self.make(board, "build/system_top.hdf")
                self.assertIn("-j2", (self.root / directory / "flags").read_text())
                self.assertEqual((self.root / "build/system_top.hdf").read_text(), "hdf")
                (self.root / "build/system_top.hdf").unlink()


class UpdaterTest(SandboxTest):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "calls"
        self.scratch = self.root / "firmware.frm"
        for command in ("dd", "fw_setenv", "sync"):
            self.write(f"bin/{command}", f"""
                #!/bin/sh
                echo '{command}' >> '{self.calls}'
                [ "$FAIL_COMMAND" != '{command}' ]
            """, executable=True)
        self.write("device_config", 'FRM_MAGIC="ANTSDR E200 (MICROPHASE)"\n')
        for board in BOARDS:
            script = (FIRMWARE / f"buildroot/board/{board}/update_frm.sh").read_text()
            # Redirect fixed device paths inside the fixture; execute the actual
            # checksum, validation, write sequencing, cleanup and exit handling.
            script = script.replace("/etc/device_config", str(self.root / "device_config"))
            script = script.replace("/opt/firmware.frm", str(self.scratch))
            script = script.replace("/sys/class/leds/led0:green", str(self.root / "led"))
            script = script.replace("/dev/mtdblock3", str(self.root / "flash"))
            self.write(f"{board}.sh", script)
        (self.root / "led").mkdir()

    def image(self, payload, checksum=None):
        path = self.root / "image with spaces.frm"
        digest = checksum or hashlib.md5(payload).hexdigest()
        path.write_bytes(payload + digest.encode() + b"\n")
        return path

    def check_update(self, board, payload, expected_calls, success, failure="", checksum=None):
        self.calls.unlink(missing_ok=True)
        self.env["FAIL_COMMAND"] = failure
        image = self.image(payload, checksum)
        result = self.run_command(["sh", f"{board}.sh", str(image)], success=success)
        actual = self.calls.read_text().splitlines() if self.calls.exists() else []
        self.assertEqual(actual, expected_calls)
        self.assertFalse(self.scratch.exists())
        self.assertEqual("Done" in result.stdout, success)

    def test_update_success_and_failures(self):
        for board in BOARDS:
            magic = f"ANTSDR {'E200' if board == 'e200' else 'E310V2'} (MICROPHASE)"
            self.write("device_config", f'FRM_MAGIC="{magic}"\n')
            payload = (magic + " firmware").encode()
            cases = [
                ("success", payload, ["dd", "fw_setenv", "sync"], True, "", None),
                ("checksum", payload, [], False, "", "0" * 32),
                ("board", b"another board", ["sync"], False, "", None),
                ("literal magic", magic.replace("(", "").replace(")", "").encode(), ["sync"], False, "", None),
                ("flash", payload, ["dd", "sync"], False, "dd", None),
                ("environment", payload, ["dd", "fw_setenv", "sync"], False, "fw_setenv", None),
                ("sync", payload, ["dd", "fw_setenv", "sync"], False, "sync", None),
            ]
            for name, data, calls, success, failure, checksum in cases:
                with self.subTest(board=board, failure=name):
                    self.check_update(board, data, calls, success, failure, checksum)
            self.write("device_config", 'FRM_MAGIC=""\n')
            with self.subTest(board=board, failure="empty magic"):
                self.check_update(board, payload, [], False)

    def test_extraction_failure_does_not_flash(self):
        self.write("bin/head", "#!/bin/sh\nexit 1\n", executable=True)
        for board in BOARDS:
            with self.subTest(board=board):
                self.check_update(board, b"ANTSDR E200 (MICROPHASE)", [], False)

    def test_invalid_inputs_fail_without_writes(self):
        empty = self.write("empty.frm", "")
        wrong_extension = self.write("wrong.bin", "data")
        for board in BOARDS:
            for args in ([], ["missing.frm"], [str(empty)], [str(wrong_extension)]):
                with self.subTest(board=board, args=args):
                    self.run_command(["sh", f"{board}.sh", *args], success=False)
                    self.assertFalse(self.calls.exists())


class ServiceTest(SandboxTest):
    def test_stop_matches_started_daemons(self):
        self.write("bin/start-stop-daemon", f'''
            #!/usr/bin/env python3
            from pathlib import Path
            import sys
            state = Path({str(self.root / 'running')!r})
            running = set(state.read_text().splitlines()) if state.exists() else set()
            args = sys.argv[1:]
            if '--start' in args:
                running.add(Path(args[-1]).name)
            else:
                name = args[args.index('--name') + 1]
                if name not in running:
                    sys.exit(1)
                running.remove(name)
            state.write_text(''.join(name + '\\n' for name in sorted(running)))
        ''', executable=True)
        self.write("bin/ifconfig", '#!/bin/sh\necho "inet 192.168.1.10"\n', executable=True)
        self.write("bin/ping", "#!/bin/sh\nexit 0\n", executable=True)
        for board in BOARDS:
            with self.subTest(board=board):
                script = FIRMWARE / f"buildroot/board/{board}/S90device_start.sh"
                self.run_command(["sh", str(script), "start"])
                self.assertEqual(set((self.root / "running").read_text().splitlines()), {board, "net_set"})
                self.run_command(["sh", str(script), "stop"])
                self.assertEqual((self.root / "running").read_text(), "")


if __name__ == "__main__":
    unittest.main()
