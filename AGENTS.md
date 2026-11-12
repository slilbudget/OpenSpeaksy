# AGENTS.md

## Install

1. macOS only. Clone to `~/OpenSpeaksy` unless told otherwise:
   `git clone https://github.com/slilbudget/OpenSpeaksy.git ~/OpenSpeaksy`
2. Ask the user to create a Mistral API key (<https://console.mistral.ai/api-keys>) and copy it to the clipboard. Don't have them paste it into the chat: it would sit in your transcript.
3. Run `MISTRAL_API_KEY="$(pbpaste)" ./scripts/install.sh` and never echo the variable. The script's own key prompt is a hidden `read` you can't answer. If it has to install Homebrew, that step needs the user's password: hand it to them.
4. The user must allow `<repo>/venv/bin/python` under Input Monitoring and Accessibility (System Settings → Privacy & Security), and the Microphone prompt on the first recording. No scripted path. launchd relaunches the app every few seconds until the grant lands.
5. Done when `~/Library/Logs/com.openspeaksy/main.log` shows `OpenSpeaksy running`.
6. Tell the user the keys: hold right ⌘ or right ⌥ to dictate; both together for hands-free, tap either to stop; hold right ⇧ Russian → English; Caps Lock cancels a stuck transcription, twice retries it.

## Code

`main.py` (state machine, keys, paste, watchdog, recovery), `transcriber.py` (Mistral STT, translation, vocabulary, command mood), `overlay.py` (pill), `recorder.py`, `launchd/com.openspeaksy.plist.template`. Tests: `./venv/bin/python -m pytest`.

Invariants that are easy to break:

- **State** is the recording slot only (`idle`/`recording`); finished takes live in `_jobs` (spoken order) while they transcribe in parallel. It changes only via `_begin_recording`, `_abandon_recording_cycle`, `begin_processing` (frees the slot and queues the job in one step), `_drop_job`, `handle_shutdown`, `_latch_hands_free`, `cancel_everything` and the watchdog. A worker whose job left `_jobs` was voided: it drops its result, the file stays queued.
- **Pasting** is `_drain_ready_jobs` only: the done takes at the head of `_jobs`, in order, joined into one paste, never while `_modifiers_held()` (⌘V would merge with a held ⌥ or ⇧). The tap drains when the last modifier comes up; a held drain rechecks every `DRAIN_RECHECK_SEC` (one timer). A modifier held `MODIFIER_HOLD_MAX_SEC` with a done head and nothing recording (`_held_since`) counts as stuck: copy, don't paste. `paste_text` looks at the modifiers again right before posting. A refused paste falls back to the clipboard ("Copied · ⌘V"); only a failed copy keeps the files. The drain holds `_clipboard_gate` from taking jobs out of `_jobs` to deleting their files, and recovery globs and snapshots `_jobs` under the same gate (and skips the pass while a take has no path yet), so no file is ever between owners. Recovery never writes the clipboard while a take is live: it keeps the text in `_recovered_text` for the next pass. `on_key_up` stops the recorder before `begin_processing` frees the slot. `PASTE_SETTLE_SEC` spaces pastes so an app reads the clipboard before it changes. The pill is `_refresh_overlay`'s; flashes return to it, and none covers a recording longer than `RECORDING_FLASH_MAX_SEC`.
- **`current_hotkey`** owns the cycle: key-ups from other keys are ignored. Right ⌘ and right ⌥ are both dictation keys (`DICTATION_KEYS`); a hands-free stop tap on the non-owner ends the cycle through its owner (`_finish_hands_free`).
- **Event tap** is listen-only on `kCGEventFlagsChanged`. Never subscribe to key events: it would see every keystroke. Every hotkey is therefore a modifier. Use `_other_input_since` (system idle counters) to detect a key or click during a tap.
- **Hands-free**: the other dictation key's state comes from the event's own flags, never a remembered key-down. The stop tap counts only if `_other_input_since` is false.
- **Caps Lock** is a toggle: one event per press, handle every event. Cancel acts only on unfinished work older than `CANCEL_GRACE_SEC` (younger and done takes keep going and paste; voided files are held by name), stays silent otherwise, and never deletes audio.
- **Audio** is written atomically to `.pending/` (0700/0600) before any request; the mode is in the filename. Recovery copies to the clipboard, never pastes, and runs before the tap starts.
- **Retries**: `_pending_failures` (quarantine, ignores outages), `_pending_attempt_count` (backoff) and `_pending_next_attempt` are separate on purpose. EPIPE/ECONNRESET/ETIMEDOUT/ENOTCONN are `ProviderUnavailableError`: pause, don't count.
- **Mistral errors**: read the body, not `str(HTTPError)`, and only through the cached `_http_error_text`. 429 code `1300` = quota, fail fast; `3505` = capacity, wait it out. Chat calls use `retry_throttling=False`.
- **Vocabulary**: `vocabulary.txt` (gitignored) feeds Voxtral `context_bias` (max 100; a comma is HTTP 400 for the whole request, and so is whitespace on Transcribe 2 models, `_ONE_WORD_BIAS_MODELS`) plus `heard => written` whole-word fixes (one pass) and `term = meaning` glossary lines for the translator. Reloaded on mtime change; a missing or broken file never fails a dictation. Write terms in the script they are spoken in: Latin terms barely steer Russian audio.
- **Timeouts** grow with the input (`TRANSCRIPTION_TIMEOUT_SEC_PER_AUDIO_MIN`, `CHAT_TIMEOUT_CHARS_PER_SEC`): the server is silent while it works, and a 20 min take outlasts the flat 30 s.
- **Command mood** (`OPENSPEAKSY_COMMAND_MOOD`, off by default): `_as_commands` turns a first-person verb into the imperative per sentence; "я" anywhere in the sentence or `_NOT_A_COMMAND_BEFORE` within three words keeps it. Runs before the vocabulary fixes.
- **Translation**: the dictation goes inside `<dictation>` tags (`_dictation_message`), or small models answer questions instead of translating them. `mistral-large-4` reasons on every call: `reasoning_effort` goes only to `_REASONING_MODEL_PREFIXES` (others answer 400), and its reply is a list of blocks (`_chat_text`). A failed call falls back once to `MISTRAL_TRANSLATION_FALLBACK_MODEL`, never on `ProviderUnavailableError`.
- **STT fallback**: a refused primary (`fail_fast`: one attempt) hands the take to `MISTRAL_FALLBACK_MODEL` (Transcribe 2, one-word terms only); a quota/access refusal (429 code 1300, 403) skips the primary for `PRIMARY_STT_SKIP_SEC`. Never on 401, `ProviderUnavailableError` or an oversized upload.
- **Silence** is judged by `wav_has_speech` (loudest 100 ms), not average level.
- **Plist env overrides code defaults.** Changing a model means both places.
- Log lengths, file names and errors only. Never transcripts, never the key. No `print()`. Provider error bodies lose any `input` field (`_without_echo`): a validation error can quote the dictation back.
- The transcript always stays in the clipboard. No "restore old clipboard".
- Renaming the `com.openspeaksy` label means `LOG_DIR` in `main.py` and both scripts. Moving the repo means re-running `install.sh`: the plist holds absolute paths.
