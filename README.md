# claude-code-ptt

Push-to-talk for [Claude Code](https://claude.com/claude-code) on Windows.

Press **Ctrl+M** to unmute, speak, press **Ctrl+M** again — your words are
transcribed locally with Whisper and typed straight into your most recently
focused Claude Code session. Claude's replies can be spoken back to you with a
free Microsoft Edge neural voice.

> **Status: early development — not usable yet.** Watch/star the repo if you
> want to follow along.

## How it will work

- **One background daemon** owns the global hotkey (Ctrl+M), the microphone,
  local Whisper transcription, text injection and text-to-speech.
- **A thin MCP server** (installed once with `claude mcp add --scope user`)
  connects every Claude Code session to that daemon and lets Claude speak.
- **Target selection:** transcripts go to the most recently focused Claude
  Code window; `/ptt-here` pins the current session as the target.

## Planned features

- Local transcription (OpenAI Whisper, no cloud, any language Whisper knows)
- Spoken replies via `edge-tts` (free neural voices)
- Mic auto-mutes while Claude is speaking; unmuting interrupts the speech
- Works with any number of parallel sessions

## Requirements (planned)

- Windows 10/11
- Python 3.10+
- Claude Code

## License

MIT
