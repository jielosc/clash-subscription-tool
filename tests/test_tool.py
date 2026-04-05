from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

from clash_subscription_tool import RunResult, ToolError, build_parser, main, run


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            from requests import HTTPError

            raise HTTPError(f"{self.status_code} error")


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, timeout: int, headers: dict[str, str]) -> FakeResponse:
        self.calls.append(
            {
                "url": url,
                "timeout": timeout,
                "headers": headers,
            }
        )
        return self._response


class ClashSubscriptionToolTests(unittest.TestCase):
    def test_parser_defaults_to_local_settings_yaml(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.config, "settings.yaml")

    def test_main_uses_default_settings_file_without_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = Path.cwd()
            temp_path = Path(temp_dir).resolve()
            expected_result = RunResult(
                history_path=temp_path / "out" / "config.yaml",
                latest_path=temp_path / "out" / "latest.yaml",
                provider_count=1,
                rule_count=2,
            )

            os.chdir(temp_path)
            try:
                with patch("clash_subscription_tool.run", return_value=expected_result) as run_mock:
                    exit_code = main([])
            finally:
                os.chdir(original_cwd)

            self.assertEqual(exit_code, 0)
            run_mock.assert_called_once_with(Path("settings.yaml"))

    def test_run_replaces_rules_rule_providers_and_prepends_proxy_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            settings_path = temp_path / "settings.yaml"
            preferences_path = temp_path / "preferences.yaml"
            output_dir = temp_path / "out"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./out",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "rule-providers:",
                        "  custom:",
                        "    type: http",
                        "    behavior: classical",
                        "    url: https://example.com/provider.yaml",
                        "    path: ./ruleset/custom.yaml",
                        "rules:",
                        "  - RULE-SET,custom,Proxy",
                        "  - MATCH,DIRECT",
                    ]
                ),
                encoding="utf-8",
            )

            subscription_text = "\n".join(
                [
                    "mixed-port: 7890",
                    "allow-lan: true",
                    "proxies:",
                    "  - name: node-a",
                    "    type: ss",
                    "    server: 1.1.1.1",
                    "    port: 443",
                    "proxy-groups:",
                    "  - name: PROXY",
                    "    type: select",
                    "    proxies: [stale]",
                    "  - name: Auto",
                    "    type: url-test",
                    "    proxies: [node-a]",
                    "  - name: Fallback",
                    "    type: select",
                    "    proxies: [node-a]",
                    "rule-providers:",
                    "  old-provider:",
                    "    type: http",
                    "    behavior: classical",
                    "    url: https://old.example.com/rules.yaml",
                    "    path: ./ruleset/old.yaml",
                    "rules:",
                    "  - MATCH,REJECT",
                ]
            )
            session = FakeSession(FakeResponse(subscription_text))
            now = datetime(2026, 3, 12, 14, 15, 16)

            result = run(settings_path, session=session, now=now)

            history_path = output_dir / "config-20260312-141516.yaml"
            latest_path = output_dir / "latest.yaml"

            self.assertEqual(result.history_path, history_path)
            self.assertEqual(result.latest_path, latest_path)
            self.assertEqual(result.provider_count, 1)
            self.assertEqual(result.rule_count, 2)
            self.assertTrue(history_path.exists())
            self.assertTrue(latest_path.exists())
            self.assertEqual(
                history_path.read_text(encoding="utf-8"),
                latest_path.read_text(encoding="utf-8"),
            )

            merged = yaml.safe_load(latest_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["mixed-port"], 7890)
            self.assertEqual(merged["allow-lan"], True)
            self.assertEqual(merged["proxies"][0]["name"], "node-a")
            self.assertEqual(list(merged["rule-providers"].keys()), ["custom"])
            self.assertEqual(
                merged["rules"],
                ["RULE-SET,custom,Proxy", "MATCH,DIRECT"],
            )
            self.assertEqual(
                merged["proxy-groups"][0],
                {
                    "name": "PROXY",
                    "type": "select",
                    "proxies": ["Auto", "Fallback"],
                },
            )
            self.assertEqual(
                [group["name"] for group in merged["proxy-groups"][1:]],
                ["Auto", "Fallback"],
            )
            self.assertEqual(session.calls[0]["url"], "https://example.com/subscription")
            self.assertEqual(session.calls[0]["timeout"], 20)

    def test_run_adds_missing_rules_sections_and_proxy_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            settings_path = temp_path / "settings.yaml"
            preferences_path = temp_path / "preferences.yaml"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./out",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "rule-providers:",
                        "  direct:",
                        "    type: file",
                        "    behavior: classical",
                        "    path: ./ruleset/direct.yaml",
                        "rules:",
                        "  - RULE-SET,direct,DIRECT",
                    ]
                ),
                encoding="utf-8",
            )

            session = FakeSession(
                FakeResponse(
                    "\n".join(
                        [
                            "port: 7890",
                            "proxies: []",
                        ]
                    )
                )
            )

            result = run(settings_path, session=session, now=datetime(2026, 3, 12, 1, 2, 3))

            merged = yaml.safe_load(result.latest_path.read_text(encoding="utf-8"))
            self.assertIn("rule-providers", merged)
            self.assertIn("rules", merged)
            self.assertIn("proxy-groups", merged)
            self.assertEqual(merged["rules"], ["RULE-SET,direct,DIRECT"])
            self.assertEqual(
                merged["proxy-groups"],
                [
                    {
                        "name": "PROXY",
                        "type": "select",
                        "proxies": [],
                    }
                ],
            )

    def test_run_merges_preference_proxies_and_overrides_matching_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            settings_path = temp_path / "settings.yaml"
            preferences_path = temp_path / "preferences.yaml"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./out",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "proxies:",
                        "  - name: node-b",
                        "    type: socks5",
                        "    server: 8.8.8.8",
                        "    port: 1080",
                        "  - name: custom-node",
                        "    type: http",
                        "    server: 9.9.9.9",
                        "    port: 8080",
                        "rule-providers:",
                        "  direct:",
                        "    type: file",
                        "    behavior: classical",
                        "    path: ./ruleset/direct.yaml",
                        "rules:",
                        "  - MATCH,DIRECT",
                    ]
                ),
                encoding="utf-8",
            )

            session = FakeSession(
                FakeResponse(
                    "\n".join(
                        [
                            "proxies:",
                            "  - name: node-a",
                            "    type: ss",
                            "    server: 1.1.1.1",
                            "    port: 443",
                            "  - name: node-b",
                            "    type: ss",
                            "    server: 2.2.2.2",
                            "    port: 8443",
                            "proxy-groups:",
                            "  - name: Auto",
                            "    type: url-test",
                            "    proxies: [node-a, node-b]",
                        ]
                    )
                )
            )

            result = run(settings_path, session=session, now=datetime(2026, 3, 12, 1, 2, 3))

            merged = yaml.safe_load(result.latest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [proxy["name"] for proxy in merged["proxies"]],
                ["node-a", "node-b", "custom-node"],
            )
            self.assertEqual(merged["proxies"][1]["type"], "socks5")
            self.assertEqual(merged["proxies"][1]["server"], "8.8.8.8")
            self.assertEqual(
                merged["proxy-groups"][0],
                {
                    "name": "PROXY",
                    "type": "select",
                    "proxies": ["Auto", "node-b", "custom-node"],
                },
            )

    def test_preferences_validation_requires_non_empty_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            settings_path = temp_path / "settings.yaml"
            preferences_path = temp_path / "preferences.yaml"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./out",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "rule-providers: {}",
                        "rules: []",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ToolError,
                "Preferences rule-providers must be a non-empty mapping",
            ):
                run(
                    settings_path,
                    session=FakeSession(FakeResponse("proxies: []")),
                    now=datetime(2026, 3, 12, 1, 2, 3),
                )

    def test_subscription_validation_rejects_non_mapping_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir).resolve()
            settings_path = temp_path / "settings.yaml"
            preferences_path = temp_path / "preferences.yaml"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./out",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "rule-providers:",
                        "  proxy:",
                        "    type: file",
                        "    behavior: classical",
                        "    path: ./ruleset/proxy.yaml",
                        "rules:",
                        "  - MATCH,DIRECT",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ToolError,
                "Subscription content is not a Clash YAML mapping",
            ):
                run(
                    settings_path,
                    session=FakeSession(FakeResponse("- a\n- list\n")),
                    now=datetime(2026, 3, 12, 1, 2, 3),
                )

    def test_relative_paths_are_resolved_from_settings_file(self) -> None:
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as other_dir:
            temp_path = Path(temp_dir).resolve()
            settings_dir = temp_path / "config"
            settings_dir.mkdir()
            settings_path = settings_dir / "settings.yaml"
            preferences_path = settings_dir / "preferences.yaml"

            settings_path.write_text(
                "\n".join(
                    [
                        "subscription_url: https://example.com/subscription",
                        "preferences_file: ./preferences.yaml",
                        "output_dir: ./generated",
                    ]
                ),
                encoding="utf-8",
            )
            preferences_path.write_text(
                "\n".join(
                    [
                        "rule-providers:",
                        "  proxy:",
                        "    type: file",
                        "    behavior: classical",
                        "    path: ./ruleset/proxy.yaml",
                        "rules:",
                        "  - MATCH,DIRECT",
                    ]
                ),
                encoding="utf-8",
            )

            os.chdir(other_dir)
            try:
                result = run(
                    settings_path,
                    session=FakeSession(FakeResponse("proxies: []")),
                    now=datetime(2026, 3, 12, 1, 2, 3),
                )
            finally:
                os.chdir(original_cwd)

            self.assertEqual(result.latest_path.parent, settings_dir / "generated")
            self.assertTrue(result.latest_path.exists())


if __name__ == "__main__":
    unittest.main()
