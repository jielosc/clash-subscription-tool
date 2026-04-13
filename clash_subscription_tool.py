#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
import yaml

USER_AGENT = "clash-subscription-tool/1.0"
PRIMARY_PROXY_GROUP_NAME = "PROXY"
DEFAULT_CONFIG_PATH = "settings.yaml"
DEFAULT_VLESS_MIXED_PORT = 7890


class ToolError(Exception):
    """Raised when the tool cannot safely build a Clash config."""


@dataclass(frozen=True)
class ToolConfig:
    subscription_url: str | None
    vless_links: list[str]
    preferences_file: Path
    output_dir: Path
    latest_filename: str
    history_filename_pattern: str
    request_timeout_sec: int


@dataclass(frozen=True)
class Preferences:
    proxies: list[dict[str, Any]]
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
        description=(
            "Build a Clash or Mihomo config from either a subscription YAML "
            "or one or more VLESS links."
        )
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the settings YAML file. Default: ./{DEFAULT_CONFIG_PATH}",
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
    merged_config = build_output_config(
        config,
        preferences,
        session=session,
    )
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

    subscription_url = load_optional_non_empty_string(
        data.get("subscription_url"),
        "settings.subscription_url",
    )
    vless_links = load_vless_links(data.get("vless_links"))
    if subscription_url and vless_links:
        raise ToolError(
            "Choose exactly one input source: settings.subscription_url or "
            "settings.vless_links"
        )
    if not subscription_url and not vless_links:
        raise ToolError(
            "Settings must define exactly one input source: "
            "settings.subscription_url or settings.vless_links"
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
        vless_links=vless_links,
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

    proxies_value = data.get("proxies", [])
    if not isinstance(proxies_value, list):
        raise ToolError("Preferences proxies must be a list when provided")

    normalized_proxies: list[dict[str, Any]] = []
    seen_proxy_names: set[str] = set()
    for index, proxy in enumerate(proxies_value, start=1):
        if not isinstance(proxy, Mapping) or not proxy:
            raise ToolError(f"Preferences proxy #{index} must be a non-empty mapping")

        proxy_name = proxy.get("name")
        if not isinstance(proxy_name, str) or not proxy_name.strip():
            raise ToolError(
                f"Preferences proxy #{index} must define a non-empty name"
            )
        if proxy_name in seen_proxy_names:
            raise ToolError(f"Duplicate preferences proxy name: {proxy_name}")

        seen_proxy_names.add(proxy_name)
        normalized_proxies.append(dict(proxy))

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
        proxies=normalized_proxies,
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


def build_output_config(
    config: ToolConfig,
    preferences: Preferences,
    *,
    session: requests.Session | Any | None = None,
) -> dict[str, Any]:
    if config.subscription_url:
        subscription_text = download_subscription(
            config.subscription_url,
            timeout=config.request_timeout_sec,
            session=session,
        )
        subscription_config = parse_subscription_config(subscription_text)
        return merge_preferences(subscription_config, preferences)

    generated_proxies = parse_vless_links(config.vless_links)
    return build_minimal_config(generated_proxies, preferences)


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
    merged_proxies = merge_proxies(merged.get("proxies"), preferences.proxies)
    if merged_proxies is not None:
        merged["proxies"] = merged_proxies
    merged["rule-providers"] = copy.deepcopy(preferences.rule_providers)
    merged["rules"] = list(preferences.rules)
    merged["proxy-groups"] = build_proxy_groups(
        merged.get("proxy-groups"),
        extra_proxy_names=[proxy["name"] for proxy in preferences.proxies],
    )
    return merged


def build_minimal_config(
    generated_proxies: list[dict[str, Any]],
    preferences: Preferences,
) -> dict[str, Any]:
    merged_proxies = merge_proxies(generated_proxies, preferences.proxies)
    proxy_names = collect_proxy_names(merged_proxies or [])
    return {
        "mixed-port": DEFAULT_VLESS_MIXED_PORT,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": merged_proxies or [],
        "proxy-groups": build_minimal_proxy_groups(proxy_names),
        "rule-providers": copy.deepcopy(preferences.rule_providers),
        "rules": list(preferences.rules),
    }


def merge_proxies(
    subscription_proxies_value: Any,
    preference_proxies: list[dict[str, Any]],
) -> list[Any] | None:
    if subscription_proxies_value is None:
        if not preference_proxies:
            return None
        merged_proxies: list[Any] = []
    elif not isinstance(subscription_proxies_value, list):
        raise ToolError("Subscription proxies must be a list")
    else:
        merged_proxies = copy.deepcopy(subscription_proxies_value)

    proxy_indexes_by_name: dict[str, int] = {}
    for index, proxy in enumerate(merged_proxies):
        if not isinstance(proxy, Mapping):
            continue
        proxy_name = proxy.get("name")
        if not isinstance(proxy_name, str) or not proxy_name.strip():
            continue
        proxy_indexes_by_name[proxy_name] = index

    for proxy in preference_proxies:
        proxy_name = proxy["name"]
        if proxy_name in proxy_indexes_by_name:
            merged_proxies[proxy_indexes_by_name[proxy_name]] = copy.deepcopy(proxy)
            continue

        proxy_indexes_by_name[proxy_name] = len(merged_proxies)
        merged_proxies.append(copy.deepcopy(proxy))

    return merged_proxies


def build_proxy_groups(
    proxy_groups_value: Any,
    *,
    extra_proxy_names: list[str] | None = None,
) -> list[Any]:
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

    for proxy_name in extra_proxy_names or []:
        if proxy_name in proxies:
            continue
        proxies.append(proxy_name)

    primary_group = {
        "name": PRIMARY_PROXY_GROUP_NAME,
        "type": "select",
        "proxies": proxies,
    }
    return [primary_group, *remaining_groups]


def build_minimal_proxy_groups(proxy_names: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": PRIMARY_PROXY_GROUP_NAME,
            "type": "select",
            "proxies": list(proxy_names),
        },
    ]


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


