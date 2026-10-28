import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
SOURCE = Path(os.environ.get(
    "UBOOT_DIAGNOSTIC_SOURCE",
    ROOT / "artifacts/recovery-diagnostics-20261028/uboot-source",
))


class UBootTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = SOURCE / "cmd/fastboot.c"
        if not path.is_file() or not shutil.which("cc"):
            raise unittest.SkipTest("U-Boot source or C compiler not available")
        code = path.read_text()
        start = code.index("static int do_fastboot_usb(")
        opening = code.index("{", start)
        depth = 1
        end = opening + 1
        while depth:
            if code[end] == "{":
                depth += 1
            elif code[end] == "}":
                depth -= 1
            end += 1
        cls.function = code[start:end]
        start = code.index("static int do_fastboot(")
        opening = code.index("{", start)
        depth = 1
        end = opening + 1
        while depth:
            if code[end] == "{":
                depth += 1
            elif code[end] == "}":
                depth -= 1
            end += 1
        cls.wrapper = code[start:end]

    def run_usb_loop(self, diagnostic):
        harness = r'''
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct udevice { int unused; };
static struct udevice device;
static unsigned long ticks, registered_at;
static int diagnostic;
#define CMD_RET_FAILURE 1
#define CMD_RET_USAGE 2
#define CMD_RET_SUCCESS 0
#define CONFIG_USB_FUNCTION_FASTBOOT 1
#define IS_ENABLED(x) (x)
#define pr_err(...) ((void)0)
#define simple_strtoul(s, e, b) strtoul(s, e, b)
static const char *env_get(const char *name) {
    return diagnostic && !strcmp(name, "rmxdiag.usb_wait") ? "1" : NULL;
}
static unsigned long get_timer(unsigned long base) { return ticks - base; }
static int udc_device_get_by_index(int i, struct udevice **p) { (void)i; *p = &device; return 0; }
static void g_dnl_clear_detach(void) {}
static int g_dnl_register(const char *name) { (void)name; ticks = 30000; registered_at = ticks; return 0; }
static int g_dnl_board_usb_cable_connected(void) { return 1; }
static int g_dnl_detach(void) { return ticks - registered_at >= 15000; }
static int ctrlc(void) { return 0; }
static void schedule(void) { ticks += 1000; }
static void dm_usb_gadget_handle_interrupts(struct udevice *u) { (void)u; }
static void udc_device_put(struct udevice *u) { (void)u; }
static void g_dnl_unregister(void) {}
''' + self.function + r'''
int main(int argc, char **argv) {
    char *args[] = {"usb", "0", NULL};
    diagnostic = argc > 1 && !strcmp(argv[1], "diagnostic");
    if (do_fastboot_usb(2, args, 0, 0) != 0) return 2;
    printf("%lu\n", ticks - registered_at);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            test = Path(directory) / "test.c"
            binary = Path(directory) / "test"
            test.write_text(harness)
            subprocess.run(["cc", "-std=c11", "-Werror=implicit-function-declaration",
                            str(test), "-o", str(binary)], capture_output=True, check=True)
            args = [str(binary)] + (["diagnostic"] if diagnostic else [])
            return int(subprocess.check_output(args, text=True, timeout=3).splitlines()[-1])

    def test_diagnostic_wait_stops_ten_seconds_after_usb_registration(self):
        self.assertEqual(self.run_usb_loop(True), 10000)

    def test_normal_fastboot_keeps_waiting_for_detach(self):
        self.assertEqual(self.run_usb_loop(False), 15000)

    def test_diagnostic_wrapper_returns_to_the_boot_flow_after_usb_wait(self):
        harness = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct cmd_tbl { int unused; };
#define CMD_RET_FAILURE 1
#define CMD_RET_USAGE 2
#define CONFIG_IS_ENABLED(x) 0
#define pr_err(...) ((void)0)
static int diagnostic;
static const char *env_get(const char *name) {
    return diagnostic && !strcmp(name, "rmxdiag.usb_wait") ? "1" : NULL;
}
static uintptr_t hextoul(const char *p, char **end) { return strtoul(p, end, 16); }
static void fastboot_init(void *p, size_t n) { (void)p; (void)n; }
static int do_fastboot_usb(int argc, char *const argv[], uintptr_t p, size_t n) {
    (void)argc; (void)argv; (void)p; (void)n; return 0;
}
static void hang(void) { exit(75); }
''' + self.wrapper + r'''
int main(int argc, char **argv) {
    char *args[] = {"fastboot", "usb", "0", NULL};
    diagnostic = argc > 1 && !strcmp(argv[1], "diagnostic");
    return do_fastboot(NULL, 0, 3, args);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.c"
            binary = Path(directory) / "test"
            source.write_text(harness)
            subprocess.run(["cc", "-std=c11", "-Werror=implicit-function-declaration",
                            str(source), "-o", str(binary)], capture_output=True, check=True)
            diagnostic = subprocess.run([str(binary), "diagnostic"], timeout=3)
            normal = subprocess.run([str(binary)], timeout=3)
            self.assertEqual(diagnostic.returncode, 0)
            self.assertEqual(normal.returncode, 75)


if __name__ == "__main__":
    unittest.main()
