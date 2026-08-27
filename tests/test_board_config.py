import re
import unittest
from pathlib import Path


BOARD_CONFIG = Path(__file__).parents[1] / "BoardConfig.mk"


class BoardConfigTest(unittest.TestCase):
    def test_does_not_define_recovery_dtbo_input(self):
        contents = BOARD_CONFIG.read_text()

        self.assertIsNone(
            re.search(
                r"(?m)^\s*BOARD_INCLUDE_RECOVERY_DTBO\s*(?::=|\?=|\+=)",
                contents,
            )
        )


if __name__ == "__main__":
    unittest.main()
