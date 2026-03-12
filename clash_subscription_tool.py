from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml

USER_AGENT = "clash-subscription-tool/1.0"
PRIMARY_PROXY_GROUP_NAME = "PROXY"


class ToolError(Exception):
    """Raised when the tool cannot safely build a Clash config."""


@dataclass(frozen=True)
class ToolConfig:
    subscription_url: str
    preferences_file: Path
    output_dir: Path
    latest_filename: str
    history_filename_pattern: str
    request_timeout_sec: int


@dataclass(frozen=True)
class Preferences:
    rule_providers: dict[str, dict[str, Any]]
    rules: list[str]


@dataclass(frozen=True)
class RunResult:
    history_path: Path
    latest_path: Path
    provider_count: int
    rule_count: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download a Clash subscription and replace rule-providers and rules."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the settings YAML file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        result = run(Path(args.config))
    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"Updated Clash config with {result.provider_count} rule-providers "
        f"and {result.rule_count} rules."
    )
    print(f"History file: {result.history_path}")
    print(f"Latest file: {result.latest_path}")
    return 0


def run(
    config_path: Path,
    *,
    session: requests.Session | Any | None = None,
    now: datetime | None = None,
) -> RunResult:
    resolved_config_path = config_path.expanduser().resolve()
    config = load_tool_config(resolved_config_path)
    preferences = load_preferences(config.preferences_file)
    subscription_text = download_subscription(
        config.subscription_url,
        timeout=config.request_timeout_sec,
        session=session,
    )
    subscription_config = parse_subscription_config(subscription_text)
    merged_config = merge_preferences(subscription_config, preferences)
    history_path, latest_path = write_outputs(
        merged_config,
        output_dir=config.output_dir,
        latest_filename=config.latest_filename,
        history_filename_pattern=config.history_filename_pattern,
        now=now,
    )
    return RunResult(
        history_path=history_path,
        latest_path=latest_path,
        provider_count=len(preferences.rule_providers),
        rule_count=len(preferences.rules),
    )


def load_tool_config(config_path: Path) -> ToolConfig:
    data = load_yaml_file(config_path, "Settings file")
    if not isinstance(data, Mapping):
        raise ToolError(f"Settings file must contain a YAML mapping: {config_path}")

    subscription_url = require_non_empty_string(
        data.get("subscription_url"), "settings.subscription_url"
    )
    preferences_file_value = require_non_empty_string(
        data.get("preferences_file"), "settings.preferences_file"
    )
    output_dir_value = require_non_empty_string(
        data.get("output_dir"), "settings.output_dir"
    )

    latest_filename = data.get("latest_filename", "latest.yaml")
    latest_filename = require_non_empty_string(
        latest_filename,
        "settings.latest_filename",
    )

    history_filename_pattern = data.get(
        "history_filename_pattern",
        "config-%Y%m%d-%H%M%S.yaml",
    )
    history_filename_pattern = require_non_empty_string(
        history_filename_pattern,
        "settings.history_filename_pattern",
    )

    request_timeout_sec = data.get("request_timeout_sec", 20)
    if isinstance(request_timeout_sec, bool) or not isinstance(
        request_timeout_sec, (int, float)
    ):
        raise ToolError("settings.request_timeout_sec must be a positive number")
    if request_timeout_sec <= 0:
        raise ToolError("settings.request_timeout_sec must be a positive number")

    settings_dir = config_path.parent
    preferences_file = resolve_path(settings_dir, preferences_file_value)
    output_dir = resolve_path(settings_dir, output_dir_value)

    if output_dir.exists() and not output_dir.is_dir():
        raise ToolError(f"settings.output_dir is not a directory: {output_dir}")

    return ToolConfig(
        subscription_url=subscription_url,
        preferences_file=preferences_file,
        output_dir=output_dir,
        latest_filename=latest_filename,
        history_filename_pattern=history_filename_pattern,
        request_timeout_sec=int(request_timeout_sec),
    )


