import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class UbootLogfsWriterTest(unittest.TestCase):
    def test_writes_only_owned_slots_and_one_shot_cannot_repeat(self):
        if not shutil.which("cc"):
            self.skipTest("C compiler unavailable")
        program = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#define CONFIG_RMX1901_FAT_EXCLUSIVE_TEST 1
#define ARCH_DMA_MINALIGN 64
#define GD_FLG_RECORD_OVF 0x2000
#define FS_TYPE_FAT 1
#define SCSI_SYNC_CACHE 0x35
#define DMA_NONE 3
typedef unsigned long ulong;
#include "uboot_logfs_slots.h"
struct membuf { const char *data; int used; };
struct global { int flags; struct membuf console_out; };
static struct global global_data, *gd = &global_data;
struct blk_desc { unsigned char target, lun; };
struct udevice { struct udevice *parent; struct blk_desc *desc; };
struct scsi_cmd { unsigned char cmd[16], cmdlen, target, lun, dma_dir; };
struct file { char data[32768]; size_t size; bool exists; };
static struct file files[3];
static const char *replacement;
static int writes;
static int index_of(const char *name) {
    const char *names[] = {"RMXLOG01.LOG", "RMXLOG02.LOG", "RMXLOG03.LOG"};
    for (int i = 0; i < 3; i++) if (!strcmp(name, names[i])) return i;
    return -1;
}
static const char *env_get(const char *key) { (void)key; return "usb-fixture"; }
static void *dev_get_uclass_plat(struct udevice *d) { return d->desc; }
static void *memalign(size_t alignment, size_t size) {
    void *p = NULL; return posix_memalign(&p, alignment, size) ? NULL : p;
}
static int membuf_avail(struct membuf *b) { return b->used; }
static int membuf_get(struct membuf *b, char *out, int size) {
    int n = b->used < size ? b->used : size;
    memcpy(out, b->data, (size_t)n); b->data += n; b->used -= n; return n;
}
static int fs_set_blk_dev_with_part(struct blk_desc *d, int p) { (void)d; (void)p; return 0; }
static int fs_get_type(void) { return FS_TYPE_FAT; }
static void fs_close(void) {}
static int fs_exists(const char *name) { int i = index_of(name); return i >= 0 && files[i].exists; }
static int fs_size(const char *name, loff_t *size) {
    int i = index_of(name); if (i < 0 || !files[i].exists) return -1;
    *size = (loff_t)files[i].size; return 0;
}
static int fs_read(const char *name, ulong addr, loff_t offset, loff_t size, loff_t *read_size) {
    int i = index_of(name);
    if (i < 0 || !files[i].exists || offset || size != (loff_t)files[i].size) return -1;
    memcpy((void *)addr, files[i].data, (size_t)size); *read_size = size; return 0;
}
void rmx_logfs_allow_replace(const char *name) { replacement = name; }
static int fs_write(const char *name, ulong addr, loff_t offset, loff_t size, loff_t *written) {
    int i = index_of(name);
    if (i < 0 || offset || size <= 0 || size > 32768 ||
        (files[i].exists && (!replacement || strcmp(name, replacement)))) return -1;
    memcpy(files[i].data, (void *)addr, (size_t)size);
    files[i].size = (size_t)size; files[i].exists = true; *written = size; writes++; return 0;
}
static int scsi_exec(struct udevice *d, struct scsi_cmd *cmd) {
    (void)d;
    return cmd->cmd[0] == SCSI_SYNC_CACHE && cmd->cmdlen == 10 &&
           cmd->target == 0 && cmd->lun == 4 && cmd->dma_dir == DMA_NONE ? 0 : -1;
}
#include "uboot_logfs_writer.h"
static void put(int slot, const char *data) {
    files[slot].size = strlen(data); memcpy(files[slot].data, data, files[slot].size);
    files[slot].exists = true;
}
int main(void) {
    static const char first[] = "RMX1901_UBOOT_LOG v1\ncandidate=usb-logfs-write-20261028e\n"
                                "console_overflow=0\n\nfirst console\n";
    static const char second[] = "RMX1901_UBOOT_LOG v1\ncandidate=usb-logfs-fixed-20261028g\n"
                                 "console_overflow=0\n\nsecond console\n";
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_01)
    static const char third[] = "RMX1901_UBOOT_LOG v1\ncandidate=prior-attempt\nconsole_overflow=0\n\n"
                                "RMX1901_LOGFS_FORMAT=2\nsequence=1\nthird console\n";
#endif
    const char console[] = "current boot console\n";
    struct blk_desc desc = { .target = 0, .lun = 4 };
    struct udevice parent = { 0 }, disk = { .parent = &parent, .desc = &desc };
    uint64_t generation = 0;
    put(0, first); put(1, second);
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_01)
    put(2, third);
#endif
    global_data.console_out = (struct membuf){console, sizeof(console) - 1};
    assert(!rmx1901_logfs_test_write(&disk, 31));
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_01)
    assert(writes == 1 && replacement == NULL);
    assert(rmx_logfs_record_generation(files[0].data, files[0].size, 0, &generation) && generation == 2);
    assert(files[1].size == sizeof(second) - 1 && !memcmp(files[1].data, second, sizeof(second) - 1));
    assert(files[2].size == sizeof(third) - 1 && !memcmp(files[2].data, third, sizeof(third) - 1));
    assert(rmx1901_logfs_test_write(&disk, 31));
    assert(writes == 1 && replacement == NULL);
#else
    assert(writes == 1 && files[2].exists && replacement == NULL);
    assert(files[0].size == sizeof(first) - 1 && !memcmp(files[0].data, first, sizeof(first) - 1));
    assert(files[1].size == sizeof(second) - 1 && !memcmp(files[1].data, second, sizeof(second) - 1));
    assert(rmx_logfs_record_generation(files[2].data, files[2].size, 2, &generation) && generation == 1);
    assert(global_data.console_out.used == sizeof(console) - 1);
#if defined(CONFIG_RMX1901_LOGFS_ONESHOT_03)
    assert(rmx1901_logfs_test_write(&disk, 31));
    assert(writes == 1 && replacement == NULL);
#else
    assert(!rmx1901_logfs_test_write(&disk, 31));
    assert(writes == 2 && replacement == NULL);
    assert(rmx_logfs_record_generation(files[0].data, files[0].size, 0, &generation) && generation == 2);
    assert(files[1].size == sizeof(second) - 1 && !memcmp(files[1].data, second, sizeof(second) - 1));
    put(1, "foreign file");
    assert(rmx1901_logfs_test_write(&disk, 31));
    assert(writes == 2 && !memcmp(files[1].data, "foreign file", sizeof("foreign file") - 1));
#endif
#endif
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "writer.c"
            source.write_text(program)
            for mode in ("rolling", "slot03", "slot01"):
                binary = Path(directory) / mode
                cmd = ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                       "-I", str(ROOT / "recovery/diagnostics")]
                if mode == "slot03":
                    cmd.append("-DCONFIG_RMX1901_LOGFS_ONESHOT_03=1")
                elif mode == "slot01":
                    cmd.append("-DCONFIG_RMX1901_LOGFS_ONESHOT_01=1")
                subprocess.run([*cmd, str(source), "-o", str(binary)], check=True, capture_output=True)
                subprocess.run([str(binary)], check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
