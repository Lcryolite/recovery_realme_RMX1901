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

## Note about ozip decrypt
* This is necessary for downgrades back to stock android-9.0 (ColorOS).
* Early versions of android-10.0(Realme UI v1) have decryptor built into the updater binary so this patch isnt necessary.
* Later versions of android-10.0(Realme UI v1) are not ozip encrypted at all and so is android-11.0 (Realme UI v2) potentially following the OPLUS merger.
* To automate renaming .ozip to .zip, ozip decrypt tool can be used with dummy key for devices launching with android 10 and above.

## Credits

- Thanks to @mauronfrio for the TWRP tree for realme X/XT
- TWRP team
