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


if __name__ == "__main__":
    unittest.main()
