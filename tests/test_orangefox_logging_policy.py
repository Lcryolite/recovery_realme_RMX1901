import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
VENDOR = Path(os.environ.get(
    "ORANGEFOX_VENDOR_SOURCE",
    ROOT / "artifacts/recovery-diagnostics-20261028/orangefox-vendor-source",
))
RECOVERY = Path(os.environ.get(
    "ORANGEFOX_RECOVERY_SOURCE",
    ROOT / "artifacts/recovery-diagnostics-20261028/orangefox-recovery-source",
))


class OrangeFoxLoggingPolicyTest(unittest.TestCase):
    def test_diagnostic_retention_failure_has_a_visible_dismissible_warning(self):
        gui = RECOVERY / "gui/gui.cpp"
        dialogs = RECOVERY / "gui/theme/portrait_hdpi/pages/dialogs.xml"
        if not gui.is_file() or not dialogs.is_file():
            self.skipTest("OrangeFox Recovery source not available")
        self.assertIn('/tmp/rmx1901-diagnostics.status', gui.read_text())
        self.assertIn('state=error', gui.read_text())
        self.assertIn('state=partial', gui.read_text())
        self.assertIn('state=pending', gui.read_text())
        self.assertIn('diagnostics_unready_since', gui.read_text())
        self.assertIn('dialog_rmx_diagnostics_warning', gui.read_text())
        dialog = dialogs.read_text().split('<page name="dialog_rmx_diagnostics_warning">', 1)[1].split('</page>', 1)[0]
        self.assertIn('Diagnostics logs may not be saved', dialog)
        self.assertIn('action function="overlay"', dialog)

    def test_adb_backup_does_not_create_a_userspace_log(self):
        source = RECOVERY / "adbbu/twrpback.cpp"
        if not source.is_file():
            self.skipTest("OrangeFox Recovery source not available")
        text = source.read_text()
        self.assertNotIn('"/tmp/adb.log"', text)
        self.assertIn('adblogfile.open("/dev/null"', text)

    def test_pmsg_refresh_and_persist_do_not_start_automatically(self):
        makefile = RECOVERY / "Android.mk"
        scripts = [RECOVERY / "recovery-refresh.rc", RECOVERY / "recovery-persist.rc"]
        if not makefile.is_file() or not all(path.is_file() for path in scripts):
            self.skipTest("OrangeFox Recovery source not available")
        self.assertNotIn("TWRP_REQUIRED_MODULES += recovery-persist recovery-refresh", makefile.read_text())
        for path in scripts:
            active = [line for line in path.read_text().splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
            self.assertFalse(any("exec" in line for line in active))

    def test_cache_unmount_and_rmrf_release_diagnostics_before_storage_changes(self):
        source = RECOVERY / "partition.cpp"
        if not source.is_file():
            self.skipTest("OrangeFox Recovery source not available")
        text = source.read_text()
        unmount = text.split("bool TWPartition::UnMount(", 1)[1].split("bool TWPartition::", 1)[0]
        wipe = text.split("bool TWPartition::Wipe_RMRF(", 1)[1].split("bool TWPartition::", 1)[0]
        for body, operation in ((unmount, "umount2(Mount_Point.c_str(), flags)"),
                                (wipe, "TWFunc::removeDir(Mount_Point, true)")):
            self.assertLess(body.index("pause_cache_diagnostics"), body.index(operation))
            self.assertGreater(body.rindex("resume_cache_diagnostics"), body.index(operation))

    def test_cache_pause_waits_for_stop_and_restores_running_service(self):
        source = RECOVERY / "partition.cpp"
        compiler = shutil.which("c++")
        if not source.is_file() or not compiler:
            self.skipTest("OrangeFox Recovery source or C++ compiler not available")
        helpers = "static bool pause_cache_diagnostics" + source.read_text().split(
            "static bool pause_cache_diagnostics", 1)[1].split("\nstruct flag_list", 1)[0]
        harness = r'''
#include <cstring>
#include <cstdio>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#define PROPERTY_VALUE_MAX 92
static char state[PROPERTY_VALUE_MAX];
static int mode, stops, starts, syncs;
int fake_get(const char *, char *out, const char *) { std::strcpy(out, state); return std::strlen(out); }
int fake_open(const char *path, int) { return std::strcmp(path, "/cache") ? -1 : 42; }
int fake_close(int fd) { return fd == 42 ? 0 : -1; }
int fake_syncfs(int fd) { if (fd != 42) return -1; ++syncs; return mode == 3 ? -1 : 0; }
int fake_set(const char *key, const char *) {
    if (!std::strcmp(key, "ctl.stop")) {
        ++stops;
        if (syncs < stops) return -1;
        if (mode == 1) return -1;
        std::strcpy(state, mode == 2 ? "stopping" : "stopped");
    } else {
        ++starts;
        std::strcpy(state, "running");
    }
    return 0;
}
int fake_sleep(unsigned int) { return 0; }
namespace msg { enum Kind { kWarning }; }
struct Msg { Msg(msg::Kind, const char *) {} };
void gui_msg(const Msg &) {}
#define property_get fake_get
#define property_set fake_set
#define usleep fake_sleep
#define open fake_open
#define close fake_close
#define syncfs fake_syncfs
#define LOGERR(...) ((void)0)
''' + helpers + r'''
int main() {
    bool resume;
    std::strcpy(state, "stopped");
    if (!pause_cache_diagnostics(&resume) || resume || stops || starts) return 1;
    resume_cache_diagnostics(resume);
    if (starts) return 2;
    std::strcpy(state, "running");
    if (!pause_cache_diagnostics(&resume) || !resume || stops != 1 || syncs != 1) return 3;
    resume_cache_diagnostics(resume);
    if (starts != 1 || std::strcmp(state, "running")) return 4;
    mode = 2;
    if (pause_cache_diagnostics(&resume) || !resume) return 5;
    resume_cache_diagnostics(resume);
    if (starts != 2) return 6;
    mode = 1;
    if (pause_cache_diagnostics(&resume) || !resume) return 7;
    resume_cache_diagnostics(resume);
    if (starts != 3) return 8;
    mode = 3;
    if (pause_cache_diagnostics(&resume) || resume || stops != 3 || syncs != 4) return 9;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache_pause.cpp"
            binary = path.with_suffix("")
            path.write_text(harness)
            build = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                                    str(path), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_copy_log_menu_reports_that_userspace_logging_is_disabled(self):
        path = RECOVERY / "gui/action.cpp"
        if not path.is_file():
            self.skipTest("OrangeFox Recovery source not available")
        source = path.read_text().split("int GUIAction::copylog(", 1)[1].split(
            "int GUIAction::compute(", 1
        )[0]
        self.assertFalse('copy_file("/tmp/recovery.log"' in source,
                         "menu still copies the disabled log")
        self.assertIn("Recovery userspace logging is disabled", source)
        self.assertIn("copy_kernel_log", source)
        self.assertNotIn("TWFunc::copy_logcat(", source)
        self.assertIn("logcat is unavailable", source)

    def test_backup_and_reboot_do_not_auto_copy_userspace_logs(self):
        files = [RECOVERY / "twrp-functions.cpp", RECOVERY / "partitionmanager.cpp"]
        if not all(path.is_file() for path in files):
            self.skipTest("OrangeFox Recovery source not available")
        for path in files:
            source = path.read_text()
            self.assertFalse('copy_file("/tmp/recovery.log"' in source,
                             f"{path.name} still copies the runtime log")
            self.assertFalse("cp -f /tmp/recovery.log" in source,
                             f"{path.name} still generates a log-copy script")
            self.assertFalse("cp -af /tmp/recovery.log" in source,
                             f"{path.name} still generates a log-copy script")

    def test_automatic_logging_does_not_write_recovery_records_to_cache_or_pmsg(self):
        functions = RECOVERY / "twrp-functions.cpp"
        logging = RECOVERY / "recovery_utils/logging.cpp"
        main = RECOVERY / "recovery_main.cpp"
        if not all(p.is_file() for p in (functions, logging, main)):
            self.skipTest("OrangeFox Recovery source not available")
        text = functions.read_text()
        update = text.split("void TWFunc::Update_Log_File(void) {", 1)[1].split(
            "void TWFunc::Clear_Bootloader_Message()", 1
        )[0]
        self.assertIn('unlink("/cache/recovery/command")', update)
        self.assertFalse("Copy_Log(" in update, "startup still archives Recovery logs")
        self.assertFalse("last_log.gz" in update, "startup still rotates Recovery logs")
        copy_logs = logging.read_text().split("void copy_logs(bool save_current_log) {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertFalse("copy_log_file" in copy_logs, "AOSP log archiving is still active")
        self.assertFalse("rotate_logs" in copy_logs, "AOSP log rotation is still active")
        self.assertFalse("__android_log_pmsg_file_read(" in main.read_text(),
                         "Recovery still rewrites old pmsg records")

    def test_startup_discards_recovery_stdio_and_script_appends(self):
        init = RECOVERY / "etc/init.rc"
        main = RECOVERY / "twrp.cpp"
        if not init.is_file() or not main.is_file():
            self.skipTest("OrangeFox Recovery source not available")
        init_text = init.read_text()
        main_text = main.read_text()
        mount = init_text.index("mount tmpfs tmpfs /tmp")
        sink = init_text.find("symlink /dev/null /tmp/recovery.log")
        self.assertGreater(sink, mount, "log sink must follow the /tmp mount")
        self.assertIn('freopen("/dev/null", "a", stdout)', main_text)
        self.assertIn('freopen("/dev/null", "a", stderr)', main_text)
        self.assertNotIn("import /init.recovery.logd.rc", init_text)

    def test_startup_retains_panel_kernel_input_without_a_logcat_snapshot(self):
        path = VENDOR / "FoxExtras/sbin/foxstart.sh"
        if not path.is_file():
            self.skipTest("OrangeFox vendor source not available")
        startup = path.read_text()
        self.assertFalse("logcat -d" in startup, "startup still captures logcat")
        self.assertIn("dmesg &> /tmp/dmesg.log", startup)
        self.assertIn('local KLOG="/tmp/dmesg.log"', startup)
        self.assertIn("Get_Display_Panel", startup)


if __name__ == "__main__":
    unittest.main()
