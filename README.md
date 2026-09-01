# claude-code-ptt

Push-to-talk for [Claude Code](https://claude.com/claude-code) on Windows.

Press **Ctrl+M** (configurable) to unmute, speak, press it again — your words
are transcribed locally with Whisper and pasted into the Claude Code session
you picked in the floating overlay, prefixed with `[mic] `. Claude's replies
can be spoken back to you with a free Microsoft Edge neural voice.

> **Status: early development.** The core loop works (hotkey → record →
> local Whisper → inject → delivery confirmation, spoken replies).
> Watch/star the repo if you want to follow along.

## Install

One line in PowerShell (needs Python 3.10+ and Claude Code installed) —
straight from this repo, so what you run is what you see in
[`install.ps1`](install.ps1):

```powershell
irm https://raw.githubusercontent.com/Jogi-OpenSource/claude-code-ptt/main/install.ps1 | iex
```

It also installs the Microsoft Visual C++ runtime if your machine does not
have it — Whisper's native library needs it, and a fresh Windows does not
ship it.

Or manually, same result:

```powershell
python -m pip install https://github.com/Jogi-OpenSource/claude-code-ptt/archive/main.zip
claude-code-ptt install
```

`claude-code-ptt install` registers the MCP server (`claude mcp add --scope
user`), adds the two delivery-confirmation hooks to
`~/.claude/settings.json` (a backup is written next to it), and downloads
the Whisper model (~0.5 GB, with a progress bar) so your first recording
does not have to wait for it. It is idempotent — rerun it any time, e.g.
after moving Python.

Then start a **new** Claude Code session and press **Ctrl+M**.

## How it works

- **One background daemon** per logged-on Windows account owns the global
  hotkey, the microphone, local Whisper transcription, text injection,
  text-to-speech and a floating overlay that lists every running Claude
  Code session — click a row to pick the delivery target.
- **A thin MCP server** (installed once with `claude mcp add --scope user`)
  connects every Claude Code session to that daemon and lets Claude speak.
- **Proven delivery:** hooks report back when the injected text is actually
  processed. The overlay shows the whole journey — recording, transcribing,
  sending, delivered — and if the session is busy working, the prompt is
  shown as queued instead of failed and confirms when the turn ends.
- **A voice per session:** with several terminals open, every session gets
  its own voice from `tts_voice`'s language, kept for as long as it runs —
  so you hear which one is talking. A session that closes hands its voice
  back to the next one.

## Configuration

Settings live in `%APPDATA%\claude-code-ptt\config.json`, written on first
start. Edit it and restart the daemon (close the overlay, press the hotkey
again) for the changes to take effect.

| Key | Default | What it does |
|---|---|---|
| `language` | `""` | Spoken language, as an ISO code such as `de`, `en`, `es`, `fr`. Empty means Whisper guesses per recording — accurate on clear speech, but near-silence can come back as a random language. Set it if you always speak the same one. |
| `tts_voice` | `en-US-GuyNeural` | Voice for spoken replies. Run `python -m edge_tts --list-voices` to see all of them; pick a name matching your language, e.g. `de-DE-ConradNeural`, `es-ES-AlvaroNeural`. Your first session speaks with it, further ones take the other voices of the same locale. |
| `tts_rate` | `+0%` | Speaking rate for spoken replies. Use a signed percentage such as `+25%` or `-15%`. |
| `tts_pitch` | `+0Hz` | Voice pitch for spoken replies. Use a signed frequency such as `+20Hz` or `-10Hz`. |
| `tts_volume` | `+0%` | Volume for spoken replies. Use a signed percentage such as `+10%` or `-20%`. |
| `whisper_model` | `small` | `tiny`, `base`, `small`, `medium` or `large-v3`. Larger is more accurate and slower, and downloads on first use. |
| `whisper_hotwords` | `""` | Words Whisper should be biased towards — names, commands, jargon it keeps mishearing. |
| `hotkey_modifiers` / `hotkey_key` | `["ctrl"]` / `M` | The push-to-talk hotkey. Modifiers can be `ctrl`, `alt`, `shift`, `win`. |
| `daemon_port` | `8377` | Localhost port the daemon listens on. If a second Windows account is logged on and its daemon already owns that port, the next daemon takes the following free one by itself, so every account keeps its own daemon and overlay. Change this only if something else owns the default. |
| `event_webhook` | `""` | URL that gets a POST for every state change — see below. Empty means nothing is sent. |

### Event webhook

Set `event_webhook` to a URL and the daemon mirrors what it is doing there, so
another app — a status bar, an overlay, a second screen — can follow along:

```json
{"kind": "state", "state": "listening", "level": 0.42}
{"kind": "state", "state": "thinking"}
{"kind": "speak",  "text": "…", "session": "my-project"}
{"kind": "state", "state": "speaking"}
{"kind": "state", "state": "idle"}
```

Every event also carries `"source": "claude-code-ptt"`, so a listener that
receives state from several places can tell whose it is.

`listening` carries the microphone level (0–1, ~10 updates a second) while
recording; `thinking` covers transcription, `speaking` brackets the audible
reply. Delivery is fire-and-forget with a 2 s timeout: a listener that is
slow, broken or gone never delays recording or playback, and nothing is
retried.

## Features

- Local transcription (faster-whisper, no cloud, any language Whisper knows)
- Spoken replies via `edge-tts` (free neural voices), one voice per session
- OS-level mic auto-unmute for recording, previous state restored afterwards
- Starting a recording interrupts Claude's speech
- End-to-end delivery confirmation, queue-aware while the session is busy
- Works with any number of parallel sessions

## Requirements

- Windows 10/11
- Python 3.10+
- Claude Code

## License

MIT
