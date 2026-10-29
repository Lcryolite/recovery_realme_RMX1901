from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


class DiagnosticsMakeIntegrationTest(unittest.TestCase):
    def modules_for_device(self, device):
        if not shutil.which("make"):
            self.skipTest("GNU Make unavailable")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / "clear.mk").write_text(
                "LOCAL_MODULE :=\nLOCAL_SRC_FILES :=\nLOCAL_MODULE_PATH :=\n"
                "LOCAL_CFLAGS :=\nLOCAL_STATIC_LIBRARIES :=\n"
                "LOCAL_FORCE_STATIC_EXECUTABLE :=\n"
            )
            (work / "executable.mk").write_text(
                "$(info MODULE|$(LOCAL_MODULE)|$(LOCAL_MODULE_PATH)|"
                "$(LOCAL_SRC_FILES)|$(LOCAL_FORCE_STATIC_EXECUTABLE))\n"
            )
            # AOSP build/make android-12.1.0_r27 core/definitions.mk:
            # all-subdir-makefiles discovers immediate children, not descendants.
            makefile = work / "Makefile"
            makefile.write_text(
                f"TARGET_DEVICE := {device}\n"
                f"TARGET_RECOVERY_ROOT_OUT := {work}/recovery/root\n"
                f"CLEAR_VARS := {work}/clear.mk\n"
                f"BUILD_EXECUTABLE := {work}/executable.mk\n"
                "my-dir = $(patsubst %/,%,$(dir $(lastword $(MAKEFILE_LIST))))\n"
                "all-makefiles-under = $(wildcard $(1)/*/Android.mk)\n"
                "all-subdir-makefiles = $(call all-makefiles-under,$(call my-dir))\n"
                f"include {ROOT}/Android.mk\n"
                ".PHONY: all\nall:\n\t@:\n"
            )
            result = subprocess.run(
                ["make", "--no-print-directory", "-f", str(makefile), "all"],
                check=True, capture_output=True, text=True,
            )
            modules = {}
            for line in result.stdout.splitlines():
                if line.startswith("MODULE|"):
                    _, name, path, sources, static = line.split("|")
                    self.assertNotIn(name, modules, "module was loaded twice")
                    modules[name] = (Path(path).relative_to(work).as_posix(),
                                     sources.split(), static)
            return modules

    def test_device_entry_loads_both_recovery_executables(self):
        self.assertEqual(self.modules_for_device("RMX1901"), {
            "rmx1901-pstore-snapshot": (
                "recovery/root/system/bin", ["pstore_snapshot.c"], "true"),
            "rmx1901-diagnostics": (
                "recovery/root/system/bin", ["diagnostics.c", "pstore_snapshot.c"], "true"),
        })

    def test_other_devices_do_not_load_rmx1901_modules(self):
        self.assertEqual(self.modules_for_device("unrelated"), {})


if __name__ == "__main__":
    unittest.main()
