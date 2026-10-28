# Recovery diagnostics on RMX1901

The Recovery kernel enables `/dev/mem` with the kernel's strict range filter
disabled. The node is `0600 root:root`; access also requires `CAP_SYS_RAWIO`.
Hardware, secure firmware or the architecture can still reject a range. Use a
controlled allocation for write tests; a read or write to a device register
may itself change hardware state.

Recovery userspace run logging is disabled: the Recovery log stream is sent to
`/dev/null`, the Recovery `logd` service is not started, and automatic logcat
snapshots and Recovery log copies are disabled. A temporary kernel `dmesg`
file used by panel detection remains. The existing Copy Log action can still
export the kernel log; it reports that Recovery logcat is unavailable.

The `rmx1901-diagnostics` service waits for `/cache` and pstore to be mounted,
then saves exposed pstore crash, console and pmsg records without removing
the source. Complete snapshots have raw files and `records.json` entries;
an interrupted snapshot may lack its manifest. Unknown origins remain unknown.
Recovery kernel messages are captured from `/dev/kmsg`
with userspace-injected records excluded and a sync attempted about once a
second. Kernel debug messages are excluded by default. To include them for
the current Recovery session, create a root-owned `0600` file containing
`1` and a newline at `/tmp/rmx1901-diagnostics.debug`; remove it to turn the
option off. The service checks this flag about once per second. Including
debug in the pstore console may also require raising the kernel console log
level; both settings apply only to the current Recovery boot. A reset, write
failure or power loss can lose unsynced data.
After each successful sync, a sequence checkpoint records the last complete
kernel record. On a service restart within the same boot, later records are
replayed from `/dev/kmsg` and earlier records are skipped. An uncheckpointed
tail or a gap in the available sequence is marked partial; if the ring has
already overwritten the missing data, replay cannot recover it.

The private archive is `/cache/recovery/rmx1901-diagnostics`. Its `current`,
`previous`, and `oldest` directories contain at most three boot sessions. A
session reserves up to 3 MiB for pstore, 1 MiB for the latest kernel stream,
128 KiB for one host-captured U-Boot USB transcript, and up to three 32 KiB
U-Boot records from `logfs`. Bounded metadata
keeps the total under 16 MiB. Only recognized files created by this feature
are rotated. An unknown file stops rotation and reports an error rather than
being deleted. Kernel and USB truncation retain the tail
and are marked. Raw pstore capture prioritizes crash records, then console,
then pmsg; truncated records are marked in `records.json`.
If the crash, console and pmsg areas together exceed the 3 MiB pstore limit,
part of pmsg can be omitted and the session reports a partial capture.
An exposed zero-byte pstore record is also reported as partial, rather than
being presented as saved evidence.

Space is reserved for a pending pstore snapshot before older sessions are
rotated. If an interrupted pending snapshot contains raw evidence, it is
preserved as an `incomplete` session rather than deleted, and the next session
reports `reason=pstore_interrupted`. Its raw files may be partial and their
boot origin remains unknown. An interrupted snapshot containing no record
bytes does not take an additional history slot, but a durable warning remains
until it can be transferred to a session, including after a failed retry.

Before Recovery unmounts or wipes `/cache`, it syncs that filesystem, asks
Android init to stop the archive service, and waits for its files to close.
Android init may force-stop the service, so recent uncheckpointed records
still depend on `/dev/kmsg` replay. Recovery restarts the service afterwards;
the service waits for the cache to be mounted and recreates `/cache/recovery`
if a wipe removed it. If stopping fails, the cache operation is aborted. These
changes require validation on the source-built Recovery image.

At startup the service checks the expected 8 MiB, 4 KiB-sector `logfs` device,
mounts it read-only, and copies recognized `RMXLOG01.LOG` through
`RMXLOG03.LOG` records into the current session. The originals are left in
place. Each copy has `uboot_logfs` metadata and an unverified boot-instance
flag; an invalid record, a reported console-buffer overflow, or an
unavailable device is reported as a partial archive. This reader does not
itself create or rotate files on `logfs`.

