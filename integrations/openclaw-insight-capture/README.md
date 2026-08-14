# Insight Capture OpenClaw plugin

This local plugin separates read and write permissions. The optional
`insight_capture_status` tool can inspect the dashboard and list bags. The
optional `insight_capture_recording` tool can start a default-topic recording
and stop only a recording created through the OpenClaw automation endpoint.

Install it as a linked development plugin:

```bash
openclaw plugins install --link ./integrations/openclaw-insight-capture
openclaw config set plugins.allow '["codex", "insight-capture"]' --strict-json
openclaw config set tools.alsoAllow '["insight_capture_status"]' --strict-json
```

Grant `insight_capture_recording` separately only after the device operator
explicitly authorizes voice start/stop control.

The dashboard URL defaults to `http://127.0.0.1:8765` and is restricted to a
loopback host. Override it under `plugins.entries.insight-capture.config` only
when the dashboard uses another local port.
