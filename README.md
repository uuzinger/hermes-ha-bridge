# Hermes ↔ Home Assistant bridge

Lets a Home Assistant voice satellite reach the Hermes agent harness over the
LAN. No third-party messaging service in the path.

Hermes runs on `hermes.example.com`; HA runs on `ha.example.com`; the satellite is
an Echo Show 8 running LineageOS with VACA / View Assist.

```
Echo (VACA) ──► HA custom sentence "ask hermes ..."
                     │
                     ├──► rest_command ──► bridge :8420 ──► hermes chat
                     │                                            │
                     └──► "Let me think about that." (immediate)   │
                                                                  ▼
Echo speaks ◄── tts.speak ◄── HA webhook automation ◄── bridge POSTs answer
```

Asynchronous by design. Agent runs take 15-40 seconds; blocking an HA
automation that long against `rest_command` timeouts is fragile, so the POST
returns `202` immediately and the answer arrives later at a webhook.

## Contents

```
bridge/
  hermes_bridge.py            FastAPI shim wrapping `hermes chat`
  hermes-bridge.service       systemd unit
  bridge.env.example          environment template
homeassistant/
  configuration.snippet.yaml  → configuration.yaml
  secrets.snippet.yaml        → secrets.yaml
  automations.snippet.yaml    → automations.yaml
```

Each snippet names its destination file at the top. They are insertions, not
replacements — merge into what you have.

---

## 1. Generate credentials

```bash
openssl rand -hex 32   # bridge token
openssl rand -hex 16   # webhook id
```

Both appear in two places and must match:

| Value | On the bridge | In Home Assistant |
|---|---|---|
| token | `HERMES_BRIDGE_TOKEN` (bare) | `hermes_bridge_token` (**with** `Bearer ` prefix) |
| webhook id | inside `HA_WEBHOOK_URL` | `hermes_reply_webhook_id` |

The asymmetric token handling is the most common setup mistake. HA's `!secret`
cannot be interpolated into a surrounding string, so the secret carries the
full `Bearer <token>` header value while the env var holds the bare token.

## 2. Install the bridge

On `hermes.example.com`, as the user that runs `hermes`:

```bash
mkdir -p ~/hermes_bridge && cd ~/hermes_bridge
python3 -m venv .venv-bridge
.venv-bridge/bin/pip install fastapi uvicorn httpx

cp /path/to/bridge/hermes_bridge.py .
cp /path/to/bridge/bridge.env.example bridge.env
chmod 600 bridge.env
$EDITOR bridge.env
```

Run it in the foreground first:

```bash
set -a && . bridge.env && set +a
.venv-bridge/bin/uvicorn hermes_bridge:app --host 0.0.0.0 --port 8420
```

`0.0.0.0` for this first test only; the systemd unit pins it to the LAN address.

**Verify from the HA host, not localhost** — you are proving HA's network path:

```bash
curl -s http://hermes.example.com:8420/health | jq

curl -s -X POST http://hermes.example.com:8420/ask \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"text":"what is the weather in Dallas","device_id":"echo_screen_8"}'
```

You get a `request_id` back immediately. Watch the console for the run.
`401` means the token is wrong; `403` means `device_id` is not in
`ALLOWED_DEVICES`.

## 3. Home Assistant

Insert the three snippets into their named files. Then:

1. Substitute your real entity ids in `automations.snippet.yaml`
   (`media_player.vaca_echo_screen8`, `tts.piper`)
2. Developer Tools → YAML → Reload all YAML
3. Confirm **both** "Hermes — ask" and "Hermes — reply" appear under
   Settings → Automations

Test each hop in isolation before combining:

```yaml
# Developer Tools → Actions — does TTS reach the Echo at all?
action: tts.speak
target:
  entity_id: tts.piper
data:
  media_player_entity_id: media_player.echo_screen_8
  message: test
```

```yaml
# Does the rest_command reach the bridge?
action: rest_command.hermes_ask
data:
  question: what is the weather in Dallas
  device: zeds_office
```

