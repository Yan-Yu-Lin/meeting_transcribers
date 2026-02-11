#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "websockets",
#     "elevenlabs>=2.35.0",
#     "python-dotenv",
# ]
# ///
"""Meeting Transcriber — FastAPI server bridging browser mic to ElevenLabs Scribe v2 Realtime."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shutil
import uuid
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from elevenlabs import (
    AudioFormat,
    CommitStrategy,
    ElevenLabs,
    RealtimeAudioOptions,
    RealtimeEvents,
)
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "meetings"
STATIC_DIR = BASE_DIR / "static"

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.elevenlabs_client = ElevenLabs(api_key=app.state.api_key)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Data directory: {DATA_DIR}")
    yield


app = FastAPI(lifespan=lifespan)

# ---------------------------------------------------------------------------
# Static files + index.html
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/api/meetings")
async def list_meetings():
    meetings = []
    if DATA_DIR.exists():
        for folder in DATA_DIR.iterdir():
            meta_path = folder / "metadata.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["id"] = folder.name
                meetings.append(meta)
    meetings.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return meetings


@app.get("/api/meetings/{meeting_id}")
async def get_meeting(meeting_id: str):
    folder = DATA_DIR / meeting_id
    if not folder.exists():
        return JSONResponse({"error": "Meeting not found"}, status_code=404)
    meta_path = folder / "metadata.json"
    transcript_path = folder / "transcript.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta["id"] = meeting_id
    transcript = json.loads(transcript_path.read_text(encoding="utf-8")) if transcript_path.exists() else []
    return {"metadata": meta, "transcript": transcript}


@app.delete("/api/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str):
    folder = DATA_DIR / meeting_id
    if not folder.exists():
        return JSONResponse({"error": "Meeting not found"}, status_code=404)
    shutil.rmtree(folder)
    return {"ok": True}


@app.get("/api/meetings/{meeting_id}/audio")
async def get_audio(meeting_id: str):
    audio_path = DATA_DIR / meeting_id / "audio.webm"
    if not audio_path.exists():
        return JSONResponse({"error": "Audio not found"}, status_code=404)
    return FileResponse(audio_path, media_type="audio/webm")


@app.post("/api/meetings/{meeting_id}/audio")
async def upload_audio(meeting_id: str, request: Request):
    folder = DATA_DIR / meeting_id
    folder.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    (folder / "audio.webm").write_bytes(body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSocket bridge: browser <-> ElevenLabs Scribe v2 Realtime
# ---------------------------------------------------------------------------

@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()

    # Wait for start message
    try:
        raw = await ws.receive_text()
        start_msg = json.loads(raw)
        if start_msg.get("type") != "start":
            await ws.send_json({"type": "error", "message": "Expected start message"})
            await ws.close()
            return
    except (WebSocketDisconnect, json.JSONDecodeError):
        return

    title = start_msg.get("title", "Untitled Meeting")
    language = start_msg.get("language")

    # Create meeting folder
    meeting_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    meeting_dir = DATA_DIR / meeting_id
    meeting_dir.mkdir(parents=True, exist_ok=True)

    created_at = dt.datetime.now().isoformat()
    metadata = {
        "title": title,
        "created_at": created_at,
        "status": "recording",
        "duration": 0,
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # Tell browser the meeting ID
    await ws.send_json({"type": "session_started", "meeting_id": meeting_id})

    # ElevenLabs connection
    client: ElevenLabs = app.state.elevenlabs_client
    event_queue: asyncio.Queue = asyncio.Queue()

    options = RealtimeAudioOptions(
        model_id="scribe_v2_realtime",
        audio_format=AudioFormat.PCM_16000,
        sample_rate=16000,
        commit_strategy=CommitStrategy.VAD,
    )
    if language:
        options["language_code"] = language

    try:
        connection = await client.speech_to_text.realtime.connect(options)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": f"ElevenLabs connection failed: {exc}"})
        await ws.close()
        return

    # Sync event handlers push to async queue
    def on_partial(data):
        event_queue.put_nowait(("partial", data))

    def on_committed(data):
        event_queue.put_nowait(("committed", data))

    def on_error(data):
        event_queue.put_nowait(("error", data))

    def on_close(*_args):
        event_queue.put_nowait(("close", None))

    connection.on(RealtimeEvents.PARTIAL_TRANSCRIPT, on_partial)
    connection.on(RealtimeEvents.COMMITTED_TRANSCRIPT, on_committed)
    connection.on(RealtimeEvents.ERROR, on_error)
    connection.on(RealtimeEvents.CLOSE, on_close)

    segments: list[dict] = []
    stop_requested = False
    el_closed = False
    start_time = asyncio.get_event_loop().time()

    async def browser_to_elevenlabs():
        """Read audio/stop messages from browser, relay audio to ElevenLabs."""
        nonlocal stop_requested
        try:
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "audio":
                    audio_b64 = msg.get("data", "")
                    if audio_b64:
                        await connection.send({"audio_base_64": audio_b64})

                elif msg_type == "stop":
                    stop_requested = True
                    return
        except WebSocketDisconnect:
            stop_requested = True
        except Exception:
            stop_requested = True

    async def elevenlabs_to_browser():
        """Drain event queue, forward to browser, collect committed segments."""
        nonlocal el_closed
        while True:
            try:
                event_type, data = await asyncio.wait_for(event_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if stop_requested and event_queue.empty():
                    return
                continue

            try:
                if event_type == "partial":
                    text = (data.get("text") or "").strip()
                    if text:
                        await ws.send_json({"type": "partial", "text": text})

                elif event_type == "committed":
                    text = (data.get("text") or "").strip()
                    if text:
                        segments.append({"text": text, "timestamp": dt.datetime.now().isoformat()})
                        await ws.send_json({"type": "committed", "text": text})

                elif event_type == "error":
                    message = data.get("error") or data.get("message") or str(data)
                    await ws.send_json({"type": "error", "message": message})

                elif event_type == "close":
                    el_closed = True
                    return
            except WebSocketDisconnect:
                return
            except Exception:
                return

    # Run both tasks concurrently
    browser_task = asyncio.create_task(browser_to_elevenlabs())
    relay_task = asyncio.create_task(elevenlabs_to_browser())

    try:
        # Wait for browser task to finish (stop or disconnect)
        await browser_task

        # Final commit + drain
        if not el_closed:
            try:
                await connection.commit()
            except Exception:
                pass
            await asyncio.sleep(0.8)

        # Let relay drain remaining events
        await asyncio.wait_for(relay_task, timeout=3.0)
    except asyncio.TimeoutError:
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass
    except Exception:
        relay_task.cancel()
        try:
            await relay_task
        except asyncio.CancelledError:
            pass
    finally:
        # Close ElevenLabs connection
        if not el_closed:
            try:
                await connection.close()
            except Exception:
                pass

        # Calculate duration
        duration = int(asyncio.get_event_loop().time() - start_time)

        # Write transcript
        (meeting_dir / "transcript.json").write_text(
            json.dumps(segments, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Finalize metadata
        metadata["status"] = "completed"
        metadata["duration"] = duration
        metadata["segment_count"] = len(segments)
        (meeting_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Close browser WS if still open
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import threading

    import uvicorn

    load_dotenv()
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        print("\n  ERROR: ELEVENLABS_API_KEY not set.")
        print("  Create a .env file with: ELEVENLABS_API_KEY=your_key_here\n")
        raise SystemExit(1)

    app.state.api_key = api_key
    port = int(os.getenv("PORT", "8765"))

    threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    uvicorn.run(app, host="0.0.0.0", port=port)
