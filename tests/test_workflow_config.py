import re
import unittest
from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "build_OrangeFox.yml"


class WorkflowConfigTest(unittest.TestCase):
    def test_android_build_has_an_explicit_ccache_wrapper(self):
        contents = WORKFLOW.read_text()

        self.assertRegex(
            contents,
            r"(?m)^\s*CCACHE_EXEC:\s*/usr/bin/ccache\s*$",
        )
        self.assertRegex(contents, r"(?m)^\s*USE_CCACHE:\s*1\s*$")

    def test_restored_ccache_is_copied_into_a_writable_directory(self):
        contents = WORKFLOW.read_text()

        self.assertIn("path: .ccache-restore", contents)
        self.assertIn(
            'cp -R --no-preserve=mode,ownership,timestamps .ccache-restore/. "${CCACHE_DIR}/"',
            contents,
        )
        self.assertIn('chmod -R u+rwX "${CCACHE_DIR}"', contents)


if __name__ == "__main__":
    unittest.main()
