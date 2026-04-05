# Clash Subscription Tool

Download a Clash or Mihomo subscription YAML, optionally merge extra `proxies`, replace the top-level `rule-providers` and `rules`, and write both a timestamped history file and a stable `latest.yaml`.

## Files

- `clash_subscription_tool.py`: CLI entrypoint and core logic.
- `settings.example.yaml`: sample runtime config.
- `preferences.example.yaml`: sample replacement for `rule-providers` and `rules`.

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

## Config format

`settings.yaml`:

```yaml
subscription_url: https://example.com/sub?token=YOUR_TOKEN
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
  - RULE-SET,proxy,Proxy
  - MATCH,DIRECT
```

## Notes

- By default the script reads `./settings.yaml`. You can still override it with `--config /path/to/settings.yaml` when needed.
- The subscription URL must already return Clash or Mihomo YAML.
- The tool merges `preferences.yaml` `proxies` into the subscription `proxies` list. When a proxy name already exists in the subscription, the preferences entry overrides it.
- The tool replaces `rule-providers` and `rules`. Other top-level fields are preserved.
- The tool prepends a generated `PROXY` select group whose `proxies` list contains all other `proxy-groups` by name, plus any extra proxies added from `preferences.yaml`.
- Provider files are not downloaded by this script. Clash or Mihomo still fetches them using the final config.

## Acknowledgements

- The sample rule providers in `preferences.example.yaml` are based on [Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules).

## GitHub publishing

- Commit `settings.example.yaml` and `preferences.example.yaml`, not your local `settings.yaml`.
- Do not commit the generated `output/` directory because it can contain subscription secrets and node credentials.
- The included `.gitignore` already excludes `settings.yaml`, `preferences.yaml`, `output/`, and Python cache files.
