# ElevenLabs Batch Studio

A Python/Tkinter desktop GUI for turning Markdown or text documents into audio with the hosted ElevenLabs Text to Speech API. It follows the batch workflow and appearance of the sibling Higgs Audio app while exposing the ElevenLabs-specific API controls.

## Features

- Load the voices and text-to-speech models available to your own ElevenLabs account, or enter IDs manually.
- Choose any official API region and all 28 current output formats.
- Use standard or streaming speech generation.
- Configure all current `POST /v1/text-to-speech/{voice_id}` parameters:
  - model and language code
  - stability, similarity boost, style, speed, and speaker boost
  - up to three pronunciation dictionary/version locators
  - seed, previous/next text, and previous/next request IDs
  - text normalization and Japanese language normalization
  - request logging / zero-retention mode (plan permitting)
  - deprecated latency optimization and PVC-as-IVC options, clearly labeled
- Automatically pass adjacent text chunks for continuity and optionally stitch response request IDs.
- Split numeric H1 sections such as `# 01 Chapter` into sentence-aware requests.
- Retry transient failures, resume by skipping existing files, cancel safely, add section-end silence, and optionally merge the output.
- Remember GUI choices in the operating system config directory and support light/dark themes.

## Install and run

```bash
./install.sh
./run_elevenlabs_tts.sh
```

Or install `requests` and run:

```bash
python3 elevenlabs_tts_gui.py
```

Tkinter is included by many Python distributions. On Fedora install `python3-tkinter`; on Debian/Ubuntu install `python3-tk`. `ffmpeg` is needed only for compressed silence and merged MP3/Opus/WAV output. Raw PCM, A-law, and μ-law silence/merging do not need it.

## Keep the API key outside this repository

The app never asks you to paste a key into the GUI and never stores the key in its settings. Use either:

1. An environment variable:

   ```bash
   export ELEVENLABS_API_KEY='your-key-here'
   ./run_elevenlabs_tts.sh
   ```

2. A text or `.env` file located **outside this repository**, selected in the GUI. It may contain a raw key or:

   ```text
   ELEVENLABS_API_KEY=your-key-here
   ```

Only the external file's path is remembered. The repository's `.gitignore` also excludes common key and environment filenames as a second line of defense.

## Batch behavior

A heading such as `# 12 Chapter` produces `12_01.mp3`, `12_02.mp3`, and so on. Documents without numbered H1 headings use section `01`. The extension follows the selected codec (`.mp3`, `.opus`, `.wav`, `.pcm`, `.alaw`, or `.ulaw`).

The default 5,000-character chunk size is safe for Eleven v3. Eleven v4 supports up to 10,000 characters per request. Other models allow different limits, so the GUI keeps it editable. Output formats and some features depend on your subscription tier. Zero-retention mode (`Enable request logging` off) is restricted by ElevenLabs to eligible enterprise accounts.

Eleven v3 does not support request stitching. When that model is selected, the app automatically omits previous/next text and previous/next request IDs even if continuity options remain filled in; other compatible models continue to receive them.

API references: [Create speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert), [stream speech](https://elevenlabs.io/docs/api-reference/text-to-speech/stream), [list voices](https://elevenlabs.io/docs/api-reference/voices/search), and [list models](https://elevenlabs.io/docs/api-reference/models/list).

## Tests

```bash
python3 -m unittest -v
```
