import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class UbootLogfsSlotsTest(unittest.TestCase):
    def test_selects_a_free_slot_then_rotates_only_known_records(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        program = r'''
#include <assert.h>
#include <stdint.h>
#include "uboot_logfs_slots.h"
int main(void) {
    struct rmx_logfs_slot slots[3] = {{1, 1, 0}, {1, 1, 0}, {0, 0, 0}};
    unsigned selected = 99;
    uint64_t generation = 0;
    assert(!rmx_logfs_select(slots, &selected, &generation));
    assert(selected == 2 && generation == 1);
    slots[2] = (struct rmx_logfs_slot){1, 1, 1};
    assert(!rmx_logfs_select(slots, &selected, &generation));
    assert(selected == 0 && generation == 2);
    slots[0].owned = false;
    assert(rmx_logfs_select(slots, &selected, &generation));
    slots[0].owned = true;
    slots[0].generation = UINT64_MAX;
    assert(rmx_logfs_select(slots, &selected, &generation));
    const char legacy[] = "RMX1901_UBOOT_LOG v1\ncandidate=usb-logfs-write-20261028e\n"
                          "console_overflow=0\n\nold console\n";
    const char current[] = "RMX1901_UBOOT_LOG v1\ncandidate=release-1\nconsole_overflow=0\n\n"
                           "RMX1901_LOGFS_FORMAT=2\nsequence=13\nnew console\n";
    const char foreign[] = "RMX1901_UBOOT_LOG v1\ncandidate=foreign\nconsole_overflow=0\n\n"
                           "unrelated data\n";
    assert(rmx_logfs_record_generation(legacy, sizeof(legacy) - 1, 0, &generation));
    assert(generation == 0);
    assert(!rmx_logfs_record_generation(legacy, sizeof(legacy) - 1, 1, &generation));
    assert(rmx_logfs_record_generation(current, sizeof(current) - 1, 2, &generation));
    assert(generation == 13);
    assert(!rmx_logfs_record_generation(foreign, sizeof(foreign) - 1, 2, &generation));
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "slots.c"
            binary = Path(directory) / "slots"
            source.write_text(program)
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            "-I", str(ROOT / "recovery/diagnostics"), str(source),
                            "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
