import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from elevenlabs_tts import (
    KNOWN_MODELS,
    OUTPUT_FORMATS,
    DictionaryLocator,
    ElevenLabsClient,
    Job,
    VoiceSettings,
    chunk_text,
    fetch_voices,
    make_chunks,
    output_extension,
    parse_id_list,
    read_api_key,
    require_external_path,
    supports_request_stitching,
    split_sections,
)


def sample_job(directory: Path, **changes):
    values = dict(
        source=directory / "book.md",
        output_dir=directory / "Audio",
        api_key_file=None,
        api_base="https://api.elevenlabs.io",
        voice_id="voice/test",
        model_id="eleven_multilingual_v2",
        output_format="mp3_44100_128",
        streaming=False,
        enable_logging=True,
        optimize_streaming_latency=None,
        language_code="en",
        voice_settings=VoiceSettings(0.5, 0.75, 0.0, 1.0, True),
        dictionaries=(DictionaryLocator("dictionary", "version"),),
        seed=42,
        previous_text="before",
        next_text="after",
        previous_request_ids=("prior-id",),
        next_request_ids=("next-id",),
        auto_context=True,
        stitch_request_ids=True,
        apply_text_normalization="auto",
        apply_language_text_normalization=False,
        use_pvc_as_ivc=False,
        max_chars=5000,
        timeout=30,
        retries=0,
        retry_delay=0,
        silence_seconds=0,
        overwrite=False,
        merge=False,
    )
    values.update(changes)
    return Job(**values)


class TextProcessingTests(unittest.TestCase):
    def test_v4_is_available_as_a_model_choice(self):
        self.assertIn("eleven_v4", KNOWN_MODELS)

    def test_numbered_sections(self):
        self.assertEqual(
            [(1, "Hello."), (12, "Goodbye.")],
            split_sections("# 01 First\nHello.\n# 12 Last\nGoodbye."),
        )

    def test_unheaded_document(self):
        self.assertEqual([(1, "Hello")], split_sections(" Hello "))

    def test_chunk_limit_and_content(self):
        chunks = chunk_text("First sentence. Second sentence is longer. Third.", 25)
        self.assertTrue(all(len(item) <= 25 for item in chunks))
        self.assertEqual("First sentence. Second sentence is longer. Third.", " ".join(chunks))

    def test_make_chunks_marks_section_end(self):
        chunks = make_chunks("# 3 Test\nOne. Two.", 5)
        self.assertEqual(3, chunks[0].section)
        self.assertTrue(chunks[-1].ends_section)

    def test_all_documented_formats_and_extensions(self):
        self.assertEqual(28, len(OUTPUT_FORMATS))
        self.assertEqual("mp3", output_extension("mp3_44100_128"))
        self.assertEqual("ulaw", output_extension("ulaw_8000"))


class AuthenticationTests(unittest.TestCase):
    def test_environment_key_is_supported(self):
        self.assertEqual("env-secret", read_api_key(None, {"ELEVENLABS_API_KEY": " env-secret "}))

    def test_external_key_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outside.env"
            path.write_text("# secret\nexport ELEVENLABS_API_KEY='file-secret'\n", encoding="utf-8")
            self.assertEqual("file-secret", read_api_key(path, {"ELEVENLABS_API_KEY": "other-secret"}))

    def test_request_id_limit(self):
        self.assertEqual(("one", "two"), parse_id_list("one, two"))
        with self.assertRaises(ValueError):
            parse_id_list("1,2,3,4")

    def test_key_file_must_be_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            with self.assertRaisesRegex(ValueError, "outside this repository"):
                require_external_path(repository / "secret.env", repository)
            external = Path(directory) / "secret.env"
            self.assertEqual(external.resolve(), require_external_path(external, repository))


class FakeResponse:
    def __init__(self, *, data=b"audio", headers=None, json_data=None):
        self.status_code = 200
        self.headers = headers or {}
        self._data = data
        self._json_data = json_data
        self.text = ""
        self.reason = "OK"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _size):
        yield self._data

    def json(self):
        return self._json_data


class ApiTests(unittest.TestCase):
    def test_speech_request_contains_every_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = sample_job(root, streaming=True, optimize_streaming_latency=3)
            client = ElevenLabsClient("secret", job, threading.Event())
            response = FakeResponse(headers={"request-id": "generated-id"})
            client.session.post = Mock(return_value=response)
            destination = root / "audio.mp3"

            request_id = client.synthesize(
                "hello", destination, previous_text="previous", next_text="next",
                previous_request_ids=("old",),
            )

            self.assertEqual("generated-id", request_id)
            self.assertEqual(b"audio", destination.read_bytes())
            _, kwargs = client.session.post.call_args
            self.assertTrue(client.session.post.call_args.args[0].endswith("/voice%2Ftest/stream"))
            self.assertEqual("secret", kwargs["headers"]["xi-api-key"])
            self.assertEqual("mp3_44100_128", kwargs["params"]["output_format"])
            self.assertEqual(3, kwargs["params"]["optimize_streaming_latency"])
            body = kwargs["json"]
            self.assertEqual("en", body["language_code"])
            self.assertEqual(42, body["seed"])
            self.assertEqual("dictionary", body["pronunciation_dictionary_locators"][0]["pronunciation_dictionary_id"])
            self.assertEqual(["old"], body["previous_request_ids"])
            self.assertEqual(["next-id"], body["next_request_ids"])
            self.assertEqual(1.0, body["voice_settings"]["speed"])
            self.assertIn("apply_text_normalization", body)
            self.assertIn("apply_language_text_normalization", body)
            self.assertIn("use_pvc_as_ivc", body)

    def test_v3_omits_unsupported_request_stitching_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = sample_job(root, model_id="eleven_v3")
            client = ElevenLabsClient("secret", job, threading.Event())
            client.session.post = Mock(return_value=FakeResponse())

            client.synthesize(
                "hello", root / "audio.mp3", previous_text="previous", next_text="next",
                previous_request_ids=("old",),
            )

            body = client.session.post.call_args.kwargs["json"]
            for field in ("previous_text", "next_text", "previous_request_ids", "next_request_ids"):
                self.assertNotIn(field, body)
            self.assertFalse(supports_request_stitching("eleven_v3"))
            self.assertTrue(supports_request_stitching("eleven_multilingual_v2"))

    @patch("elevenlabs_tts.requests.get")
    def test_voice_catalog_paginates(self, get):
        first = FakeResponse(json_data={
            "voices": [{"voice_id": "one", "name": "One"}],
            "has_more": True,
            "next_page_token": "page-two",
        })
        second = FakeResponse(json_data={
            "voices": [{"voice_id": "two", "name": "Two"}],
            "has_more": False,
        })
        get.side_effect = [first, second]
        voices = fetch_voices("secret")
        self.assertEqual(["one", "two"], [item["voice_id"] for item in voices])
        self.assertEqual("page-two", get.call_args_list[1].kwargs["params"]["next_page_token"])


if __name__ == "__main__":
    unittest.main()
