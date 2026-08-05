from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any

from simpmc_motd.config import DEFAULTS

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PAGE_ROOT = REPOSITORY_ROOT / "pages" / "settings"


def _flatten_keys(value: Any, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    keys: set[str] = set()
    for key, item in value.items():
        child = f"{prefix}.{key}" if prefix else str(key)
        keys.update(_flatten_keys(item, child))
    return keys


class SettingsPageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
        cls.javascript = (PAGE_ROOT / "app.js").read_text(encoding="utf-8")
        cls.schema = json.loads((REPOSITORY_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))

    def test_packaged_page_uses_authenticated_bridge_without_preview_assets(self) -> None:
        self.assertTrue((PAGE_ROOT / "style.css").is_file())
        self.assertFalse((PAGE_ROOT / "preview-bridge.js").exists())
        self.assertNotIn("preview-bridge", self.html)
        self.assertIn('bridge.apiGet("settings")', self.javascript)
        self.assertIn('bridge.apiPost("settings/save", { settings })', self.javascript)
        self.assertRegex(self.javascript, r"await\s+bridge\.ready\(\)")
        self.assertNotRegex(
            self.javascript,
            r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval)\b",
        )

    def test_schema_backend_page_and_form_field_sets_match(self) -> None:
        expected_storage = set(DEFAULTS)
        expected_form = (expected_storage - {"group_servers_json"}) | {"group_servers"}
        default_block = re.search(
            r"const DEFAULT_SETTINGS = Object\.freeze\(\{(?P<body>.*?)\}\);",
            self.javascript,
            re.DOTALL,
        )
        self.assertIsNotNone(default_block)
        frontend_fields = set(
            re.findall(r"^\s{2}([a-z][a-z0-9_]*):", default_block.group("body"), re.MULTILINE)
        )
        html_fields = set(
            re.findall(
                r'<(?:input|select|textarea)\b[^>]*\bname="([a-z][a-z0-9_]*)"',
                self.html,
                re.DOTALL,
            )
        )

        self.assertEqual(expected_storage, set(self.schema))
        self.assertEqual(expected_form, frontend_fields)
        self.assertEqual(expected_form - {"group_servers"}, html_fields)
        self.assertTrue(self.schema["group_whitelist"]["invisible"])
        self.assertTrue(self.schema["group_servers_json"]["invisible"])

    def test_all_static_page_translation_keys_exist_in_both_locales(self) -> None:
        html_keys = set(re.findall(r'data-i18n(?:-aria)?="([a-zA-Z0-9_.-]+)"', self.html))
        javascript_keys = set(
            re.findall(r"translate\(\s*[\"']([a-zA-Z0-9_.-]+)[\"']", self.javascript)
        )
        used_keys = html_keys | javascript_keys

        for locale in ("zh-CN", "en-US"):
            messages = json.loads(
                (REPOSITORY_ROOT / ".astrbot-plugin" / "i18n" / f"{locale}.json").read_text(
                    encoding="utf-8"
                )
            )
            available = _flatten_keys(messages["pages"]["settings"])
            self.assertEqual(set(), used_keys - available, locale)

    def test_page_explains_console_only_configuration_and_exact_triggers(self) -> None:
        self.assertIn("仅限控制台配置", self.html)
        self.assertIn("/motd", self.html)
        self.assertIn(">motd<", self.html)


if __name__ == "__main__":
    unittest.main()
