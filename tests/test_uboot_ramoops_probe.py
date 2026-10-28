from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).parents[1]
HEADER = ROOT / "recovery/diagnostics/uboot_ramoops_probe.h"


class UBootRamoopsProbeTest(unittest.TestCase):
    def test_reads_linear_and_wrapped_markers_without_changing_the_buffer(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        harness = r'''
#include <stdint.h>
#include <string.h>
typedef uint32_t u32;
typedef uint8_t u8;
#include "uboot_ramoops_probe.h"
int main(void) {
    union { u32 alignment; u8 bytes[RMX1901_CONSOLE_SIZE]; } memory, original;
    struct rmx1901_persistent_buffer *p = (void *)memory.bytes;
    const char marker[] = RMX1901_PROBE_MARKER;
    memset(&memory, 0, sizeof(memory));
    if (rmx1901_console_has_probe(p) != -1) return 1;
    p->sig = RMX1901_PERSISTENT_SIG;
    p->size = sizeof(marker) + 8;
    p->start = p->size;
    memcpy(p->data + 3, marker, sizeof(marker) - 1);
    memcpy(&original, &memory, sizeof(memory));
    if (rmx1901_console_has_probe(p) != 1 || memcmp(&original, &memory, sizeof(memory))) return 2;
    memset(p->data, 'x', p->size);
    p->start = 7;
    for (unsigned i = 0; i < sizeof(marker) - 1; i++)
        p->data[(p->start + i) % p->size] = marker[i];
    memcpy(&original, &memory, sizeof(memory));
    if (rmx1901_console_has_probe(p) != 1 || memcmp(&original, &memory, sizeof(memory))) return 3;
    p->data[p->start] = 'x';
    if (rmx1901_console_has_probe(p) != 0) return 4;
    p->size = RMX1901_CONSOLE_SIZE;
    if (rmx1901_console_has_probe(p) != -1) return 5;
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / "test.c", Path(directory) / "test"
            source.write_text(harness)
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(HEADER.parent),
                            str(source), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
