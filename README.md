# claude-code-ptt

Push-to-talk for [Claude Code](https://claude.com/claude-code) on Windows.

Press **Ctrl+M** (configurable) to unmute, speak, press it again — your words
are transcribed locally with Whisper and pasted into the Claude Code session
you picked in the floating overlay, prefixed with `[mic] `. Claude's replies
can be spoken back to you with a free Microsoft Edge neural voice.

> **Status: early development.** The core loop works (hotkey → record →
> local Whisper → inject → delivery confirmation, spoken replies), but the
> packaging and install story is not finished yet. Watch/star the repo if
> you want to follow along.

## How it works

- **One background daemon** owns the global hotkey, the microphone, local
  Whisper transcription, text injection, text-to-speech and a floating
  overlay that lists every running Claude Code session — click a row to
  pick the delivery target.
- **A thin MCP server** (installed once with `claude mcp add --scope user`)
  connects every Claude Code session to that daemon and lets Claude speak.
- **Proven delivery:** hooks report back when the injected text is actually
  processed. The overlay shows the whole journey — recording, transcribing,
  sending, delivered — and if the session is busy working, the prompt is
  shown as queued instead of failed and confirms when the turn ends.

## Features

- Local transcription (faster-whisper, no cloud, any language Whisper knows)
- Spoken replies via `edge-tts` (free neural voices)
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
