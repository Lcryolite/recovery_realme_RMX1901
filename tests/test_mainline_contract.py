import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
INIT_RC = ROOT / "recovery" / "root" / "init.recovery.qcom.rc"
MAINLINE_DTS = ROOT / "mainline" / "dts" / "sdm710-realme-rmx1901.dts"
KERNEL_FRAGMENT = ROOT / "mainline" / "kernel.fragment"


class MainlineContractTest(unittest.TestCase):
    def test_recovery_does_not_assume_downstream_ufs_path(self):
        contents = INIT_RC.read_text()

        self.assertNotIn("wait /dev/block/platform/soc/", contents)
        self.assertIn("symlink /dev/block /dev/block/bootdevice", contents)

    def test_device_tree_keeps_vendor_carveouts_and_pstore(self):
        dts = MAINLINE_DTS.read_text()
        fragment = KERNEL_FRAGMENT.read_text()

        for node in (
            "vendor-firmware@88f00000",
            "gpu@98a15000",
            "ramoops@b7e00000",
            "kboot-log@b8200000",
        ):
            self.assertIn(node, dts)

        self.assertIn("&mpss_region", dts)
        self.assertIn("reg = <0 0x8b000000 0 0xda15000>;", dts)
        self.assertIn('qcom,board-id = <0 0>;', dts)
        self.assertIn('gpio = <&pm660_gpios 12 GPIO_ACTIVE_HIGH>;', dts)
        self.assertIn("CONFIG_PSTORE_CONSOLE=y", fragment)
        self.assertIn("CONFIG_PSTORE_PMSG=y", fragment)


if __name__ == "__main__":
    unittest.main()
