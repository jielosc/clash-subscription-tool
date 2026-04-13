# Clash Subscription Tool

Build a Clash or Mihomo YAML from either an existing subscription URL or one or more `vless://` links, optionally merge extra `proxies`, replace the top-level `rule-providers` and `rules`, and write both a timestamped history file and a stable `latest.yaml`.

## Files

- `clash_subscription_tool.py`: CLI entrypoint and core logic.
- `settings.yaml.example`: sample runtime config.
- `preferences.yaml.example`: sample replacement for `rule-providers` and `rules`.

## Usage

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Copy and edit the example files:

```bash
cp settings.yaml.example settings.yaml
cp preferences.yaml.example preferences.yaml
```

3. Run the tool:

```bash
python clash_subscription_tool.py
```

## Input modes

Choose exactly one input source in `settings.yaml`:

- `subscription_url`: download an existing Clash or Mihomo YAML, then replace `rule-providers` and `rules`.
- `vless_links`: convert one or more `vless://` links into Mihomo-compatible `proxies`, then generate a minimal config skeleton with a fixed `PROXY` group and built-in `DIRECT`.

## Config format

`settings.yaml`:

```yaml
subscription_url: https://example.com/sub?token=YOUR_TOKEN
# vless_links:
#   - vless://123e4567-e89b-12d3-a456-426614174000@example.com:443?encryption=none&security=tls&type=ws&sni=cdn.example.com&host=cdn.example.com&path=%2Fws#example-node
preferences_file: ./preferences.yaml
output_dir: ./output
latest_filename: latest.yaml
history_filename_pattern: config-%Y%m%d-%H%M%S.yaml
request_timeout_sec: 20
```

`preferences.yaml`:

```yaml
proxies:
  - name: custom-node
    type: socks5
    server: 127.0.0.1
    port: 1080

rule-providers:
  proxy:
    type: http
    behavior: classical
    url: https://example.com/rules/proxy.yaml
    path: ./ruleset/proxy.yaml
    interval: 86400

rules:
  - RULE-SET,proxy,PROXY
  - MATCH,DIRECT
```

## Notes

- By default the script reads `./settings.yaml`. You can still override it with `--config /path/to/settings.yaml` when needed.
- `settings.yaml` must define exactly one input source: `subscription_url` or `vless_links`.
- The subscription URL must already return Clash or Mihomo YAML.
- In `vless_links` mode, the tool supports common VLESS combinations built on `tcp` or `ws`, with `security=none`, `security=tls`, or `security=reality` (Reality currently only with `tcp`).
- The tool merges `preferences.yaml` `proxies` into the generated `proxies` list. When a proxy name already exists, the preferences entry overrides it.
- The tool replaces `rule-providers` and `rules`. For downloaded subscriptions, other top-level fields are preserved. For `vless_links`, the tool generates a minimal config with `mixed-port: 7890`, `mode: rule`, a fixed `PROXY` group, and built-in `DIRECT`.
- For downloaded subscriptions, the tool prepends a generated `PROXY` select group whose `proxies` list contains all other `proxy-groups` by name, plus any extra proxies added from `preferences.yaml`.
- Provider files are not downloaded by this script. Clash or Mihomo still fetches them using the final config.

## Acknowledgements

- The sample rule providers in `preferences.yaml.example` are based on [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules).