def load_preferences(preferences_path: Path) -> Preferences:
    data = load_yaml_file(preferences_path, "Preferences file")
    if not isinstance(data, Mapping):
        raise ToolError(
            f"Preferences file must contain a YAML mapping: {preferences_path}"
        )

    if "rule-providers" not in data:
        raise ToolError("Preferences file must define rule-providers")
    if "rules" not in data:
        raise ToolError("Preferences file must define rules")

    providers_value = data["rule-providers"]
    if not isinstance(providers_value, Mapping) or not providers_value:
        raise ToolError("Preferences rule-providers must be a non-empty mapping")

    normalized_providers: dict[str, dict[str, Any]] = {}
    for name, provider_config in providers_value.items():
        if not isinstance(name, str) or not name.strip():
            raise ToolError("Each rule-providers key must be a non-empty string")
        if not isinstance(provider_config, Mapping) or not provider_config:
            raise ToolError(
                f"rule-providers.{name} must be a non-empty mapping"
            )
        normalized_providers[name] = dict(provider_config)

    rules_value = data["rules"]
    if not isinstance(rules_value, list) or not rules_value:
        raise ToolError("Preferences rules must be a non-empty list")

    normalized_rules: list[str] = []
    for index, rule in enumerate(rules_value, start=1):
        if not isinstance(rule, str) or not rule.strip():
            raise ToolError(f"Preferences rule #{index} must be a non-empty string")
        normalized_rules.append(rule)

    return Preferences(
        rule_providers=normalized_providers,
        rules=normalized_rules,
    )


def download_subscription(
    subscription_url: str,
    *,
    timeout: int,
    session: requests.Session | Any | None = None,
) -> str:
    requester = session or requests.Session()
    created_session = session is None

    try:
        response = requester.get(
            subscription_url,
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/x-yaml, text/yaml, text/plain, */*",
            },
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ToolError(f"Failed to download subscription: {exc}") from exc
    finally:
        if created_session:
            requester.close()

    if not response.text.strip():
        raise ToolError("Subscription response is empty")
    return response.text


def parse_subscription_config(subscription_text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(subscription_text)
    except yaml.YAMLError as exc:
        raise ToolError("Subscription content is not valid YAML") from exc

    if not isinstance(data, Mapping):
        raise ToolError("Subscription content is not a Clash YAML mapping")
    return dict(data)


def merge_preferences(
    subscription_config: dict[str, Any],
    preferences: Preferences,
) -> dict[str, Any]:
    merged = copy.deepcopy(subscription_config)
    merged["rule-providers"] = copy.deepcopy(preferences.rule_providers)
    merged["rules"] = list(preferences.rules)
    merged["proxy-groups"] = build_proxy_groups(merged.get("proxy-groups"))
    return merged


def build_proxy_groups(proxy_groups_value: Any) -> list[Any]:
    if proxy_groups_value is None:
        remaining_groups: list[Any] = []
    elif not isinstance(proxy_groups_value, list):
        raise ToolError("Subscription proxy-groups must be a list")
    else:
        remaining_groups = []
        for group in proxy_groups_value:
            if (
                isinstance(group, Mapping)
                and group.get("name") == PRIMARY_PROXY_GROUP_NAME
            ):
                continue
            remaining_groups.append(copy.deepcopy(group))

    proxies: list[str] = []
    for group in remaining_groups:
        if not isinstance(group, Mapping):
            continue
        group_name = group.get("name")
        if not isinstance(group_name, str) or not group_name.strip():
            continue
        if group_name in proxies:
            continue
        proxies.append(group_name)

    primary_group = {
        "name": PRIMARY_PROXY_GROUP_NAME,
        "type": "select",
        "proxies": proxies,
    }
    return [primary_group, *remaining_groups]


def write_outputs(
    merged_config: dict[str, Any],
    *,
    output_dir: Path,
    latest_filename: str,
    history_filename_pattern: str,
    now: datetime | None = None,
) -> tuple[Path, Path]:
    timestamp = now or datetime.now()
    history_filename = timestamp.strftime(history_filename_pattern)

    output_dir.mkdir(parents=True, exist_ok=True)
    history_path = output_dir / history_filename
    latest_path = output_dir / latest_filename
    latest_path.parent.mkdir(parents=True, exist_ok=True)

    yaml_text = yaml.safe_dump(
        merged_config,
        allow_unicode=True,
        sort_keys=False,
    )
    history_path.write_text(yaml_text, encoding="utf-8", newline="\n")
    latest_path.write_text(yaml_text, encoding="utf-8", newline="\n")
    return history_path, latest_path


def load_yaml_file(path: Path, label: str) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ToolError(f"{label} not found: {path}") from exc
    except OSError as exc:
        raise ToolError(f"Unable to read {label.lower()}: {path}") from exc

    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ToolError(f"{label} is not valid YAML: {path}") from exc


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{field_name} must be a non-empty string")
    return value.strip()
