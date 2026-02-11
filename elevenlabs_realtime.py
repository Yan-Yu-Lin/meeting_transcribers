#!/usr/bin/env python3
"""ElevenLabs Scribe v2 Realtime meeting transcriber (VAD-friendly)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import os
import queue
import shutil
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from elevenlabs import (
    AudioFormat,
    CommitStrategy,
    ElevenLabs,
    RealtimeAudioOptions,
    RealtimeEvents,
)


CHANNELS = 1

SAMPLE_RATE_TO_FORMAT = {
    8000: AudioFormat.PCM_8000,
    16000: AudioFormat.PCM_16000,
    22050: AudioFormat.PCM_22050,
    24000: AudioFormat.PCM_24000,
    44100: AudioFormat.PCM_44100,
    48000: AudioFormat.PCM_48000,
}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class ElevenLabsRealtimeTranscriber:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.chunk_samples = int(args.sample_rate * args.chunk_ms / 1000)
        self.stop_event = threading.Event()
        self.audio_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=args.max_audio_queue_chunks
        )

        load_dotenv()
        api_key = args.api_key or os.getenv("ELEVENLABS_API_KEY")
        if not api_key:
            raise SystemExit(
                "ERROR: ELEVENLABS_API_KEY is required (env or --api-key)."
            )

        if args.sample_rate not in SAMPLE_RATE_TO_FORMAT:
            allowed = ", ".join(str(v) for v in SAMPLE_RATE_TO_FORMAT)
            raise SystemExit(
                f"ERROR: Unsupported sample rate {args.sample_rate}. Use: {allowed}"
            )

        self.client = ElevenLabs(api_key=api_key)

        session_name = (
            args.session_name or f"scribe_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )
        self.session_dir = Path(args.output_root).expanduser().resolve() / session_name
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.segments_dir = self.session_dir / "segments"
        self.segments_dir.mkdir(parents=True, exist_ok=True)

        self.stream_wav_path = self.session_dir / "stream.wav"
        self.transcript_path = self.session_dir / "transcript.txt"
        self.transcript_plain_path = self.session_dir / "transcript_plain.txt"

        self.write_lock = threading.Lock()
        self.segment_index = 0
        self.last_partial_text = ""
        self.partial_line_active = False
        self.partial_render_len = 0
        self.last_committed_text = ""
        self.last_committed_time = 0.0

        self.stream_wav = wave.open(str(self.stream_wav_path), "wb")
        self.stream_wav.setnchannels(CHANNELS)
        self.stream_wav.setsampwidth(2)
        self.stream_wav.setframerate(args.sample_rate)

        with self.transcript_path.open("w", encoding="utf-8") as f:
            f.write("ElevenLabs Realtime Transcript\n")
            f.write(f"Started: {now_stamp()}\n")
            f.write(f"Model: {args.model}\n")
            f.write(f"Commit strategy: {args.commit_strategy}\n")
            f.write(f"Sample rate: {args.sample_rate}\n")
            f.write("\n")

        with self.transcript_plain_path.open("w", encoding="utf-8") as f:
            f.write("")

    def _fit_partial_for_terminal(self, text: str) -> str:
        width = shutil.get_terminal_size(fallback=(120, 20)).columns
        max_len = max(20, width - 1)
        if len(text) <= max_len:
            return text
        return "..." + text[-(max_len - 3) :]

    def _render_partial_line(self, text: str) -> None:
        rendered = self._fit_partial_for_terminal(text)
        clear = " " * self.partial_render_len
        print(f"\r{clear}\r{rendered}", end="", flush=True)
        self.partial_line_active = True
        self.partial_render_len = len(rendered)

    def _dedupe_committed_text(self, text: str) -> str:
        current = (text or "").strip()
        if not current:
            return ""

        previous = self.last_committed_text
        now = time.monotonic()

        if not previous:
            self.last_committed_text = current
            self.last_committed_time = now
            return current

        # If commits are far apart, treat as independent segments.
        if now - self.last_committed_time > 8.0:
            self.last_committed_text = current
            self.last_committed_time = now
            return current

        if current == previous:
            return ""

        if current.startswith(previous):
            delta = current[len(previous) :].lstrip()
            self.last_committed_text = current
            self.last_committed_time = now
            return delta

        if previous.startswith(current):
            return ""

        max_overlap = min(len(previous), len(current), 220)
        overlap = 0
        for size in range(max_overlap, 19, -1):
            if previous[-size:] == current[:size]:
                overlap = size
                break

        self.last_committed_text = current
        self.last_committed_time = now
        if overlap > 0:
            return current[overlap:].lstrip()
        return current

    def _print_committed_line(self, line: str) -> None:
        if self.args.show_partial and self.partial_line_active:
            clear = " " * self.partial_render_len
            # Overwrite the current rolling partial line with final text.
            print(f"\r{clear}\r{line}")
            self.partial_line_active = False
            self.partial_render_len = 0
            self.last_partial_text = ""
        else:
            print(line)

    def _append_transcript(self, text: str) -> None:
        with self.write_lock:
            with self.transcript_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n")

    def _append_plain(self, text: str) -> None:
        with self.write_lock:
            with self.transcript_plain_path.open("a", encoding="utf-8") as f:
                f.write(text + "\n\n")

    def _audio_callback(self, indata, frames, time_info, status):
        del time_info

        if status and self.args.display == "full":
            print(f"[audio] {status}")

        frame = indata[:, 0].copy().reshape(-1)
        if frames != self.chunk_samples:
            padded = np.zeros((self.chunk_samples,), dtype=np.int16)
            keep = min(len(frame), self.chunk_samples)
            padded[:keep] = frame[:keep]
            frame = padded
        else:
            frame = frame.astype(np.int16)

        audio_bytes = frame.tobytes()
        with self.write_lock:
            self.stream_wav.writeframes(audio_bytes)

        try:
            self.audio_queue.put_nowait(audio_bytes)
        except queue.Full:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.audio_queue.put_nowait(audio_bytes)
            except queue.Full:
                pass

    def _handle_committed_text(self, text: str) -> None:
        text = self._dedupe_committed_text(text)
        if not text:
            return

        self.segment_index += 1
        stamp = now_stamp()
        seg_txt = self.segments_dir / f"seg_{self.segment_index:05d}.txt"
        seg_txt.write_text(text + "\n", encoding="utf-8")

        terminal_line = text if self.args.display == "text" else f"[{stamp}] {text}"

        if self.args.display == "text":
            self._print_committed_line(terminal_line)
        else:
            self._print_committed_line(terminal_line)

        self._append_transcript(f"[{stamp}] {text}")
        self._append_transcript(f"segment_text: {seg_txt}")
        self._append_transcript("")
        self._append_plain(text)

    def _on_session_started(self, data):
        if self.args.display == "full":
            print("Connected to ElevenLabs Realtime")

    def _on_partial_transcript(self, data):
        if not self.args.show_partial:
            return

        text = (data.get("text") or "").strip()
        if text:
            if text == self.last_partial_text:
                return

            # Render partial on one rolling terminal line (no repeated spam lines).
            self._render_partial_line(text)
            self.last_partial_text = text

    def _on_committed_transcript(self, data):
        if self.args.include_timestamps:
            return
        self._handle_committed_text(data.get("text", ""))

    def _on_committed_with_timestamps(self, data):
        # The API can emit both committed events; avoid duplicate output by only
        # consuming timestamped event when include_timestamps is enabled.
        if self.args.include_timestamps:
            self._handle_committed_text(data.get("text", ""))

    def _on_error(self, data):
        if self.args.show_partial and self.partial_line_active:
            clear = " " * self.partial_render_len
            print(f"\r{clear}\r")
            self.partial_line_active = False
            self.partial_render_len = 0
            self.last_partial_text = ""
        if self.args.display == "full":
            print(f"error: {data}")

    def _on_close(self, *_):
        if self.args.show_partial and self.partial_line_active:
            clear = " " * self.partial_render_len
            print(f"\r{clear}\r")
            self.partial_line_active = False
            self.partial_render_len = 0
            self.last_partial_text = ""
        if self.args.display == "full":
            print("Connection closed")

    async def _sender_loop(self, connection) -> None:
        while not self.stop_event.is_set() or not self.audio_queue.empty():
            try:
                audio_bytes = self.audio_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.002)
                continue

            payload = {
                "audio_base_64": base64.b64encode(audio_bytes).decode("utf-8"),
                "sample_rate": self.args.sample_rate,
            }
            await connection.send(payload)

    def _build_options(self) -> RealtimeAudioOptions:
        strategy = (
            CommitStrategy.VAD
            if self.args.commit_strategy == "vad"
            else CommitStrategy.MANUAL
        )

        kwargs = {
            "model_id": self.args.model,
            "audio_format": SAMPLE_RATE_TO_FORMAT[self.args.sample_rate],
            "sample_rate": self.args.sample_rate,
            "commit_strategy": strategy,
            "include_timestamps": self.args.include_timestamps,
        }

        if self.args.language_code:
            kwargs["language_code"] = self.args.language_code

        if self.args.commit_strategy == "vad":
            kwargs["vad_silence_threshold_secs"] = self.args.vad_silence_threshold_secs
            kwargs["vad_threshold"] = self.args.vad_threshold
            kwargs["min_speech_duration_ms"] = self.args.min_speech_duration_ms
            kwargs["min_silence_duration_ms"] = self.args.min_silence_duration_ms

        return RealtimeAudioOptions(**kwargs)

    async def run(self) -> None:
        options = self._build_options()
        realtime_client = getattr(self.client.speech_to_text, "realtime", None)
        if realtime_client is None:
            raise RuntimeError("Realtime client unavailable in current ElevenLabs SDK")
        connection = await realtime_client.connect(options)

        connection.on(RealtimeEvents.SESSION_STARTED, self._on_session_started)
        connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, self._on_partial_transcript)
        connection.on(
            RealtimeEvents.COMMITTED_TRANSCRIPT, self._on_committed_transcript
        )
        connection.on(
            RealtimeEvents.COMMITTED_TRANSCRIPT_WITH_TIMESTAMPS,
            self._on_committed_with_timestamps,
        )
        connection.on(RealtimeEvents.ERROR, self._on_error)
        connection.on(RealtimeEvents.CLOSE, self._on_close)

        if self.args.display == "full":
            print("=" * 68)
            print("ElevenLabs Scribe v2 Realtime transcriber")
            print("=" * 68)
            print(f"Session folder: {self.session_dir}")
            print(f"Transcript: {self.transcript_path}")
            print(f"Plain transcript: {self.transcript_plain_path}")
            print(f"Stream audio: {self.stream_wav_path}")
            print("Listening... press Ctrl+C to stop.")
            print("")

        sender_task = asyncio.create_task(self._sender_loop(connection))

        try:
            with sd.InputStream(
                samplerate=self.args.sample_rate,
                channels=CHANNELS,
                dtype=np.int16,
                blocksize=self.chunk_samples,
                latency="low",
                callback=self._audio_callback,
            ):
                while not self.stop_event.is_set():
                    await asyncio.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_event.set()
            await sender_task
            await connection.commit()
            await asyncio.sleep(self.args.final_commit_wait_secs)
            await connection.close()
            with self.write_lock:
                self.stream_wav.close()

            if self.args.display == "full":
                print("Done.")
                print(f"Saved in: {self.session_dir}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ElevenLabs Scribe v2 Realtime meeting transcriber"
    )
    parser.add_argument("--api-key", default=None, help="Override ELEVENLABS_API_KEY")
    parser.add_argument(
        "--model", default="scribe_v2_realtime", help="Realtime STT model ID"
    )
    parser.add_argument(
        "--commit-strategy",
        choices=["vad", "manual"],
        default="vad",
        help="Segment commit strategy (default: vad)",
    )
    parser.add_argument(
        "--sample-rate", type=int, default=16000, help="PCM sample rate"
    )
    parser.add_argument(
        "--chunk-ms", type=int, default=60, help="Audio chunk size in ms"
    )
    parser.add_argument(
        "--language-code", default=None, help="Optional language code hint"
    )
    parser.add_argument(
        "--include-timestamps", action="store_true", help="Request word timestamps"
    )

    parser.add_argument(
        "--vad-silence-threshold-secs",
        type=float,
        default=0.45,
        help="Silence seconds before VAD auto-commit",
    )
    parser.add_argument(
        "--vad-threshold", type=float, default=0.25, help="VAD threshold"
    )
    parser.add_argument(
        "--min-speech-duration-ms",
        type=int,
        default=50,
        help="Minimum speech duration for VAD",
    )
    parser.add_argument(
        "--min-silence-duration-ms",
        type=int,
        default=50,
        help="Minimum silence duration for VAD",
    )

    parser.add_argument(
        "--display",
        choices=["text", "full"],
        default="text",
        help="Terminal output mode (default: text)",
    )
    parser.set_defaults(show_partial=True)
    parser.add_argument(
        "--show-partial",
        dest="show_partial",
        action="store_true",
        help="Show rolling live partial transcript (default)",
    )
    parser.add_argument(
        "--no-partial",
        dest="show_partial",
        action="store_false",
        help="Disable live partial transcript and show committed lines only",
    )
    parser.add_argument(
        "--output-root",
        default="recordings",
        help="Root folder for session outputs",
    )
    parser.add_argument(
        "--session-name", default=None, help="Optional session folder name"
    )
    parser.add_argument(
        "--max-audio-queue-chunks",
        type=int,
        default=200,
        help="In-memory audio queue capacity",
    )
    parser.add_argument(
        "--final-commit-wait-secs",
        type=float,
        default=0.8,
        help="Wait time after final commit before close",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    transcriber = ElevenLabsRealtimeTranscriber(args)
    asyncio.run(transcriber.run())


if __name__ == "__main__":
    main()
