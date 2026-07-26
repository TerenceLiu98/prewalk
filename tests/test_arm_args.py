from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_arm(host: str):
    hooks = ROOT / host / "hooks"
    saved_modules = {
        name: sys.modules.pop(name, None)
        for name in ("_bootstrap", "_common", "prewalk_core")
    }
    sys.path.insert(0, str(hooks))
    try:
        spec = importlib.util.spec_from_file_location(f"{host}_arm", hooks / "_arm.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)
        for name in ("_bootstrap", "_common", "prewalk_core"):
            sys.modules.pop(name, None)
        sys.modules.update({name: value for name, value in saved_modules.items() if value})


class ArmArgumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parsers = [load_arm(host)._parse_args for host in ("codex", "claude-code")]

    def assert_parses(self, arguments: list[str], expected: tuple[str | None, bool]) -> None:
        for parse in self.parsers:
            with self.subTest(parser=parse.__module__, arguments=arguments):
                self.assertEqual(parse(arguments), expected)

    def test_freeform_task_never_selects_a_preset(self) -> None:
        self.assert_parses(["code-value refactor the hook"], (None, False))
        self.assert_parses(["fast", "refactor", "the", "hook"], (None, False))

    def test_quoted_skill_arguments_detect_leading_options(self) -> None:
        self.assert_parses(
            ["--preset code-value --no-pause refactor the hook"],
            ("code-value", True),
        )

    def test_task_text_stops_option_parsing(self) -> None:
        self.assert_parses(["document --no-pause and --preset fast"], (None, False))
        self.assert_parses(["--", "--preset", "fast"], (None, False))

    def test_explicit_preset_forms(self) -> None:
        self.assert_parses(["--preset", "fast", "task"], ("fast", False))
        self.assert_parses(["--preset=fast", "task"], ("fast", False))

    def test_missing_preset_name_is_rejected(self) -> None:
        for parse in self.parsers:
            with self.subTest(parser=parse.__module__):
                with self.assertRaisesRegex(ValueError, "requires a name"):
                    parse(["--preset"])


if __name__ == "__main__":
    unittest.main()
