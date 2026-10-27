import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import create_sdsp_8m_candidate as comparison
import recovery_image_guard as guard

COMPARISON_SHA256 = "82093020d41d697f0b3ee82d9719748100f367a3770873ec338b1a3d2022df4d"


class SdspComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = (ROOT / "prebuilt" / "Image.gz-dtb").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            cls.comparison = comparison.make_8m_kernel(
                cls.original, COMPARISON_SHA256, Path(directory)
            )

    def test_matches_the_source_built_8m_dtbs(self):
        self.assertEqual(hashlib.sha256(self.comparison).hexdigest(), COMPARISON_SHA256)
        old_gzip, old_dtbs = guard.split_kernel_dtb(self.original)
        new_gzip, new_dtbs = guard.split_kernel_dtb(self.comparison)
        self.assertEqual(old_gzip, new_gzip)
        self.assertEqual(guard.validate_dtb_group(old_dtbs), guard.validate_dtb_group(new_dtbs))
        differences = [(a, b) for a, b in zip(old_dtbs, new_dtbs) if a != b]
        self.assertEqual(differences, [(0x40, 0x80)] * 4)

    def test_rejects_already_changed_sdsp_pools(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(guard.ValidationError, "does not have a 4 MiB"),
        ):
            comparison.make_8m_kernel(self.comparison, COMPARISON_SHA256, Path(directory))

    def test_rejects_an_unexpected_8m_kernel_hash(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(guard.ValidationError, "SHA-256"),
        ):
            comparison.make_8m_kernel(self.original, "0" * 64, Path(directory))

    def test_avb_validation_rejects_a_misleading_filename(self):
        with self.assertRaisesRegex(guard.ValidationError, "named recovery.img"):
            comparison.verify_avb(Path("avbtool.py"), Path("other.img"), Path("key.pem"))


if __name__ == "__main__":
    unittest.main()
