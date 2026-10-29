# AGENTS.md

## Install

1. macOS only. Clone to `~/OpenSpeaksy` unless told otherwise:
   `git clone https://github.com/slilbudget/OpenSpeaksy.git ~/OpenSpeaksy`
2. Get the user's Mistral API key (<https://console.mistral.ai/api-keys>).
3. Run `MISTRAL_API_KEY=... ./scripts/install.sh`. Its own key prompt is a hidden `read` you can't answer. If it has to install Homebrew, that step needs the user's password: hand it to them.
4. The user must allow `<repo>/venv/bin/python` under Input Monitoring and Accessibility (System Settings → Privacy & Security). No scripted path. launchd relaunches the app every few seconds until the grant lands.
5. Done when `~/Library/Logs/com.openspeaksy/main.log` shows `OpenSpeaksy running`.
6. Tell the user the keys: hold right ⌘ or right ⌥ to dictate; both together for hands-free, tap either to stop; hold right ⇧ Russian → English; Caps Lock cancels a stuck transcription.

## Code

`main.py` (state machine, keys, paste, watchdog, recovery), `transcriber.py` (Mistral STT, translation, vocabulary, command mood), `overlay.py` (pill), `recorder.py`, `launchd/com.openspeaksy.plist.template`. Tests: `./venv/bin/python -m pytest`.

Invariants that are easy to break:

- **State** changes only via `_begin_recording`, `_abandon_recording_cycle`, `begin_processing`, `_claim_job_completion`, `handle_shutdown`, `_latch_hands_free` and the watchdog. Anything that pastes, deletes a pending file or touches the overlay must hold a claim. `current_job_id` voids stale workers; a stale paste lands in whatever the user is doing now.
- **`current_hotkey`** owns the cycle: key-ups from other keys are ignored. Right ⌘ and right ⌥ are both dictation keys (`DICTATION_KEYS`); a hands-free stop tap on the non-owner ends the cycle through its owner (`_finish_hands_free`).
- **Event tap** is listen-only on `kCGEventFlagsChanged`. Never subscribe to key events: it would see every keystroke. Every hotkey is therefore a modifier. Use `_other_input_since` (system idle counters) to detect a key or click during a tap.
- **Hands-free**: the other dictation key's state comes from the event's own flags, never a remembered key-down. The stop tap counts only if `_other_input_since` is false.
- **Caps Lock** is a toggle: one event per press, handle every event. Cancel acts only on work older than `CANCEL_GRACE_SEC`, stays silent otherwise, and never deletes audio.
- **Audio** is written atomically to `.pending/` (0700/0600) before any request; the mode is in the filename. Recovery copies to the clipboard, never pastes, and runs before the tap starts.
- **Retries**: `_pending_failures` (quarantine, ignores outages), `_pending_attempt_count` (backoff) and `_pending_next_attempt` are separate on purpose. EPIPE/ECONNRESET/ETIMEDOUT/ENOTCONN are `ProviderUnavailableError`: pause, don't count.
- **Mistral errors**: read the body, not `str(HTTPError)`, and only through the cached `_http_error_text`. 429 code `1300` = quota, fail fast; `3505` = capacity, wait it out. Chat calls use `retry_throttling=False`.
- **Vocabulary**: `vocabulary.txt` (gitignored) feeds Voxtral `context_bias` (max 100; a comma is HTTP 400 for the whole request, and so is whitespace on Transcribe 2 models, `_ONE_WORD_BIAS_MODELS`) plus `heard => written` whole-word fixes (one pass) and `term = meaning` glossary lines for the translator. Reloaded on mtime change; a missing or broken file never fails a dictation. Write terms in the script they are spoken in: Latin terms barely steer Russian audio.
- **Timeouts** grow with the input (`TRANSCRIPTION_TIMEOUT_SEC_PER_AUDIO_MIN`, `CHAT_TIMEOUT_CHARS_PER_SEC`): the server is silent while it works, and a 20 min take outlasts the flat 30 s.
- **Command mood** (`OPENSPEAKSY_COMMAND_MOOD`, off by default): `_as_commands` turns a first-person verb into the imperative per sentence; "я" anywhere in the sentence or `_NOT_A_COMMAND_BEFORE` within three words keeps it. Runs before the vocabulary fixes.
- **Translation**: the dictation goes inside `<dictation>` tags (`_dictation_message`), or small models answer questions instead of translating them. `mistral-large-4` reasons on every call: `reasoning_effort` goes only to `_REASONING_MODEL_PREFIXES` (others answer 400), and its reply is a list of blocks (`_chat_text`). A failed call falls back once to `MISTRAL_TRANSLATION_FALLBACK_MODEL`, never on `ProviderUnavailableError`.
- **Silence** is judged by `wav_has_speech` (loudest 100 ms), not average level.
- **Plist env overrides code defaults.** Changing a model means both places.
- Log lengths, paths and errors only. Never transcripts, never the key. No `print()`.
- The transcript always stays in the clipboard. No "restore old clipboard".
- Renaming the `com.openspeaksy` label means `LOG_DIR` in `main.py` and both scripts. Moving the repo means re-running `install.sh`: the plist holds absolute paths.
