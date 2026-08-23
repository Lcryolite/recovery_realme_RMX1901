#!/usr/bin/env python3
"""Unit tests for Android Binary XML (ABX) conversion and recovery rescue tools."""

import io
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

# Import the abx_tool module
MODULE_PATH = Path(__file__).parents[1] / "tools" / "abx_tool.py"
sys.path.insert(0, str(MODULE_PATH.parent))

try:
    import abx_tool
except ImportError:
    abx_tool = None


def make_abx(write_body):
    out = io.BytesIO()
    writer = abx_tool.FastDataOutput(out)
    writer.write_bytes(abx_tool.ABX_MAGIC)
    writer.write_byte(abx_tool.START_DOCUMENT | (abx_tool.TYPE_NULL << 4))
    write_body(writer)
    writer.write_byte(abx_tool.END_DOCUMENT | (abx_tool.TYPE_NULL << 4))
    return out.getvalue()


def write_element(writer, start="root", end="root"):
    writer.write_byte(abx_tool.START_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
    writer.write_interned_utf(start)
    writer.write_byte(abx_tool.END_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
    writer.write_interned_utf(end)


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

    def test_namespace_round_trip_is_well_formed(self):
        sample_xml = (
            '<root xmlns:a="http://schemas.android.com/apk/res/android">'
            '<a:item a:name="value"/>'
            "</root>"
        )

        decoded_xml = abx_tool.abx2xml(abx_tool.xml2abx(sample_xml))
        root = ET.fromstring(decoded_xml)

        self.assertEqual(root[0].tag, "{http://schemas.android.com/apk/res/android}item")
        self.assertEqual(root[0].attrib["{http://schemas.android.com/apk/res/android}name"], "value")
        self.assertIn('xmlns:android="http://schemas.android.com/apk/res/android"', decoded_xml)

    def test_unicode_names_round_trip(self):
        sample_xml = '<根 属性="值"><子/><xmlns/></根>'

        decoded_xml = abx_tool.abx2xml(abx_tool.xml2abx(sample_xml))
        root = ET.fromstring(decoded_xml)

        self.assertEqual(root.tag, "根")
        self.assertEqual(root.attrib["属性"], "值")
        self.assertEqual(root[0].tag, "子")
        self.assertEqual(root[1].tag, "xmlns")

    def test_rejects_unsupported_namespace(self):
        sample_xml = '<root xmlns:custom="urn:custom" custom:value="x"/>'

        with self.assertRaises(ValueError):
            abx_tool.xml2abx(sample_xml)

    def test_preserves_top_level_comments_and_processing_instructions(self):
        sample_xml = '<!--before--><root><?inside data?><!--child--></root><!--after--><?outside done?>'

        decoded_xml = abx_tool.abx2xml(abx_tool.xml2abx(sample_xml))

        self.assertIn("<!--before-->", decoded_xml)
        self.assertIn("<?inside data?>", decoded_xml)
        self.assertIn("<!--child-->", decoded_xml)
        self.assertIn("<!--after-->", decoded_xml)
        self.assertIn("<?outside done?>", decoded_xml)
        positions = [
            decoded_xml.index(token)
            for token in (
                "<!--before-->",
                "<root>",
                "<?inside data?>",
                "<!--child-->",
                "</root>",
                "<!--after-->",
                "<?outside done?>",
            )
        ]
        self.assertEqual(positions, sorted(positions))

    def test_preserves_decimal_attribute_lexical_values(self):
        sample_xml = '<root ratio="3.14" precise="1.234567890123"/>'

        decoded_xml = abx_tool.abx2xml(abx_tool.xml2abx(sample_xml))

        self.assertIn('ratio="3.14"', decoded_xml)
        self.assertIn('precise="1.234567890123"', decoded_xml)

    def test_hex_attribute_does_not_corrupt_following_tokens(self):
        sample_xml = '<root value="0x1A"><child/></root>'

        decoded_xml = abx_tool.abx2xml(abx_tool.xml2abx(sample_xml))

        ET.fromstring(decoded_xml)
        self.assertIn('<child></child>', decoded_xml)

    def test_rejects_malformed_document_structure(self):
        with self.assertRaises(ValueError):
            abx_tool.abx2xml(make_abx(lambda writer: write_element(writer, end="other")))

        valid_abx = make_abx(write_element)
        with self.assertRaises(ValueError):
            abx_tool.abx2xml(valid_abx + b"trailing")
        with self.assertRaises(ValueError):
            abx_tool.abx2xml(valid_abx[:-1])

        def write_two_roots(writer):
            write_element(writer)
            write_element(writer, start="other", end="other")

        with self.assertRaises(ValueError):
            abx_tool.abx2xml(make_abx(write_two_roots))

    def test_rejects_attribute_types_that_cannot_round_trip(self):
        def payload(attr_type):
            def write_body(writer):
                writer.write_byte(abx_tool.START_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
                writer.write_interned_utf("root")
                writer.write_byte(abx_tool.ATTRIBUTE | (attr_type << 4))
                writer.write_interned_utf("value")
                if attr_type == abx_tool.TYPE_STRING:
                    writer.write_utf("123")
                elif attr_type == abx_tool.TYPE_STRING_INTERNED:
                    writer.write_interned_utf("123")
                elif attr_type in (abx_tool.TYPE_BYTES_HEX, abx_tool.TYPE_BYTES_BASE64):
                    writer.write_short(1)
                    writer.write_bytes(b"\xff")
                elif attr_type in (abx_tool.TYPE_LONG, abx_tool.TYPE_LONG_HEX):
                    writer.write_long(1)
                elif attr_type == abx_tool.TYPE_DOUBLE:
                    writer.write_double(1.0)
                writer.write_byte(abx_tool.END_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
                writer.write_interned_utf("root")

            return make_abx(write_body)

        for attr_type in (
            abx_tool.TYPE_NULL,
            abx_tool.TYPE_BYTES_HEX,
            abx_tool.TYPE_BYTES_BASE64,
            abx_tool.TYPE_STRING,
            abx_tool.TYPE_STRING_INTERNED,
            abx_tool.TYPE_LONG,
            abx_tool.TYPE_LONG_HEX,
            abx_tool.TYPE_DOUBLE,
        ):
            with self.subTest(attr_type=attr_type), self.assertRaises(ValueError):
                abx_tool.abx2xml(payload(attr_type))

    def test_rejects_invalid_xml_tokens(self):
        def write_comment(writer):
            writer.write_byte(abx_tool.COMMENT | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("invalid--comment")
            write_element(writer)

        def write_entity(writer):
            writer.write_byte(abx_tool.START_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")
            writer.write_byte(abx_tool.ENTITY_REF | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("amp")
            writer.write_byte(abx_tool.END_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")

        def write_cdata(writer):
            writer.write_byte(abx_tool.START_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")
            writer.write_byte(abx_tool.CDSECT | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("text")
            writer.write_byte(abx_tool.END_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")

        def write_ignorable_whitespace(writer):
            writer.write_byte(abx_tool.START_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")
            writer.write_byte(abx_tool.IGNORABLE_WHITESPACE | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("<child/>")
            writer.write_byte(abx_tool.END_TAG | (abx_tool.TYPE_STRING_INTERNED << 4))
            writer.write_interned_utf("root")

        def write_processing_instruction(writer):
            writer.write_byte(abx_tool.PROCESSING_INSTRUCTION | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("target ?>")
            write_element(writer)

        def write_wrong_token_type(writer):
            writer.write_byte(abx_tool.COMMENT | (abx_tool.TYPE_INT << 4))
            writer.write_utf("comment")
            write_element(writer)

        def write_invalid_name(writer):
            write_element(writer, start="bad name", end="bad name")

        for write_body in (
            write_comment,
            write_entity,
            write_cdata,
            write_ignorable_whitespace,
            write_processing_instruction,
            write_wrong_token_type,
            write_invalid_name,
        ):
            with self.subTest(write_body=write_body.__name__), self.assertRaises(ValueError):
                abx_tool.abx2xml(make_abx(write_body))

    def test_rejects_doctype_that_cannot_round_trip(self):
        with self.assertRaises(ValueError):
            abx_tool.xml2abx('<!DOCTYPE root [<!ENTITY value "data">]><root>&value;</root>')

        def write_doctype(writer):
            writer.write_byte(abx_tool.DOCDECL | (abx_tool.TYPE_STRING << 4))
            writer.write_utf("root")
            write_element(writer)

        with self.assertRaises(ValueError):
            abx_tool.abx2xml(make_abx(write_doctype))

        decoded_xml = abx_tool.abx2xml(
            abx_tool.xml2abx("<root><!--literal <!DOCTYPE marker--></root>")
        )
        self.assertIn("<!--literal <!DOCTYPE marker-->", decoded_xml)

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
