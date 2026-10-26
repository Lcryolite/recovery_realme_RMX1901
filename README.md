# recovery_device_realme_RMX1901
Recovery tree for realme X

## Features

Works:

 - Everything

## Compile

First checkout manifest :

```
repo init --depth=1 -u https://github.com/minimal-manifest-twrp/platform_manifest_twrp_aosp.git -b twrp-12.1
repo sync -c
```

Then clone the current device tree onto device/realme/RMX1901


Finally execute these:

```
. build/envsetup.sh
lunch twrp_RMX1901-eng
mka recoveryimage
```

The recovery build pins the released `stock-next` BakaSU kernel payload from
`kernel_realme_sdm710` commit `660c54270c9ddff443a652fb224dd249f27b8b8b`
with BakaSU commit `39faf94a2d4a40d48093398bafcde649916edbc1`.
The checked-in `prebuilt/Image.gz-dtb` combines that payload with the existing
Recovery DTB group; the independent Recovery DTBO is preserved.
Recovery uses an LZMA-Alone ramdisk, enabled by `BOARD_RAMDISK_USE_LZMA`
and `OF_USE_LZMA_COMPRESSION`. The kernel supports LZMA, but not LZ4 or XZ.
This also fits the loader workspace observed on RMX1901EX_11.C.02:
`0x05600000 - 0x00080000 - ALIGN(ramdisk_size, 4096) - 0x00200000 - 8192`.
The gzip candidate left 57,724,928 bytes for a kernel whose ARM64 header
requires 60,030,976 bytes, exceeding this ABL limit.
The image guard checks the embedded IKCONFIG, ARM64 runtime size, and this
firmware-specific workspace before accepting a built image. Other bootloader
layouts remain `unverified`.
Keep the prebuilt payload and `RECOVERY_KERNEL_SHA256` pin in sync rather than
downloading from a moving kernel branch during CI.
Validate the complete image and its AVB metadata before device testing.
A new kernel/DTB combination remains `unverified` until the complete recovery
image boots into an interactive interface with working touch.

To test it:

```
fastboot flash /path/to/recovery.img
```

## Terminal Rescue Tools & Usage Guide

The recovery environment includes built-in rescue tools accessible via ADB Shell or the Recovery Terminal:

### 1. Android Binary XML (ABX) Toolchain
Android 12–16 stores system configurations (e.g. `packages.xml`, `settings_global.xml`, runtime permissions) in Binary XML (`ABX\0`) format. Use the built-in ABX tools to inspect or edit them:

```sh
# Check if a file is in ABX format
abx-tool info /data/system/packages.xml

# Decode ABX to human-readable XML
abx2xml /data/system/packages.xml /tmp/packages.xml
# Or using abx-tool directly:
abx-tool decode /data/system/users/0/settings_global.xml /tmp/settings.xml

# Encode modified XML back to ABX binary
xml2abx /tmp/packages.xml /data/system/packages.xml
```

### 2. Atomic /proc Node Writing
To prevent vendor touchpanel/kernel proc drivers from interpreting split stream chunks as distinct commands, a buffered `printf` wrapper and `safe_write_proc` helper are loaded by default in the shell:

```sh
# Safe single-syscall write to vendor proc nodes
safe_write_proc /proc/touchpanel/tp_fw_update 0
```

### 3. EFS & Baseband Disaster Recovery
EFS partitions (`modemst1`, `modemst2`, `fsg`, `fsc`, `oppostanvbk`) and Modem/DSP firmware are mapped in `twrp.flags` and included in OrangeFox Quick Backup (`OF_QUICK_BACKUP_LIST`) for one-click backup and restore.

## Note about ozip decrypt
* This is necessary for downgrades back to stock android-9.0 (ColorOS).
* Early versions of android-10.0(Realme UI v1) have decryptor built into the updater binary so this patch isnt necessary.
* Later versions of android-10.0(Realme UI v1) are not ozip encrypted at all and so is android-11.0 (Realme UI v2) potentially following the OPLUS merger.
* To automate renaming .ozip to .zip, ozip decrypt tool can be used with dummy key for devices launching with android 10 and above.

## Credits

- Thanks to @mauronfrio for the TWRP tree for realme X/XT
- TWRP team
