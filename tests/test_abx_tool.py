#!/usr/bin/env python3
"""Unit tests for Android Binary XML (ABX) conversion and recovery rescue tools."""

import sys
import tempfile
import unittest
from pathlib import Path

# Import the abx_tool module
MODULE_PATH = Path(__file__).parents[1] / "tools" / "abx_tool.py"
sys.path.insert(0, str(MODULE_PATH.parent))

try:
    import abx_tool
except ImportError:
    abx_tool = None


class ABXToolTest(unittest.TestCase):
    def setUp(self):
        if abx_tool is None:
            self.fail("tools/abx_tool.py could not be imported")

    def test_magic_detection(self):
        abx_header = b"ABX\x00\x00\x01"
        xml_header = b"<?xml version=\"1.0\" encoding=\"utf-8\"?>\n<packages>"
        random_bytes = b"\x00\x01\x02\x03"

        self.assertTrue(abx_tool.is_abx(abx_header))
        self.assertFalse(abx_tool.is_abx(xml_header))
        self.assertFalse(abx_tool.is_abx(random_bytes))

    def test_encode_and_decode_simple_xml(self):
        original_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<packages version="33">\n'
            '    <package name="com.android.settings" codePath="/system/priv-app/Settings" flags="1" enabled="true">\n'
            "    </package>\n"
            "</packages>"
        )

        abx_bytes = abx_tool.xml2abx(original_xml)
        self.assertTrue(abx_bytes.startswith(b"ABX\x00"))

        decoded_xml = abx_tool.abx2xml(abx_bytes)
        self.assertIn("<packages", decoded_xml)
        self.assertIn('name="com.android.settings"', decoded_xml)
        self.assertIn('flags="1"', decoded_xml)
        self.assertIn('enabled="true"', decoded_xml)

    def test_typed_attribute_round_trip(self):
        sample_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<config>\n'
            '    <item id="100" count="5000000000" ratio="3.14" active="true" inactive="false" hex="0x1A">\n'
            "        Some text content\n"
            "    </item>\n"
            "</config>"
        )

        abx_data = abx_tool.xml2abx(sample_xml)
        decoded_xml = abx_tool.abx2xml(abx_data)

        self.assertIn('id="100"', decoded_xml)
        self.assertIn('count="5000000000"', decoded_xml)
        self.assertIn('active="true"', decoded_xml)
        self.assertIn('inactive="false"', decoded_xml)
        self.assertIn("Some text content", decoded_xml)

    def test_xml_attribute_and_text_escaping(self):
        sample_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<root title="Rock &amp; Roll &quot;Special&quot;" note="a &lt; b">\n'
            "    1 &lt; 2 &amp; 3 &gt; 0\n"
            "</root>"
        )
        abx_data = abx_tool.xml2abx(sample_xml)
        decoded_xml = abx_tool.abx2xml(abx_data)

        self.assertIn("&amp;", decoded_xml)
        self.assertIn("&lt;", decoded_xml)

    def test_cli_file_conversion(self):
        xml_content = '<root key="value"><child index="1"/></root>'

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            xml_file = tmp_path / "test.xml"
            abx_file = tmp_path / "test.abx"
            restored_xml_file = tmp_path / "restored.xml"

            xml_file.write_text(xml_content, encoding="utf-8")

            # CLI Encode
            exit_code = abx_tool.main(["encode", str(xml_file), str(abx_file)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(abx_file.exists())
            self.assertTrue(abx_tool.is_abx(abx_file.read_bytes()))

            # CLI Decode
            exit_code = abx_tool.main(["decode", str(abx_file), str(restored_xml_file)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(restored_xml_file.exists())
            self.assertIn('<child index="1"', restored_xml_file.read_text(encoding="utf-8"))

    def test_rejects_corrupted_abx(self):
        corrupted = b"ABX\x00\xff\xff\xff"
        with self.assertRaises(ValueError):
            abx_tool.abx2xml(corrupted)


if __name__ == "__main__":
    unittest.main()
