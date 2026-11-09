#!/usr/bin/env python3
"""Tkinter desktop batch client for the ElevenLabs Text to Speech API."""

from __future__ import annotations

import json
import math
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable

from elevenlabs_tts import (
    API_BASES,
    DEFAULT_API_BASE,
    KNOWN_MODELS,
    LANGUAGE_CODES,
    OUTPUT_FORMATS,
    Cancelled,
    DictionaryLocator,
    Job,
    VoiceSettings,
    fetch_models,
    fetch_voices,
    parse_id_list,
    read_api_key,
    require_external_path,
    run_job,
)


THEMES = {
    "Light": {
        "background": "#f5f6f8", "surface": "#ffffff", "field": "#ffffff",
        "foreground": "#20242a", "muted": "#5f6670", "border": "#c9ced6",
        "accent": "#345ecb", "accent_hover": "#294ca7", "selection": "#b9cdfc",
    },
    "Dark": {
        "background": "#1e2024", "surface": "#292c32", "field": "#202226",
        "foreground": "#f0f2f5", "muted": "#b3b8c2", "border": "#4a4f59",
        "accent": "#7394f5", "accent_hover": "#8ba7fa", "selection": "#3f568f",
    },
}


class ElevenLabsTTSApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ElevenLabs Batch Studio")
        self.geometry("1080x920")
        self.minsize(860, 700)
        self.vars: dict[str, tk.Variable] = {}
        self.saved_settings = self._load_settings()
        self.voice_ids: dict[str, str] = {}
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.theme_name = str(self.saved_settings.get("theme", "Light"))
        if self.theme_name not in THEMES:
            self.theme_name = "Light"
        self._apply_theme(self.theme_name)
        self._build()
        self._watch_settings()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def variable(self, name: str, default: object, kind: type[tk.Variable] = tk.StringVar) -> tk.Variable:
        saved = self.saved_settings.get(name, default)
        if kind is tk.BooleanVar and not isinstance(saved, bool):
            saved = default
        elif kind is tk.StringVar and not isinstance(saved, str):
            saved = default
        variable = kind(value=saved)
        self.vars[name] = variable
        return variable

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        self.theme_canvas = canvas
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        canvas_window = canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))

        appearance = ttk.Frame(self.form)
        appearance.pack(fill="x", pady=(0, 4))
        ttk.Label(appearance, text="Appearance").pack(side="left")
        theme_box = ttk.Combobox(
            appearance, textvariable=self.variable("theme", self.theme_name),
            values=tuple(THEMES), state="readonly", width=9,
        )
        theme_box.pack(side="left", padx=(8, 18))
        theme_box.bind("<<ComboboxSelected>>", self._theme_changed)
        ttk.Label(
            appearance,
            text="Your key is read from ELEVENLABS_API_KEY or an external file; it is never stored by this app.",
        ).pack(side="left")

        files = ttk.LabelFrame(self.form, text="Files and authentication", padding=10)
        files.pack(fill="x", pady=(0, 8))
        self._path_row(files, 0, "Markdown source", "source", self._pick_source)
        self._path_row(files, 1, "External API key file", "api_key_file", self._pick_key)
        self._path_row(files, 2, "Output folder", "output_dir", self._pick_output)
        ttk.Label(files, text="Leave the key file blank to use ELEVENLABS_API_KEY.").grid(row=3, column=1, sticky="w", padx=8)
        files.columnconfigure(1, weight=1)

        api = ttk.LabelFrame(self.form, text="Account choices and endpoint", padding=10)
        api.pack(fill="x", pady=8)
        ttk.Label(api, text="API region/base URL").grid(row=0, column=0, sticky="w")
        ttk.Combobox(api, textvariable=self.variable("api_base", DEFAULT_API_BASE), values=API_BASES).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=8, pady=3,
        )
        ttk.Label(api, text="Voice").grid(row=1, column=0, sticky="w")
        self.voice_box = ttk.Combobox(api, textvariable=self.variable("voice", ""))
        self.voice_box.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        self.load_voices_button = ttk.Button(api, text="Load my voices", command=self._start_load_voices)
        self.load_voices_button.grid(row=1, column=2, sticky="w")
        ttk.Label(api, text="(or type a voice ID)").grid(row=1, column=3, sticky="w", padx=8)
        ttk.Label(api, text="Model").grid(row=2, column=0, sticky="w")
        self.model_box = ttk.Combobox(
            api, textvariable=self.variable("model", "eleven_multilingual_v2"), values=KNOWN_MODELS,
        )
        self.model_box.grid(row=2, column=1, sticky="ew", padx=8, pady=3)
        self.load_models_button = ttk.Button(api, text="Load TTS models", command=self._start_load_models)
        self.load_models_button.grid(row=2, column=2, sticky="w")
        ttk.Label(api, text="Language code").grid(row=3, column=0, sticky="w")
        ttk.Combobox(api, textvariable=self.variable("language_code", ""), values=LANGUAGE_CODES, width=10).grid(
            row=3, column=1, sticky="w", padx=8, pady=3,
        )
        ttk.Label(api, text="Blank = automatic; ISO 639-1; ignored by multilingual v2.").grid(
            row=3, column=2, columnspan=2, sticky="w",
        )
        api.columnconfigure(1, weight=1)

        audio = ttk.LabelFrame(self.form, text="Output and delivery", padding=10)
        audio.pack(fill="x", pady=8)
        ttk.Label(audio, text="Output format").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            audio, textvariable=self.variable("output_format", "mp3_44100_128"),
            values=OUTPUT_FORMATS, state="readonly", width=22,
        ).grid(row=1, column=0, sticky="ew", padx=(0, 10))
        ttk.Label(audio, text="Request endpoint").grid(row=0, column=1, sticky="w")
        ttk.Combobox(
            audio, textvariable=self.variable("delivery", "Standard"),
            values=("Standard", "Streaming"), state="readonly", width=14,
        ).grid(row=1, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(audio, text="Logging/history").grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(
            audio, text="Enable request logging", variable=self.variable("enable_logging", True, tk.BooleanVar),
        ).grid(row=1, column=2, sticky="w")
        ttk.Label(audio, text="Latency optimization (deprecated)").grid(row=0, column=3, sticky="w")
        ttk.Combobox(
            audio, textvariable=self.variable("latency", "None"),
            values=("None", "0", "1", "2", "3", "4"), state="readonly", width=9,
        ).grid(row=1, column=3, sticky="w")
        for column in range(4):
            audio.columnconfigure(column, weight=1)

        voice = ttk.LabelFrame(self.form, text="Per-request voice settings", padding=10)
        voice.pack(fill="x", pady=8)
        ttk.Checkbutton(
            voice, text="Override the voice's saved settings for every request",
            variable=self.variable("override_voice_settings", False, tk.BooleanVar),
            command=self._voice_settings_changed,
        ).grid(row=0, column=0, columnspan=5, sticky="w")
        voice_fields = (
            ("Stability (0–1)", "stability", "0.5"),
            ("Similarity (0–1)", "similarity", "0.75"),
            ("Style (0–1)", "style", "0"),
            ("Speed (0.7–1.2)", "speed", "1"),
        )
        self.voice_setting_entries: list[ttk.Entry] = []
        for column, (label, name, default) in enumerate(voice_fields):
            ttk.Label(voice, text=label).grid(row=1, column=column, sticky="w", pady=(7, 0))
            entry = ttk.Entry(voice, textvariable=self.variable(name, default), width=14)
            entry.grid(row=2, column=column, sticky="ew", padx=(0, 10))
            self.voice_setting_entries.append(entry)
            voice.columnconfigure(column, weight=1)
        self.speaker_boost = ttk.Checkbutton(
            voice, text="Speaker boost", variable=self.variable("speaker_boost", True, tk.BooleanVar),
        )
        self.speaker_boost.grid(row=2, column=4, sticky="w")

        dictionaries = ttk.LabelFrame(self.form, text="Pronunciation dictionaries (up to 3, applied in order)", padding=10)
        dictionaries.pack(fill="x", pady=8)
        ttk.Label(dictionaries, text="Dictionary ID").grid(row=0, column=0, sticky="w")
        ttk.Label(dictionaries, text="Version ID (blank = latest)").grid(row=0, column=1, sticky="w")
        for row in range(1, 4):
            ttk.Entry(dictionaries, textvariable=self.variable(f"dictionary_{row}_id", "")).grid(
                row=row, column=0, sticky="ew", padx=(0, 10), pady=2,
            )
            ttk.Entry(dictionaries, textvariable=self.variable(f"dictionary_{row}_version", "")).grid(
                row=row, column=1, sticky="ew", pady=2,
            )
        dictionaries.columnconfigure(0, weight=1)
        dictionaries.columnconfigure(1, weight=1)

        context = ttk.LabelFrame(self.form, text="Continuity, reproducibility, and normalization", padding=10)
        context.pack(fill="x", pady=8)
        context_fields = (
            ("Seed (0–4294967295; blank = random)", "seed", ""),
            ("Previous text", "previous_text", ""),
            ("Next text", "next_text", ""),
            ("Previous request IDs (comma-separated, max 3)", "previous_request_ids", ""),
            ("Next request IDs (comma-separated, max 3)", "next_request_ids", ""),
        )
        for row, (label, name, default) in enumerate(context_fields):
            ttk.Label(context, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(context, textvariable=self.variable(name, default)).grid(row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=2)
        ttk.Checkbutton(
            context, text="Use adjacent chunks as previous/next text",
            variable=self.variable("auto_context", True, tk.BooleanVar),
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            context, text="Stitch generated request IDs",
            variable=self.variable("stitch_request_ids", False, tk.BooleanVar),
        ).grid(row=5, column=2, sticky="w", pady=(7, 0))
        ttk.Label(context, text="Text normalization").grid(row=6, column=0, sticky="w", pady=(7, 0))
        ttk.Combobox(
            context, textvariable=self.variable("text_normalization", "auto"),
            values=("auto", "on", "off"), state="readonly", width=9,
        ).grid(row=6, column=1, sticky="w", padx=8, pady=(7, 0))
        ttk.Checkbutton(
            context, text="Language text normalization (Japanese; slower)",
            variable=self.variable("language_normalization", False, tk.BooleanVar),
        ).grid(row=6, column=2, sticky="w", pady=(7, 0))
        ttk.Checkbutton(
            context, text="Use PVC as IVC (deprecated)",
            variable=self.variable("use_pvc_as_ivc", False, tk.BooleanVar),
        ).grid(row=7, column=2, sticky="w", pady=(7, 0))
        context.columnconfigure(1, weight=1)
        context.columnconfigure(2, weight=1)

        workflow = ttk.LabelFrame(self.form, text="Batch workflow", padding=10)
        workflow.pack(fill="x", pady=8)
        workflow_fields = (
            ("Max characters/request", "max_chars", "400"),
            ("Timeout (seconds)", "timeout", "180"),
            ("Retries", "retries", "2"),
            ("Retry delay (seconds)", "retry_delay", "2"),
            ("Section silence (seconds)", "silence", "4"),
        )
        for column, (label, name, default) in enumerate(workflow_fields):
            ttk.Label(workflow, text=label).grid(row=0, column=column, sticky="w")
            ttk.Entry(workflow, textvariable=self.variable(name, default), width=13).grid(
                row=1, column=column, sticky="ew", padx=(0, 8),
            )
            workflow.columnconfigure(column, weight=1)
        ttk.Label(workflow, text="End-of-file silence (seconds; 0 = off)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0),
        )
        ttk.Entry(workflow, textvariable=self.variable("end_silence", "0"), width=13).grid(
            row=2, column=2, sticky="w", pady=(8, 0),
        )
        ttk.Checkbutton(
            workflow, text="Overwrite existing files", variable=self.variable("overwrite", False, tk.BooleanVar),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            workflow, text="Create one merged audio file", variable=self.variable("merge", False, tk.BooleanVar),
        ).grid(row=3, column=2, columnspan=2, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self.form)
        actions.pack(fill="x", pady=8)
        self.start_button = ttk.Button(actions, text="Generate Audio", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.status = ttk.Label(actions, text="Ready")
        self.status.pack(side="right")

        log_frame = ttk.LabelFrame(self.form, text="Activity", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_box = scrolledtext.ScrolledText(log_frame, height=9, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self._voice_settings_changed()
        self._apply_theme(self.theme_name)

    @staticmethod
    def _settings_path() -> Path:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return config_home / "elevenlabs-batch-studio" / "settings.json"

    def _load_settings(self) -> dict[str, object]:
        try:
            value = json.loads(self._settings_path().read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            # The old default was 5000. Move existing installs to the new
            # prose-sized default once, while preserving later user edits.
            if value.get("chunk_default_version") != 2 and value.get("max_chars") == "5000":
                value["max_chars"] = "400"
            return value
        except (OSError, ValueError, AttributeError):
            return {}

    def _save_settings(self, *_args: object) -> None:
        try:
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # These are GUI choices and an optional path only. The key itself is never persisted.
            settings = {name: variable.get() for name, variable in self.vars.items()}
            settings["chunk_default_version"] = 2
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass

    def _watch_settings(self) -> None:
        for variable in self.vars.values():
            variable.trace_add("write", self._save_settings)

    def _close(self) -> None:
        self.cancel_event.set()
        self._save_settings()
        self.destroy()

    def _theme_changed(self, _event: tk.Event | None = None) -> None:
        self.theme_name = str(self.vars["theme"].get())
        self._apply_theme(self.theme_name)

    def _apply_theme(self, name: str) -> None:
        palette = THEMES.get(name, THEMES["Light"])
        bg, surface, field = palette["background"], palette["surface"], palette["field"]
        fg, muted, border = palette["foreground"], palette["muted"], palette["border"]
        accent, hover = palette["accent"], palette["accent_hover"]
        self.configure(background=bg)
        self.style.configure(".", background=bg, foreground=fg, bordercolor=border, troughcolor=surface,
                             selectbackground=palette["selection"], selectforeground=fg)
        self.style.configure("TFrame", background=bg)
        self.style.configure("TLabelframe", background=bg, bordercolor=border)
        self.style.configure("TLabelframe.Label", background=bg, foreground=fg)
        self.style.configure("TLabel", background=bg, foreground=fg)
        self.style.configure("TButton", background=surface, foreground=fg, bordercolor=border, padding=(8, 4))
        self.style.map("TButton", background=[("active", hover), ("pressed", accent)], foreground=[("disabled", muted)])
        self.style.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg, bordercolor=border)
        self.style.map("TEntry", fieldbackground=[("disabled", surface)], foreground=[("disabled", muted)])
        self.style.configure("TCombobox", fieldbackground=field, background=surface, foreground=fg,
                             arrowcolor=fg, bordercolor=border)
        self.style.map("TCombobox", fieldbackground=[("readonly", field)], selectbackground=[("readonly", field)],
                       selectforeground=[("readonly", fg)], foreground=[("disabled", muted)])
        self.style.configure("TCheckbutton", background=bg, foreground=fg)
        self.style.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", muted)])
        self.style.configure("TProgressbar", background=accent, troughcolor=surface, bordercolor=border)
        self.style.configure("TScrollbar", background=surface, troughcolor=bg, bordercolor=border, arrowcolor=fg)
        self.option_add("*TCombobox*Listbox.background", field)
        self.option_add("*TCombobox*Listbox.foreground", fg)
        self.option_add("*TCombobox*Listbox.selectBackground", palette["selection"])
        self.option_add("*TCombobox*Listbox.selectForeground", fg)

        def update_native(widget: tk.Misc) -> None:
            if isinstance(widget, tk.Toplevel):
                widget.configure(background=bg)
            elif isinstance(widget, (tk.Text, tk.Listbox)):
                widget.configure(background=field, foreground=fg, insertbackground=fg,
                                 selectbackground=palette["selection"], selectforeground=fg,
                                 highlightbackground=border, highlightcolor=accent)
            elif isinstance(widget, tk.Canvas):
                widget.configure(background=bg)
            for child in widget.winfo_children():
                update_native(child)
        update_native(self)

    def _path_row(self, parent: ttk.Frame, row: int, label: str, name: str, command: Callable[[], None]) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=self.variable(name, "")).grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(parent, text="Browse…", command=command).grid(row=row, column=2, pady=3)

    def _pick_source(self) -> None:
        value = filedialog.askopenfilename(
            title="Select Markdown source", filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*")],
        )
        if value:
            self.vars["source"].set(value)
            if not self.vars["output_dir"].get():
                self.vars["output_dir"].set(str(Path(value).parent / "Audio"))

    def _pick_key(self) -> None:
        value = filedialog.askopenfilename(
            title="Select external API key file", filetypes=[("Text", "*.txt *.env"), ("All files", "*")],
        )
        if value:
            self.vars["api_key_file"].set(value)

    def _pick_output(self) -> None:
        value = filedialog.askdirectory(title="Select output folder")
        if value:
            self.vars["output_dir"].set(value)

    def _voice_settings_changed(self) -> None:
        state = "normal" if self.vars["override_voice_settings"].get() else "disabled"
        for entry in self.voice_setting_entries:
            entry.configure(state=state)
        self.speaker_boost.configure(state=state)

    def _external_key_path(self) -> Path | None:
        value = str(self.vars["api_key_file"].get()).strip()
        if not value:
            return None
        path = require_external_path(Path(value), Path(__file__).resolve().parent)
        if not path.is_file():
            raise ValueError(f"API key file does not exist: {path}")
        return path

    def _start_load_voices(self) -> None:
        self._start_catalog_load("voices")

    def _start_load_models(self) -> None:
        self._start_catalog_load("models")

    def _start_catalog_load(self, kind: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            key = read_api_key(self._external_key_path())
            api_base = self._validated_api_base()
            timeout = self._positive_int("timeout", "Timeout")
        except (ValueError, OSError) as exc:
            messagebox.showerror(f"Cannot load {kind}", str(exc), parent=self)
            return
        self._set_buttons(True)
        self.status.configure(text=f"Loading {kind}…")
        self.worker = threading.Thread(target=self._catalog_work, args=(kind, key, api_base, timeout), daemon=True)
        self.worker.start()

    def _catalog_work(self, kind: str, key: str, api_base: str, timeout: int) -> None:
        try:
            values = fetch_voices(key, api_base, timeout) if kind == "voices" else fetch_models(key, api_base, timeout)
        except Exception as exc:
            self.after(0, self._finish_catalog_load, kind, None, str(exc))
        else:
            self.after(0, self._finish_catalog_load, kind, values, "")

    def _finish_catalog_load(self, kind: str, values: list[dict[str, object]] | None, error: str) -> None:
        self._disable_busy_state()
        if values is None:
            self.status.configure(text=f"Could not load {kind}")
            self._log(f"Could not load {kind}: {error}")
            messagebox.showerror(f"Could not load {kind}", error, parent=self)
            return
        if kind == "voices":
            labels: list[str] = []
            self.voice_ids.clear()
            for item in values:
                voice_id = str(item.get("voice_id", "")).strip()
                if not voice_id:
                    continue
                label = f"{item.get('name') or 'Unnamed'} — {voice_id}"
                category = str(item.get("category") or "").strip()
                if category:
                    label += f" ({category})"
                labels.append(label)
                self.voice_ids[label] = voice_id
            self.voice_box.configure(values=labels)
            self.status.configure(text=f"{len(labels)} voices loaded")
            self._log(f"Loaded {len(labels)} available voices.")
        else:
            model_ids = [str(item.get("model_id")) for item in values if item.get("model_id")]
            self.model_box.configure(values=model_ids)
            self.status.configure(text=f"{len(model_ids)} TTS models loaded")
            self._log(f"Loaded {len(model_ids)} available text-to-speech models.")

    def _validated_api_base(self) -> str:
        value = str(self.vars["api_base"].get()).strip().rstrip("/")
        if not value.startswith(("https://", "http://")):
            raise ValueError("API base must be an HTTP(S) URL.")
        return value

    def _positive_int(self, name: str, label: str) -> int:
        value = int(str(self.vars[name].get()))
        if value < 1:
            raise ValueError(f"{label} must be at least 1.")
        return value

    def _voice_id(self) -> str:
        value = str(self.vars["voice"].get()).strip()
        if not value:
            raise ValueError("Choose a voice or type a voice ID.")
        if value in self.voice_ids:
            return self.voice_ids[value]
        # A saved display label remains usable after a restart without another catalog load.
        if " — " in value:
            return value.split(" — ", 1)[1].split(" ", 1)[0].strip()
        return value

    def _collect_job(self) -> Job:
        source_value = str(self.vars["source"].get()).strip()
        if not source_value:
            raise ValueError("Select a Markdown source.")
        source = Path(source_value).expanduser()
        if not source.is_file():
            raise ValueError(f"Markdown source does not exist: {source}")
        output_value = str(self.vars["output_dir"].get()).strip()
        if not output_value:
            raise ValueError("Select an output folder.")
        output_dir = Path(output_value).expanduser()
        key_path = self._external_key_path()
        read_api_key(key_path)  # Validate authentication before starting the worker.

        max_chars = self._positive_int("max_chars", "Max characters")
        timeout = self._positive_int("timeout", "Timeout")
        retries = int(str(self.vars["retries"].get()))
        retry_delay = float(str(self.vars["retry_delay"].get()))
        silence = float(str(self.vars["silence"].get()))
        end_silence = float(str(self.vars["end_silence"].get()))
        if retries < 0 or any(not math.isfinite(value) or value < 0
                              for value in (retry_delay, silence, end_silence)):
            raise ValueError("Retries, retry delay, section silence, and end-of-file silence must be nonnegative finite numbers.")

        seed_text = str(self.vars["seed"].get()).strip()
        seed = int(seed_text) if seed_text else None
        if seed is not None and not 0 <= seed <= 4_294_967_295:
            raise ValueError("Seed must be between 0 and 4294967295.")
        latency_text = str(self.vars["latency"].get())
        latency = None if latency_text == "None" else int(latency_text)

        voice_settings = None
        if self.vars["override_voice_settings"].get():
            stability = float(str(self.vars["stability"].get()))
            similarity = float(str(self.vars["similarity"].get()))
            style = float(str(self.vars["style"].get()))
            speed = float(str(self.vars["speed"].get()))
            if not all(0 <= value <= 1 for value in (stability, similarity, style)):
                raise ValueError("Stability, similarity, and style must be between 0 and 1.")
            if not 0.7 <= speed <= 1.2:
                raise ValueError("Speed must be between 0.7 and 1.2.")
            voice_settings = VoiceSettings(stability, similarity, style, speed, bool(self.vars["speaker_boost"].get()))

        locators: list[DictionaryLocator] = []
        for number in range(1, 4):
            dictionary_id = str(self.vars[f"dictionary_{number}_id"].get()).strip()
            version_id = str(self.vars[f"dictionary_{number}_version"].get()).strip()
            if version_id and not dictionary_id:
                raise ValueError(f"Pronunciation dictionary {number} has a version but no dictionary ID.")
            if dictionary_id:
                locators.append(DictionaryLocator(dictionary_id, version_id))

        return Job(
            source=source,
            output_dir=output_dir,
            api_key_file=key_path,
            api_base=self._validated_api_base(),
            voice_id=self._voice_id(),
            model_id=str(self.vars["model"].get()).strip() or "eleven_multilingual_v2",
            output_format=str(self.vars["output_format"].get()),
            streaming=str(self.vars["delivery"].get()) == "Streaming",
            enable_logging=bool(self.vars["enable_logging"].get()),
            optimize_streaming_latency=latency,
            language_code=str(self.vars["language_code"].get()).strip(),
            voice_settings=voice_settings,
            dictionaries=tuple(locators),
            seed=seed,
            previous_text=str(self.vars["previous_text"].get()).strip(),
            next_text=str(self.vars["next_text"].get()).strip(),
            previous_request_ids=parse_id_list(str(self.vars["previous_request_ids"].get())),
            next_request_ids=parse_id_list(str(self.vars["next_request_ids"].get())),
            auto_context=bool(self.vars["auto_context"].get()),
            stitch_request_ids=bool(self.vars["stitch_request_ids"].get()),
            apply_text_normalization=str(self.vars["text_normalization"].get()),
            apply_language_text_normalization=bool(self.vars["language_normalization"].get()),
            use_pvc_as_ivc=bool(self.vars["use_pvc_as_ivc"].get()),
            max_chars=max_chars,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            silence_seconds=silence,
            end_silence_seconds=end_silence,
            overwrite=bool(self.vars["overwrite"].get()),
            merge=bool(self.vars["merge"].get()),
        )

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            job = self._collect_job()
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cannot start", str(exc), parent=self)
            return
        self.cancel_event.clear()
        self.progress.configure(value=0, maximum=1)
        self._log(f"Starting {job.model_id} with voice {job.voice_id}; key source: " +
                  ("environment" if job.api_key_file is None else "external file"))
        self._set_job_busy(True)
        self.worker = threading.Thread(target=self._work, args=(job,), daemon=True)
        self.worker.start()

    def _work(self, job: Job) -> None:
        try:
            products = run_job(
                job, self.cancel_event,
                lambda message: self.after(0, self._log, message),
                lambda current, total: self.after(0, self._set_progress, current, total),
            )
        except Cancelled as exc:
            self.after(0, self._finish, "Cancelled", str(exc), False)
        except Exception as exc:
            self.after(0, self._finish, "Generation failed", str(exc), False)
        else:
            self.after(0, self._finish, "Complete", f"Created {len(products)} audio file(s) in {job.output_dir}.", True)

    def _set_progress(self, current: int, total: int) -> None:
        self.progress.configure(maximum=total, value=current)
        self.status.configure(text=f"{current}/{total}")

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.status.configure(text="Cancelling…")
        self._log("Cancellation requested; the current network read will stop as soon as possible.")

    def _set_job_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.load_voices_button.configure(state="disabled" if busy else "normal")
        self.load_models_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.status.configure(text="Working…")

    def _disable_busy_state(self) -> None:
        self._set_job_busy(False)

    def _finish(self, title: str, detail: str, success: bool) -> None:
        self._set_job_busy(False)
        self.status.configure(text=title)
        self._log(f"{title}: {detail}")
        self.bell()
        if success:
            messagebox.showinfo(title, detail, parent=self)
        elif title != "Cancelled":
            messagebox.showerror(title, detail, parent=self)

    def _log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_buttons(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.load_voices_button.configure(state="disabled" if busy else "normal")
        self.load_models_button.configure(state="disabled" if busy else "normal")


def main() -> None:
    app = ElevenLabsTTSApp()
    app.mainloop()


if __name__ == "__main__":
    main()
