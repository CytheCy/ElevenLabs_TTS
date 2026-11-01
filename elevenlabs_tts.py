#!/usr/bin/env python3
"""Core helpers for the ElevenLabs Batch Studio desktop application."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import requests


DEFAULT_API_BASE = "https://api.elevenlabs.io"
API_BASES = (
    "https://api.elevenlabs.io",
    "https://api.us.elevenlabs.io",
    "https://api.eu.residency.elevenlabs.io",
    "https://api.in.residency.elevenlabs.io",
    "https://api.sg.residency.elevenlabs.io",
)
OUTPUT_FORMATS = (
    "alaw_8000",
    "mp3_22050_32", "mp3_24000_48", "mp3_44100_32", "mp3_44100_64",
    "mp3_44100_96", "mp3_44100_128", "mp3_44100_192",
    "opus_48000_32", "opus_48000_64", "opus_48000_96", "opus_48000_128", "opus_48000_192",
    "pcm_8000", "pcm_16000", "pcm_22050", "pcm_24000", "pcm_32000", "pcm_44100", "pcm_48000",
    "ulaw_8000",
    "wav_8000", "wav_16000", "wav_22050", "wav_24000", "wav_32000", "wav_44100", "wav_48000",
)
KNOWN_MODELS = (
    "eleven_v4",
    "eleven_v3",
    "eleven_multilingual_v2",
    "eleven_flash_v2_5",
    "eleven_flash_v2",
    "eleven_turbo_v2_5",
    "eleven_turbo_v2",
    "eleven_monolingual_v1",
    "eleven_multilingual_v1",
)
LANGUAGE_CODES = (
    "", "ar", "bg", "cs", "da", "de", "el", "en", "es", "fi", "fil", "fr", "he", "hi", "hr",
    "hu", "id", "it", "ja", "ko", "ms", "nl", "no", "pl", "pt", "ro", "ru", "sk", "sv", "ta",
    "tr", "uk", "vi", "zh",
)


@dataclass(frozen=True)
class Chunk:
    section: int
    index: int
    text: str
    ends_section: bool = False


@dataclass(frozen=True)
class DictionaryLocator:
    dictionary_id: str
    version_id: str = ""


@dataclass(frozen=True)
class VoiceSettings:
    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool


@dataclass(frozen=True)
class Job:
    source: Path
    output_dir: Path
    api_key_file: Path | None
    api_base: str
    voice_id: str
    model_id: str
    output_format: str
    streaming: bool
    enable_logging: bool
    optimize_streaming_latency: int | None
    language_code: str
    voice_settings: VoiceSettings | None
    dictionaries: tuple[DictionaryLocator, ...]
    seed: int | None
    previous_text: str
    next_text: str
    previous_request_ids: tuple[str, ...]
    next_request_ids: tuple[str, ...]
    auto_context: bool
    stitch_request_ids: bool
    apply_text_normalization: str
    apply_language_text_normalization: bool
    use_pvc_as_ivc: bool
    max_chars: int
    timeout: int
    retries: int
    retry_delay: float
    silence_seconds: float
    overwrite: bool
    merge: bool


class Cancelled(RuntimeError):
    pass


def read_api_key(path: Path | None = None, environ: dict[str, str] | None = None) -> str:
    """Read ELEVENLABS_API_KEY from the environment or an external text file."""
    environment = os.environ if environ is None else environ
    if path is None:
        key = environment.get("ELEVENLABS_API_KEY", "").strip()
        if key:
            return key
        raise ValueError("Set ELEVENLABS_API_KEY or select an external API key file.")
    text = path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" in line:
            name, value = line.split("=", 1)
            if name.strip() not in {"ELEVENLABS_API_KEY", "XI_API_KEY", "API_KEY"}:
                continue
            line = value.strip()
        value = line.strip().strip("\"'").strip()
        if value:
            return value
    raise ValueError("The selected file does not contain an ElevenLabs API key.")


def require_external_path(path: Path, repository: Path) -> Path:
    """Reject a secret file located in (or symlinked into) the repository."""
    resolved = path.expanduser().resolve()
    root = repository.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise ValueError("For safety, the API key file must be stored outside this repository.")


def split_sections(text: str) -> list[tuple[int, str]]:
    """Split on numeric Markdown H1 headings, matching the Higgs Audio workflow."""
    heading = re.compile(r"^#\s*(\d+)\b[^\n]*\n?", re.MULTILINE)
    matches = list(heading.finditer(text))
    if not matches:
        return [(1, text.strip())] if text.strip() else []
    sections: list[tuple[int, str]] = []
    preamble = text[:matches[0].start()].strip()
    if preamble:
        sections.append((0, preamble))
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        if body:
            sections.append((int(match.group(1)), body))
    return sections


def _hard_split(text: str, limit: int) -> Iterable[str]:
    remaining = text.strip()
    while len(remaining) > limit:
        cut = max(remaining.rfind(" ", 0, limit + 1), remaining.rfind("\n", 0, limit + 1))
        if cut < max(1, limit // 2):
            cut = limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].strip()
    if remaining:
        yield remaining


def chunk_text(text: str, limit: int) -> list[str]:
    if limit < 1:
        raise ValueError("Chunk size must be at least 1.")
    units = re.split(r"(?<=[.!?])(?:[\"'”’)]*)\s+|\n{2,}", text.strip())
    result: list[str] = []
    current = ""
    for unit in filter(None, (item.strip() for item in units)):
        for piece in _hard_split(unit, limit):
            candidate = f"{current} {piece}".strip() if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    result.append(current)
                current = piece
    if current:
        result.append(current)
    return result


def make_chunks(markdown: str, max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section, body in split_sections(markdown):
        parts = chunk_text(body, max_chars)
        for index, part in enumerate(parts, 1):
            chunks.append(Chunk(section, index, part, index == len(parts)))
    return chunks


def output_extension(output_format: str) -> str:
    codec = output_format.split("_", 1)[0]
    return {"alaw": "alaw", "ulaw": "ulaw"}.get(codec, codec)


def output_sample_rate(output_format: str) -> int:
    try:
        return int(output_format.split("_")[1])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"Unrecognized output format: {output_format}") from exc


def error_message(response: requests.Response) -> str:
    try:
        value = response.json()
        if isinstance(value, dict):
            detail = value.get("detail", value)
            if isinstance(detail, dict):
                return str(detail.get("message") or detail.get("status") or detail)
            return str(detail)
        return str(value)
    except ValueError:
        return response.text[:500] or response.reason


def parse_id_list(value: str, maximum: int = 3) -> tuple[str, ...]:
    items = tuple(item.strip() for item in re.split(r"[,\n]", value) if item.strip())
    if len(items) > maximum:
        raise ValueError(f"No more than {maximum} request IDs may be supplied.")
    return items


def supports_request_stitching(model_id: str) -> bool:
    """Eleven v3 does not accept text context or request-ID stitching fields."""
    return not model_id.strip().lower().startswith("eleven_v3")


def fetch_models(key: str, api_base: str = DEFAULT_API_BASE, timeout: int = 30) -> list[dict[str, object]]:
    response = requests.get(
        f"{api_base.rstrip('/')}/v1/models", headers={"xi-api-key": key}, timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Models API error {response.status_code}: {error_message(response)}")
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("The models API returned an unexpected response.")
    return [item for item in data if isinstance(item, dict) and item.get("can_do_text_to_speech")]


def fetch_voices(key: str, api_base: str = DEFAULT_API_BASE, timeout: int = 30) -> list[dict[str, object]]:
    voices: list[dict[str, object]] = []
    token: str | None = None
    while True:
        params: dict[str, object] = {"page_size": 100, "include_total_count": "false", "sort": "name", "sort_direction": "asc"}
        if token:
            params["next_page_token"] = token
        response = requests.get(
            f"{api_base.rstrip('/')}/v2/voices", headers={"xi-api-key": key}, params=params, timeout=timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Voices API error {response.status_code}: {error_message(response)}")
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("voices"), list):
            raise RuntimeError("The voices API returned an unexpected response.")
        voices.extend(item for item in data["voices"] if isinstance(item, dict))
        if not data.get("has_more"):
            break
        token = data.get("next_page_token")
        if not isinstance(token, str) or not token:
            break
    return voices


class ElevenLabsClient:
    def __init__(self, key: str, job: Job, cancel: threading.Event):
        self.key = key
        self.job = job
        self.cancel = cancel
        self.session = requests.Session()

    def synthesize(
        self,
        text: str,
        destination: Path,
        *,
        previous_text: str = "",
        next_text: str = "",
        previous_request_ids: tuple[str, ...] = (),
    ) -> str | None:
        body: dict[str, object] = {"text": text, "model_id": self.job.model_id}
        if self.job.language_code:
            body["language_code"] = self.job.language_code
        if self.job.voice_settings is not None:
            settings = self.job.voice_settings
            body["voice_settings"] = {
                "stability": settings.stability,
                "similarity_boost": settings.similarity_boost,
                "style": settings.style,
                "speed": settings.speed,
                "use_speaker_boost": settings.use_speaker_boost,
            }
        if self.job.dictionaries:
            body["pronunciation_dictionary_locators"] = [
                {"pronunciation_dictionary_id": item.dictionary_id, **({"version_id": item.version_id} if item.version_id else {})}
                for item in self.job.dictionaries
            ]
        if self.job.seed is not None:
            body["seed"] = self.job.seed
        if supports_request_stitching(self.job.model_id):
            if previous_text:
                body["previous_text"] = previous_text
            if next_text:
                body["next_text"] = next_text
            if previous_request_ids:
                body["previous_request_ids"] = list(previous_request_ids[-3:])
            if self.job.next_request_ids:
                body["next_request_ids"] = list(self.job.next_request_ids)
        body["apply_text_normalization"] = self.job.apply_text_normalization
        body["apply_language_text_normalization"] = self.job.apply_language_text_normalization
        body["use_pvc_as_ivc"] = self.job.use_pvc_as_ivc

        endpoint = "stream" if self.job.streaming else ""
        url = f"{self.job.api_base.rstrip('/')}/v1/text-to-speech/{quote(self.job.voice_id, safe='')}"
        if endpoint:
            url += f"/{endpoint}"
        params: dict[str, object] = {
            "output_format": self.job.output_format,
            "enable_logging": str(self.job.enable_logging).lower(),
        }
        if self.job.optimize_streaming_latency is not None:
            params["optimize_streaming_latency"] = self.job.optimize_streaming_latency
        headers = {"xi-api-key": self.key, "Accept": "application/octet-stream", "Content-Type": "application/json"}

        last_error: Exception | None = None
        for attempt in range(self.job.retries + 1):
            if self.cancel.is_set():
                raise Cancelled("Cancelled by user.")
            try:
                with self.session.post(
                    url, headers=headers, params=params, json=body,
                    timeout=(15, self.job.timeout), stream=True,
                ) as response:
                    if response.status_code >= 400:
                        message = error_message(response)
                        if response.status_code not in {408, 409, 429} and response.status_code < 500:
                            raise RuntimeError(f"API error {response.status_code}: {message}")
                        raise requests.HTTPError(f"API error {response.status_code}: {message}", response=response)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    try:
                        with temporary.open("wb") as output:
                            for block in response.iter_content(64 * 1024):
                                if self.cancel.is_set():
                                    raise Cancelled("Cancelled by user.")
                                if block:
                                    output.write(block)
                        temporary.replace(destination)
                    except BaseException:
                        temporary.unlink(missing_ok=True)
                        raise
                    return response.headers.get("request-id") or response.headers.get("x-request-id")
            except (Cancelled, RuntimeError):
                raise
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                if attempt >= self.job.retries:
                    break
                time.sleep(self.job.retry_delay * (2 ** attempt))
        raise RuntimeError(f"Request failed after {self.job.retries + 1} attempt(s): {last_error}")


def create_silence(path: Path, output_format: str, seconds: float) -> None:
    if seconds <= 0:
        return
    rate = output_sample_rate(output_format)
    codec = output_format.split("_", 1)[0]
    if codec == "pcm":
        path.write_bytes(b"\x00\x00" * int(rate * seconds))
        return
    if codec == "ulaw":
        path.write_bytes(b"\xff" * int(rate * seconds))
        return
    if codec == "alaw":
        path.write_bytes(b"\xd5" * int(rate * seconds))
        return
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to create compressed section-silence files.")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", f"anullsrc=r={rate}:cl=mono", "-t", str(seconds), "-ar", str(rate), "-ac", "1",
    ]
    parts = output_format.split("_")
    if codec in {"mp3", "opus"} and len(parts) > 2:
        command.extend(["-b:a", f"{parts[2]}k"])
    command.append(str(path))
    subprocess.run(command, check=True)


def merge_audio(paths: list[Path], destination: Path, output_format: str) -> None:
    if not paths:
        return
    codec = output_format.split("_", 1)[0]
    if codec in {"pcm", "ulaw", "alaw"}:
        with destination.open("wb") as target:
            for path in paths:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, target)
        return
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required to merge compressed audio files.")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as listing:
        list_path = Path(listing.name)
        for path in paths:
            escaped = str(path.resolve()).replace("'", "'\\''")
            listing.write(f"file '{escaped}'\n")
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", str(destination),
        ], check=True)
    finally:
        list_path.unlink(missing_ok=True)


def run_job(
    job: Job,
    cancel: threading.Event,
    log: Callable[[str], None],
    progress: Callable[[int, int], None],
) -> list[Path]:
    key = read_api_key(job.api_key_file)
    chunks = make_chunks(job.source.read_text(encoding="utf-8-sig"), job.max_chars)
    if not chunks:
        raise ValueError("The Markdown source file contains no text.")
    job.output_dir.mkdir(parents=True, exist_ok=True)
    client = ElevenLabsClient(key, job, cancel)
    if not supports_request_stitching(job.model_id) and (
        job.auto_context or job.stitch_request_ids or job.previous_text or job.next_text
        or job.previous_request_ids or job.next_request_ids
    ):
        log("Eleven v3 selected: continuity text and request-ID stitching are omitted because this model does not support them.")
    extension = output_extension(job.output_format)
    width = max(2, len(str(max(chunk.section for chunk in chunks))))
    products: list[Path] = []
    generated_request_ids = list(job.previous_request_ids)

    for position, chunk in enumerate(chunks):
        number = position + 1
        if cancel.is_set():
            raise Cancelled("Cancelled by user.")
        filename = f"{chunk.section:0{width}d}_{chunk.index:02d}.{extension}"
        destination = job.output_dir / filename
        prior = job.previous_text
        following = job.next_text
        if job.auto_context:
            if position > 0:
                prior = chunks[position - 1].text
            if position + 1 < len(chunks):
                following = chunks[position + 1].text
        if destination.exists() and not job.overwrite:
            log(f"[{number}/{len(chunks)}] Skipped existing {filename}")
        else:
            log(f"[{number}/{len(chunks)}] Generating {filename} ({len(chunk.text)} characters)…")
            request_id = client.synthesize(
                chunk.text, destination, previous_text=prior, next_text=following,
                previous_request_ids=tuple(generated_request_ids) if job.stitch_request_ids else job.previous_request_ids,
            )
            if request_id and job.stitch_request_ids:
                generated_request_ids.append(request_id)
                generated_request_ids = generated_request_ids[-3:]
        products.append(destination)
        if chunk.ends_section and job.silence_seconds > 0:
            silence = job.output_dir / f"{chunk.section:0{width}d}_{chunk.index:02d}s.{extension}"
            if job.overwrite or not silence.exists():
                create_silence(silence, job.output_format, job.silence_seconds)
            products.append(silence)
        progress(number, len(chunks))

    if job.merge:
        merged = job.output_dir / f"{job.source.stem}_complete.{extension}"
        log(f"Merging {len(products)} files into {merged.name}…")
        merge_audio(products, merged, job.output_format)
        products.append(merged)
    return products
