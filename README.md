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

The recovery build intentionally uses the checked-in A17 ReSukiSU
`prebuilt/Image.gz-dtb`. Do not replace it from a moving kernel branch during
CI: that makes the same recovery commit produce different, untested images.
When updating the kernel, boot-test the complete recovery image and update the
`RECOVERY_KERNEL_SHA256` value in the OrangeFox workflow at the same time.

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
