# claude-code-ptt — Bauplan: Kern + Host-Adapter, terminal-unabhängige Zustellung

> Stand 09.08.2026. Architektur mit Jogi Schritt für Schritt entschieden.
> Ersetzt den ersten Wurf (reiner PTY-Wrapper).

## Leitentscheidung

**Ein stabiler Kern + austauschbare Host-Adapter.** Der Kern läuft überall gleich.
Alles, was pro KI-Werkzeug / pro Host anders ist (Installationsweg, Rückkanal,
Ankunfts-Bestätigung), steckt in einem eigenen Adapter-Modul, das je nach
Umgebung aktiviert wird. Neue Umgebung = neues Modul, Kern bleibt unangetastet.

## Zwei Zustellwege, nie beide gleichzeitig

Pro Nachricht wählt der Kern **genau einen** Weg — nie zwei, damit ein Text
niemals in zwei Sessions landet:

1. **Fokus-Paste (universeller Standard).** Merkt sich das zuletzt fokussierte
   Eingabefeld, fügt dort per Clipboard + Enter ein. Funktioniert überall, wo es
   ein fokussierbares Eingabefeld gibt — jedes Terminal UND GUI-Apps (in Jogis
   altem PTT nachweislich bis in die Claude-Desktop-App). Grenze: liefert immer
   an den **letzten Fokus** — kann keine bestimmte Hintergrund-Session gezielt
   treffen. Für den Normalfall (eine aktive Session) genau richtig.
2. **Wrapper / PTY (Präzisions-Modus, optional).** `ccptt run -- <cli>` startet
   das CLI in einem eigenen PTY und besitzt dessen Eingabestrom; Zustellung per
   Datei-Inbox → Bracketed-Paste + verzögertes Enter direkt in stdin. Fokus-frei,
   adressiert eine **bestimmte** Session exakt, auch im Hintergrund/Minimiert.
   Nur für Terminal-CLIs.

**Auswahlregel:** Ist im Overlay eine Wrapper-Session gezielt angewählt →
ausschließlich Präzisions-Kanal. Sonst → Fokus-Paste. Ein Ziel, eine Zustellung.

## Kern (host-agnostisch, immer aktiv)

- Mikrofon-Mute/Hotkey-Erkennung (Ctrl+M), WASAPI-Aufnahme
- faster-whisper (lokal, CPU int8), Halluzinationsfilter
- TTS-Synthese (edge-tts), non-blocking Queue, Interrupt bei Aufnahme
- Audio-Cues (Sinus-Töne, Warmup, eigener Output-Stream)
- Schwebendes Overlay (Session-Liste, Phasen-/Zustell-Zustände, atmende Farben,
  Grün = sprechende Session)
- Beide Zustellwege (Fokus-Paste + PTY-Wrapper) + Auswahlregel
- Session-Registry per selbst erzeugter `session_id` (nicht hwnd — Termux hat keins)

## Host-Adapter (aktiviert je nach Umgebung)

Jeder Adapter liefert nur: (a) seinen Installationsweg für den Rückkanal,
(b) den Rückkanal `ptt_speak`, (c) die Ankunfts-Bestätigung.

- **claude-code (V1):** MCP-Server (`ptt_speak`, Registrierung) via
  `claude mcp add --scope user`; Ankunfts-Bestätigung via UserPromptSubmit-Hook,
  der die `session_id` zurückmeldet.
- **codex (später):** dessen MCP-/Hook-Äquivalent.
- **claude-desktop (später, best effort):** eigene Config-Datei; Zustellung nur
  über Fokus-Paste; Bestätigung ggf. nicht möglich → ehrlich als „ohne
  Ankunfts-Garantie" markiert.

## Bau-Phasen (klein, einzeln testbar) — V1 = Kern + claude-code-Adapter

- **P0 — Fokus-Paste-Kern.** Aufnehmen → Whisper → Fokus-Paste ins zuletzt
  fokussierte Feld. *Test:* funktioniert in cmd, PowerShell, und Claude-Desktop.
- **P1 — Overlay + Session-Registry (session_id).** *Test:* aktive Session sichtbar,
  Einzel-Session automatisch Ziel.
- **P2 — PTY-Wrapper-Beweis.** `ccptt run -- claude` = transparent wie nacktes
  claude. *Test:* Tippen/Farben/Ctrl+C/Resize identisch.
- **P3 — Inbox-Zustellung ins PTY.** Bracketed-Paste + verzögertes `\r`. *Test:*
  lange/mehrzeilige Texte kommen atomar als abgeschickter Prompt an.
- **P4 — Auswahlregel.** Session angewählt → Präzisions-Kanal, sonst Fokus-Paste;
  nie beides. *Test:* zwei Sessions, Klick A→A, Klick B→B, kein Doppel-Eingang.
- **P5 — claude-code-Adapter: Rückkanal + Bestätigung.** MCP `ptt_speak`,
  UserPromptSubmit-Hook meldet `session_id`. *Test:* Zustellung an A nur für A
  bestätigt; Fremd-Hook → nicht bestätigt.
- **P6 — Zustell-Zustände im Overlay.** REC/Transkription/Senden/Angekommen +
  Fehler-Rahmen bei ausbleibender Bestätigung.
- **P7 — Packaging + Portabilitäts-Check.** `pipx install`, `ccptt`-Shim,
  Hook-Auto-Install. *Test:* frische Maschine (nur Python + Claude Code), keine
  Jogi-only-Abhängigkeit.
- **P8 (später) — Cross-Platform-Backend (ptyprocess/Termux) + codex-Adapter.**

## Abhängigkeiten (Portabilität)

Nur normale pip-Pakete: faster-whisper, sounddevice/pyaudiowpatch, edge-tts,
pywinpty (Windows-PTY) bzw. ptyprocess (Linux/Termux), mcp. Nichts aus Jogis
Setup (kein Jarvis, kein HUD). Frische-Maschinen-Check ist Pflicht (P7).
