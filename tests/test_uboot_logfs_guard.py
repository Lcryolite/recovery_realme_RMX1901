from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
HEADER = ROOT / "recovery/diagnostics/uboot_logfs_guard.h"


class UBootLogfsGuardTest(unittest.TestCase):
    def test_writes_require_one_matching_fat_partition_with_verified_geometry(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        code = r'''
#include "uboot_logfs_guard.h"
int main(void) {
    if (!rmx1901_logfs_target_valid(1, 4, 31, 0, 4, 652177, 2048, 4096, 1)) return 1;
    if (rmx1901_logfs_target_valid(0, 4, 31, 0, 4, 652177, 2048, 4096, 1)) return 2;
    if (rmx1901_logfs_target_valid(2, 4, 31, 0, 4, 652177, 2048, 4096, 1)) return 3;
    if (rmx1901_logfs_target_valid(1, 3, 31, 0, 4, 652177, 2048, 4096, 1)) return 4;
    if (rmx1901_logfs_target_valid(1, 4, 30, 0, 4, 652177, 2048, 4096, 1)) return 5;
    if (rmx1901_logfs_target_valid(1, 4, 31, 0, 4, 652178, 2048, 4096, 1)) return 6;
    if (rmx1901_logfs_target_valid(1, 4, 31, 0, 4, 652177, 2049, 4096, 1)) return 7;
    if (rmx1901_logfs_target_valid(1, 4, 31, 0, 4, 652177, 2048, 512, 1)) return 8;
    if (rmx1901_logfs_target_valid(1, 4, 31, 0, 4, 652177, 2048, 4096, 0)) return 9;
    if (rmx1901_logfs_target_valid(1, 4, 31, 1, 4, 652177, 2048, 4096, 1)) return 10;
    if (rmx1901_logfs_target_valid(1, 4, 31, 0, 3, 652177, 2048, 4096, 1)) return 11;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / "test.c", Path(directory) / "test"
            source.write_text(code)
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent),
                            str(source), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