`/tmp/rmx1901-diagnostics.status` is available through root ADB. `state=ok`
means that the archive is operating; `reason=pstore_empty` explicitly means
no previous pstore record was exposed. `state=pending` means the service is
waiting for mounts or its first sync. `state=partial` indicates truncation or
an incomplete USB import; `state=error` indicates that preservation failed.
The Recovery interface displays a dismissible warning for partial or error
states and for an unavailable status. The service retries unavailable mounts
without blocking Recovery. Actual GUI presentation requires testing on the
source-built candidate.

## U-Boot USB collection

The diagnostic U-Boot profile uses a unique attempt nonce. Start the collector
before manually selecting Start on the phone:

```sh
python3 tools/collect_uboot_console.py --manifest /path/to/manifest.json \
  --output /path/to/new-capture --timeout 240 \
  --relay-serial DEVICE_SERIAL --relay-timeout 240
```

The host records the USB transcript in a new private directory and verifies
the candidate image hash, gadget identity, attempt marker and transcript
hash. With `--relay-serial`, it waits for the owner to enter Recovery, checks
that the diagnostic session matches the current kernel boot ID, and imports
the verified transcript into that session. It does not reboot or flash the
phone. The imported `uboot-usb.json` records the host-reported hash and marks
`boot_instance_verified` false: attributing the capture to a particular boot
also requires the flash and manual Start evidence. The collector can be run
without the relay, or a previously captured transcript can be imported using
`tools/relay_uboot_console.py`. A session accepts one USB transcript; further
captures remain on the host if the destination is already occupied.

This host-assisted path does not demonstrate that logs survived in the phone
through fastboot. It cannot record stages before U-Boot executes or before a
usable recording buffer or USB channel exists. The diagnostic USB wait is
limited to 10 seconds after USB registration; the normal fastboot path has no
diagnostic timeout. The tested observation source deliberately hangs after
its preboot command, and its `bootcmd` is empty; it does not establish
continued OS boot. A boot-capable U-Boot requires a separate source and
startup-chain validation.

## U-Boot retention build

The U-Boot patches target the RMX1901 observation source used for these
tests. Apply `uboot-fastboot-observation-timeout.patch`,
`uboot-rmx1901-ramoops-readonly-probe.patch`, `uboot-ufs-nodata-cache.patch`,
and `uboot-rmx1901-logfs-retention.patch` in that order. Copy the three
`recovery/diagnostics/uboot_logfs_*.h` files into U-Boot's `include/` directory,
renaming `uboot_logfs_guard.h` to `rmx1901_logfs_guard.h`. The source tree has
no verified upstream revision; applying the patches to a different tree
requires a fresh review.

The retention diagnostic requires the RMX1901 UFS/SCSI and FAT options,
console recording, `CONFIG_RMX1901_UFS_READONLY_PROBE`,
`CONFIG_RMX1901_FAT_EXCLUSIVE_TEST` and
`CONFIG_RMX1901_LOGFS_WRITE_TEST`. It validates the discovered partition and
the ownership of all three slots before writing. It selects an unused slot
first, otherwise the lowest sequence, and stops when a slot is unrecognized or
the sequence is exhausted. The FAT guard permits replacing only the selected
recognized slot. Diagnostic one-shot options limit the reviewed device tests
to one new slot or one legacy-slot replacement, including after reboot.
The on-device file is a single snapshot taken during the UFS probe before the
USB observation loop. It does not include output produced later in that loop
or by a subsequent `bootcmd`; those stages require the live USB transcript or
a separately validated later snapshot. The USB capture also cannot guarantee
output after its connection is lost.

