# Meeting Transcribers

Two standalone entry scripts for realtime transcription experiments.

- `local_realtime.py`: local/offline ASR path (reuses `dictation_realtime.py` engine).
- `elevenlabs_realtime.py`: cloud realtime path using ElevenLabs Scribe v2 Realtime.

## Setup

From this folder:

```bash
uv sync
```

If you prefer pip, there is also a `requirements.txt` file:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Local (offline)

```bash
uv run python local_realtime.py --model small
```

Notes:
- This wrapper launches `../Dictation-Local-SenseVoice/dictation_realtime.py` via `uv run --project ...`.
- This entry defaults to `--display text` for cleaner terminal output.
- Add `--display full` if you want full status logs.

## ElevenLabs Realtime (cloud)

```bash
uv run python elevenlabs_realtime.py
```

Default behavior is rolling partial output on one line, then overwrite that line
with the final committed text when a segment is committed.
Use `--no-partial` if you want committed transcript lines only.

Optional example with VAD tuning:

```bash
uv run python elevenlabs_realtime.py \
  --commit-strategy vad \
  --vad-silence-threshold-secs 1.2 \
  --vad-threshold 0.4
```

Environment:
- Set `ELEVENLABS_API_KEY` in your environment or `.env`.

Outputs are stored under `recordings/<session_name>/`.
