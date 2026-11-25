<div align="center">

<img src="docs/banner.png" alt="Hold a key. Speak. Done. Open-source dictation for macOS, built on Mistral and Voxtral" width="720">

# OpenSpeaksy

Open-source speech-to-text for macOS, built on [Mistral](https://mistral.ai), with your own API key.

[![CI](https://github.com/slilbudget/OpenSpeaksy/actions/workflows/ci.yml/badge.svg)](https://github.com/slilbudget/OpenSpeaksy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

</div>

## Keys

| Key | Action |
|:---|:---|
| Hold **right ⌘** or **right ⌥** | Dictate |
| **Right ⌘ + right ⌥** | Dictate hands-free. Tap either key to stop |
| Hold **right ⇧** | Speak Russian, paste English |
| **Caps Lock** | Cancel a transcription stuck for over 10 s. The audio is kept; tap twice to retry it now |

Start the next take while the last one is still transcribing: they paste in the order you spoke. Text is pasted at the cursor and stays in the clipboard; if it can't be pasted, the pill says **Copied · ⌘V**.

## Install

Requires macOS 13+ and a [Mistral API key](https://console.mistral.ai/api-keys) with **API pay-as-you-go** on (Admin → Subscription). Without it Mistral refuses the default model, Transcribe 3, and every take falls back to the older `voxtral-mini-2602`. Transcribe 3 costs about $0.004 per minute of audio.

**With an AI agent** (Claude Code, Codex, Cursor). Paste:

```text
Install OpenSpeaksy on my Mac: https://github.com/slilbudget/OpenSpeaksy
Follow AGENTS.md in the repo. When you need my Mistral API key, I'll copy it to the clipboard.
```

**Manually:**

```bash
git clone https://github.com/slilbudget/OpenSpeaksy.git ~/OpenSpeaksy
cd ~/OpenSpeaksy && ./scripts/install.sh
```

Then, either way, macOS asks three times, one after the other: **Allow** the microphone; then twice **Open System Settings** → turn on **OpenSpeaksy** (Input Monitoring for the keys, then Accessibility for the pasting; on the newest macOS: *Device Control and Data Access*). After each switch the app restarts itself. `./scripts/doctor.sh` checks it all and says what's missing.

## Settings

Optional, in `~/Library/LaunchAgents/com.openspeaksy.plist` → `EnvironmentVariables`:

| Variable | Default | Effect |
|:---|:---|:---|
| `OPENSPEAKSY_DICTATE_LANGUAGE` | auto | Force a language, e.g. `en` |
| `MISTRAL_MODEL` | `voxtral-small-transcribe-3` | Speech-to-text model |
| `MISTRAL_FALLBACK_MODEL` | `voxtral-mini-2602` | Used when the main model refuses a take. Set it equal to `MISTRAL_MODEL` to turn it off |
| `MISTRAL_TRANSLATION_MODEL` | `mistral-large-4` | Translation model |
| `MISTRAL_TRANSLATION_FALLBACK_MODEL` | `ministral-8b-latest` | Used once when translation fails. Set it equal to `MISTRAL_TRANSLATION_MODEL` to turn it off |
| `OPENSPEAKSY_COMMAND_MOOD` | `0` | `1`: Russian "проверю" → "проверь", unless the sentence has "я" or не/ни/если/когда/пока/чтобы/как within three words before the verb. For dictating to AI agents |

Apply with:

```bash
launchctl unload ~/Library/LaunchAgents/com.openspeaksy.plist
launchctl load ~/Library/LaunchAgents/com.openspeaksy.plist
```

**Vocabulary.** Jargon, slang or names it mishears: copy [`vocabulary.example.txt`](vocabulary.example.txt) to `vocabulary.txt` and list them, one per line. A `heard => written` line fixes a word that still comes out wrong; a `term = meaning` line tells the translator what your jargon means. Applies on the next dictation.

## Privacy

Audio goes only to Mistral, under your key. No telemetry; transcripts and the key are never logged. To keep your audio out of Mistral's training, turn off Admin → **Privacy** → **Anonymous improvement data**.

## Troubleshooting

| Problem | Fix |
|:---|:---|
| Nothing happens | **Input Monitoring** → OpenSpeaksy on |
| Text is only copied (pill: "Copied · allow Accessibility") | **Accessibility** → OpenSpeaksy on |
| Stopped after `brew upgrade` | Re-run `./scripts/install.sh` (the switches stay on) |
| Pill says **Mic blocked** | Allow **Microphone** |
| Caps Lock types capitals | Keyboard → Modifier Keys → Caps Lock → **No Action** |
| Anything else | `./scripts/doctor.sh`, then `tail -f ~/Library/Logs/com.openspeaksy/main.log` |

## Uninstall

```bash
./scripts/uninstall.sh
```

[MIT](LICENSE). Not affiliated with Mistral AI.