Then type `ask hermes what is the weather` into the HA Assist chat panel.
Testing by text first separates sentence matching from wake word, STT, and TTS.
Only then try it by voice.

## 4. Run it as a service

```bash
sudo cp hermes-bridge.service /etc/systemd/system/
sudo $EDITOR /etc/systemd/system/hermes-bridge.service   # set the LAN IP
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-bridge
journalctl -u hermes-bridge -f
```

---

## Troubleshooting

Work the path in order; each check rules out one hop.

| Symptom | Cause |
|---|---|
| No ack in the chat panel | Sentence didn't match; the conversation agent handled it. Check the "Hermes — ask" trace. |
| Ack, but nothing in the bridge log | `rest_command` failed, or `trigger.slots.question` was empty. Check the automation trace. |
| `401` | Missing `Bearer ` prefix in `secrets.yaml`. |
| `403` | `device` value doesn't match `ALLOWED_DEVICES`. |
| Bridge logs `running` but never POSTs | Run failed or returned unusable output. A traceback will be in the console. |
| Bridge POSTs `200` but nothing happens | **HA returns 200 for unregistered webhook ids.** The reply automation is missing or the id doesn't match. |
| Reply automation fires but errors | Wrong entity ids in `tts.speak`. |

`curl -s http://hermes.example.com:8420/health | jq .last` shows the last
result per device, including whether it was a `reply` or an `error`.

## Known behaviour

**All hermes output except the answer goes to stderr.** With `-Q`, stdout
carries only the answer text — warnings, the "Session … found but has no
messages" notice, and the `session_id:` line all go to stderr. The parser
strips them anyway as a guard, but only from leading lines, so a real answer
beginning with "Warning:" survives.

**Do not set `REASONING`.** At `low`, the model printed literal tool-call
syntax (`search(foo) search(bar)`) instead of calling tools and returned no
answer. The shim rejects that shape rather than speaking it, but the run is
still wasted. Leave it unset.

**Conversation state lives in the thread name.** Each run uses
`-c voice-<device>-<bucket> --create-if-missing`, so nothing is tracked in the
bridge and context survives restarts. `THREAD_BUCKET=daily` gives each device a
fresh thread each day. `none` gives one permanent thread per device — watch
context growth.

**The voice preamble is load-bearing.** Without the explicit "output only the
answer" instruction, the model narrates the instruction back
("Here are today's top headlines, optimized for text-to-speech:") and TTS
reads it aloud.

---

## Tool policy — read before going live

This bridge makes voice an execution path into an agent that has terminal and
file access. A false wake word plus a misheard sentence is otherwise sufficient
to run something.

`TOOLSETS` is empty by default, which means **voice runs inherit whatever
`config.yaml` allows, including terminal**. That is acceptable while you are
the only one triggering runs by curl. It is not acceptable once the Echo is
listening.

Before that point:

1. Find the valid toolset names — `web_search` and `web_extract` are *tool*
   names and are rejected. Check `config.yaml` or `hermes toolsets`.
2. Set `TOOLSETS` to the minimum voice needs.
3. **Verify the restriction actually bites.** Unknown names only produce a
   warning and the run proceeds with default tools — a typo silently leaves
   you unrestricted. Ask a restricted run to do something requiring `terminal`
   and confirm it cannot.

Never add `--yolo` to the invocation.

## Open items

- Toolset restriction, above — the one item blocking unattended use.
- View Assist display integration (block 3 in the automations snippet):
  service names vary by version.
- `sensor.vaca_*_tts` suggests VACA may have a native announcement path that
  handles ducking and screen state better than a generic `tts.speak`. Worth
  checking the View Assist docs.
- The `Unknown toolsets: a2a, messaging` warning is pre-existing config drift,
  unrelated to this bridge. `--safe-mode` isolates whether it comes from your
  config.
- Multiple satellites: `device` is hardcoded in the ask automation. Resolve it
  from `trigger.device_id` when a second Echo appears.
