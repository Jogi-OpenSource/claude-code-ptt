"""User configuration, stored in %APPDATA%/claude-code-ptt/config.json."""
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "claude-code-ptt"


CONFIG_FILE = config_dir() / "config.json"


@dataclass
class Config:
    # Hotkey as modifiers + virtual key letter. Default: Ctrl+M.
    hotkey_modifiers: list[str] = field(default_factory=lambda: ["ctrl"])
    hotkey_key: str = "M"
    # Whisper model name for faster-whisper (tiny/base/small/medium/large-v3).
    whisper_model: str = "small"
    # Language hint for Whisper ("" = autodetect).
    language: str = ""
    # edge-tts voice for spoken replies.
    tts_voice: str = "en-US-GuyNeural"
    # Substrings that identify a Claude Code terminal window title.
    window_title_markers: list[str] = field(
        default_factory=lambda: ["claude"])
    # Daemon HTTP port for the MCP adapter (localhost only).
    daemon_port: int = 8377

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(CONFIG_FILE, encoding="utf-8") as fh:
                known = {f for f in cls.__dataclass_fields__}
                data = {k: v for k, v in json.load(fh).items() if k in known}
                return cls(**data)
        except FileNotFoundError:
            cfg = cls()
            cfg.save()
            return cfg
        except (ValueError, OSError):
            # unreadable config: fall back to defaults without overwriting,
            # so the user can inspect/fix their file
            return cls()

    def save(self) -> None:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