This code changes FAT metadata when enabled. The current device approvals
cover the named one-shot experiments only; enabling automatic rotation and
flashing any new image require the owner's separate authorization. The
upstream U-Boot source, configuration and final boot payload must be recorded
for every new build. A filesystem check and a backup do not establish the
unknown OEM uses of `logfs` or guarantee consistency after interrupted writes.

## Observations and remaining work

On 2026-10-28 the owner tested a Recovery kernel candidate on one RMX1901:
display, touch and storage listing worked, and a controlled pinned RAM page
passed `/dev/mem` read, write, mmap and `CAP_SYS_RAWIO` checks. The current
ramoops layout has five 256 KiB crash slots; `devinfo-size` also affects the
console offset. Earlier direct kernel writes that overwrote the previous
console were removed.

The U-Boot USB console was captured on the same device. A marker was verified
in the Recovery kernel ring and physical ramoops console before a subsequent
Recovery-to-fastboot-to-U-Boot test. The U-Boot read-only probe found the
mapped console header empty or invalid. This bounds the loss to that path but
does not identify the responsible firmware or boot phase. An initial U-Boot
USB configuration had no UFS/SCSI driver. A later single-lane read-only UFS
probe found one block device, although interconnect and reference-clock
warnings remain. A subsequent read-only probe found one `logfs` FAT partition
at U-Boot device 4, partition 31, matching Recovery's observed location and
size. The device cache is F2FS. Existing `logfs` and `logdump` partitions
both contain nonzero data; Recovery's fstab also maps `logdump` to `/metadata`.
An 8 MiB `logfs` backup was taken before and matched after the read-only probe.
Its FAT12 volume has about 5 MiB free, but a read-only `fsck.fat` reported a
missing boot-sector volume label and exited with status 1. The volume's owner
and future use are unknown. Neither its free space nor any all-zero range is
authorization to write or format it.

With separate owner approval, one diagnostic U-Boot candidate created a
1,159-byte `RMXLOG01.LOG` in `logfs`. Recovery read the uniquely marked record
after a manual fastboot transition. The post-test image differs from the
backup only in four 4 KiB blocks covering the two FATs, a root-directory block
and the new file data; read-only `fsck.fat` reports the same pre-existing label
warning. This is one observed retention path, not a durability guarantee or
an automatic, repeatable archival implementation. USB did not enumerate in
that attempt, so its UFS cache-sync result remains unverified. A later,
separately approved no-file-write candidate skipped cache maintenance for
zero-length UFS transfers: it reported successful TEST UNIT READY, SYNCHRONIZE
CACHE and readback of the existing file over USB. The partition hash was
unchanged after this comparison. This supports, but does not independently
prove, the zero-length cache-operation explanation for the first USB failure.
A second approved one-shot candidate using that fix created `RMXLOG02.LOG`,
reported a successful sync and readback over USB, and the owner then entered
Recovery through fastboot. Both new and previous records matched their saved
hashes; the read-only FAT check reported the same pre-existing warning. Each
candidate writes only once and refuses to replace an existing file. Repeated
automatic retention, crash interruption and power-loss durability remain
unverified. A third approved one-shot candidate recognized both earlier
records and created slot 3 with sequence 1. Its USB output reported successful
sync and readback; all three records matched after Recovery was entered through
fastboot, and the same pre-existing FAT warning remained. The rotation of an
existing on-device slot remains unverified. Do not repurpose partitions based
on their names.

The new ARM archive program was also run against all three retained records
using an isolated `/tmp` fixture. It copied each record with matching hashes and
left `logfs` unchanged. Its integration into the source-built Recovery and
the real `/cache` service path still require device validation.

Panic persistence, the complete source-built Recovery, its UI warning, and
the host-to-device relay need their own target validation. A controlled panic,
each image flash or restoration, and any bootloader or partition change require
the owner's separate approval. The inspected kernel tree has no pstore/blk
backend or identified UFS panic-write path; the ordinary kernel stream saved
before a crash is not a substitute for a persisted panic dump. Ubuntu Touch
adaptation and an Android panic test are outside the current scope.