def load_optional_non_empty_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return require_non_empty_string(value, field_name)


def load_vless_links(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ToolError("settings.vless_links must be a non-empty list")

    links: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ToolError(
                f"settings.vless_links[{index}] must be a non-empty string"
            )
        links.append(item.strip())
    return links


def parse_vless_links(vless_links: list[str]) -> list[dict[str, Any]]:
    proxies: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for index, link in enumerate(vless_links, start=1):
        proxy = parse_vless_link(link, index=index)
        proxy["name"] = make_unique_name(proxy["name"], used_names)
        used_names.add(proxy["name"])
        proxies.append(proxy)
    return proxies


def parse_vless_link(link: str, *, index: int) -> dict[str, Any]:
    parsed = urlsplit(link)
    if parsed.scheme.lower() != "vless":
        raise ToolError(f"VLESS link #{index} must start with vless://")
    if not parsed.username:
        raise ToolError(f"VLESS link #{index} is missing the UUID")
    if not parsed.hostname:
        raise ToolError(f"VLESS link #{index} is missing the server host")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ToolError(f"VLESS link #{index} has an invalid server port") from exc
    if port is None:
        raise ToolError(f"VLESS link #{index} is missing the server port")

    params = {
        key: value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    encryption = params.get("encryption", "none")
    if encryption != "none":
        raise ToolError(
            f"VLESS link #{index} has unsupported encryption={encryption!r}; "
            "expected 'none'"
        )

    network = params.get("type", "tcp").strip().lower() or "tcp"
    if network not in {"tcp", "ws"}:
        raise ToolError(
            f"VLESS link #{index} has unsupported transport type={network!r}; "
            "supported: tcp, ws"
        )

    proxy_name = unquote(parsed.fragment).strip() or f"{parsed.hostname}:{port}"
    proxy: dict[str, Any] = {
        "name": proxy_name,
        "type": "vless",
        "server": parsed.hostname,
        "port": port,
        "uuid": unquote(parsed.username),
        "network": network,
        "udp": True,
    }

    flow = params.get("flow", "").strip()
    if flow:
        proxy["flow"] = flow

    tls_fields = build_tls_fields(params, index=index, network=network)
    proxy.update(tls_fields)

    if network == "tcp":
        header_type = params.get("headerType", "").strip().lower()
        if header_type not in {"", "none"}:
            raise ToolError(
                f"VLESS link #{index} has unsupported headerType={header_type!r} "
                "for tcp transport"
            )
        if params.get("host") or params.get("path"):
            raise ToolError(
                f"VLESS link #{index} includes ws-only parameters for tcp transport"
            )
    else:
        proxy["ws-opts"] = build_ws_opts(params)

    unsupported_transport_params = {
        "serviceName": "grpc serviceName",
        "mode": "grpc mode",
        "authority": "grpc authority",
    }
    for key, label in unsupported_transport_params.items():
        value = params.get(key, "").strip()
        if value:
            raise ToolError(
                f"VLESS link #{index} uses unsupported {label}; only tcp and ws "
                "transports are supported"
            )

    return proxy


def build_tls_fields(
    params: Mapping[str, str],
    *,
    index: int,
    network: str,
) -> dict[str, Any]:
    security = params.get("security", "none").strip().lower() or "none"
    skip_cert_verify = parse_truthy_flag(params.get("allowInsecure", "0"))
    fields: dict[str, Any] = {}

    if security == "none":
        return fields

    if security == "tls":
        fields["tls"] = True
        fields["skip-cert-verify"] = skip_cert_verify
        servername = params.get("sni", "").strip()
        if servername:
            fields["servername"] = servername
        alpn_value = params.get("alpn", "").strip()
        if alpn_value:
            fields["alpn"] = [item.strip() for item in alpn_value.split(",") if item.strip()]
        fingerprint = params.get("fp", "").strip()
        if fingerprint:
            fields["client-fingerprint"] = fingerprint
        return fields

    if security == "reality":
        if network != "tcp":
            raise ToolError(
                f"VLESS link #{index} uses security=reality with unsupported "
                f"transport type={network!r}; supported combination: reality + tcp"
            )
        public_key = params.get("pbk", "").strip()
        if not public_key:
            raise ToolError(
                f"VLESS link #{index} is missing pbk for security=reality"
            )
        servername = params.get("sni", "").strip()
        if not servername:
            raise ToolError(
                f"VLESS link #{index} is missing sni for security=reality"
            )

        fields["tls"] = True
        fields["skip-cert-verify"] = skip_cert_verify
        fields["servername"] = servername
        fingerprint = params.get("fp", "").strip()
        if fingerprint:
            fields["client-fingerprint"] = fingerprint

        reality_opts: dict[str, Any] = {
            "public-key": public_key,
        }
        short_id = params.get("sid", "").strip()
        if short_id:
            reality_opts["short-id"] = short_id
        spider_x = params.get("spx", "").strip()
        if spider_x:
            reality_opts["spider-x"] = spider_x
        fields["reality-opts"] = reality_opts
        return fields

    raise ToolError(
        f"VLESS link #{index} has unsupported security={security!r}; "
        "supported: none, tls, reality"
    )


def build_ws_opts(params: Mapping[str, str]) -> dict[str, Any]:
    ws_opts: dict[str, Any] = {
        "path": params.get("path", "").strip() or "/",
    }
    host = params.get("host", "").strip()
    if host:
        ws_opts["headers"] = {"Host": host}
    return ws_opts


def parse_truthy_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def make_unique_name(name: str, used_names: set[str]) -> str:
    if name not in used_names:
        return name

    suffix = 2
    while True:
        candidate = f"{name} ({suffix})"
        if candidate not in used_names:
            return candidate
        suffix += 1


def collect_proxy_names(proxies: list[Any]) -> list[str]:
    names: list[str] = []
    for proxy in proxies:
        if not isinstance(proxy, Mapping):
            continue
        proxy_name = proxy.get("name")
        if not isinstance(proxy_name, str) or not proxy_name.strip():
            continue
        names.append(proxy_name)
    return names


if __name__ == "__main__":
    raise SystemExit(main())
