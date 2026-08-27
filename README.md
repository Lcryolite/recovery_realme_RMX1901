# recovery_device_realme_RMX1901
Recovery tree for realme X (RMX1901)

The `mainline-7.x` branch builds an OrangeFox recovery around the official
Linux v7.2 commit `8d3ae59288f1e7d58d76558a6ee96d533bc5019f`. The device DTS,
kernel configuration fragment, and kernel build script are under `mainline/`.

## Status

The DTS compiles against Linux v7.2 and the repository image-guard tests pass.
Recovery boot, UFS crypto, display, touch, and the complete UI remain pending
real-device validation.

## Compile

First checkout the OrangeFox manifest:

```
repo init --depth=1 -u https://github.com/minimal-manifest-twrp/platform_manifest_twrp_aosp.git -b twrp-12.1
repo sync -c
```

Then clone this device tree into `device/realme/RMX1901`, and check out the
pinned mainline kernel:

```
git clone -b mainline-7.x https://github.com/Lcryolite/recovery_realme_RMX1901.git device/realme/RMX1901
git clone --filter=blob:none --no-checkout https://github.com/torvalds/linux.git linux
git -C linux fetch --depth=1 origin 8d3ae59288f1e7d58d76558a6ee96d533bc5019f
git -C linux checkout --detach 8d3ae59288f1e7d58d76558a6ee96d533bc5019f
device/realme/RMX1901/mainline/build_kernel.sh "$PWD/linux"
```


Finally execute these:

```
. build/envsetup.sh
lunch twrp_RMX1901-eng
mka recoveryimage
```

The kernel script copies the board DTS into the Linux source, applies
`mainline/kernel.fragment`, builds `Image.gz` plus the DTB, and generates
`prebuilt/Image.gz-dtb`. This branch does not use a checked-in `dtbo.img` or a
recovery DTBO; the recovery image contains the mainline DTB appended to the
kernel image.

To test it:

```
fastboot boot /path/to/recovery.img
```

This branch has passed static DTS compilation and repository-level image-guard
tests. A real-device boot test is still required. Linux v7.2 does not currently
include the RMX1901 AMOLED panel driver or the S3706 touch driver, so the board
description initially relies on the bootloader-retained simple framebuffer and
should be treated as a bring-up baseline. UFS crypto, display, touch, and
recovery UI behavior remain unverified until hardware testing. Mainline v7.2
does not provide the Android downstream MTP gadget; recovery USB requests for
MTP therefore fall back to ADB, while configfs mass storage is enabled.

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
