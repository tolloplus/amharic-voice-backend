import base64
import json
import os
import time
from typing import Optional

import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, Response

app = FastAPI()

# -----------------------------
# Basic health routes
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "OK"


# -----------------------------
# Twilio Incoming Call -> TwiML
# -----------------------------
@app.post("/twilio/incoming-call")
async def incoming_call(_: Request):
    # IMPORTANT: Must be wss:// and publicly reachable
    ws_url = os.getenv("TWILIO_WS_URL", "wss://amharic-voice-backend.onrender.com/ws/twilio")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" />
  </Connect>
</Response>
"""
    return Response(content=twiml, media_type="text/xml")


# -----------------------------
# Helpers: G.711 mu-law <-> PCM
# -----------------------------
# Twilio Media Streams audio is typically PCMU (G.711 mu-law), 8kHz, mono.
# We'll generate a beep in PCM, then encode to mu-law for sending back.

MU_LAW_MAX = 0x1FFF
BIAS = 33

def pcm16_to_mulaw(pcm: np.ndarray) -> bytes:
    """Convert 16-bit PCM numpy array to mu-law bytes."""
    # Ensure int16
    x = pcm.astype(np.int16)

    # Get sign and magnitude
    sign = (x >> 8) & 0x80
    x = np.abs(x).astype(np.int32)

    # Clamp
    x = np.minimum(x, MU_LAW_MAX)

    x = x + BIAS

    # Determine exponent
    exponent = np.zeros_like(x)
    mask = 0x1000
    for exp in range(7, 0, -1):
        exponent = np.where(x & mask, exp, exponent)
        mask >>= 1

    mantissa = (x >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def make_beep_mulaw(duration_ms: int = 300, freq_hz: int = 880, sample_rate: int = 8000) -> str:
    """Return base64 string of mu-law beep audio."""
    t = np.arange(int(sample_rate * duration_ms / 1000.0)) / sample_rate
    # Keep amplitude conservative for telephony
    pcm = (0.2 * np.sin(2 * np.pi * freq_hz * t) * 32767).astype(np.int16)
    ulaw_bytes = pcm16_to_mulaw(pcm)
    return base64.b64encode(ulaw_bytes).decode("ascii")


# -----------------------------
# Twilio Media Streams WebSocket
# -----------------------------
@app.websocket("/ws/twilio")
async def twilio_ws(ws: WebSocket):
    await ws.accept()

    stream_sid: Optional[str] = None
    last_beep_at = 0.0

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                # Twilio confirms WebSocket connection established
                # (No streamSid yet)
                continue

            if event == "start":
                # Start includes streamSid and call metadata
                stream_sid = msg["start"]["streamSid"]

                # Send an initial beep so you can confirm audio playback works
                beep_b64 = make_beep_mulaw()
                await ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {
                        "payload": beep_b64
                    }
                }))
                last_beep_at = time.time()
                continue

            if event == "media":
                # Inbound audio from caller (base64 mu-law frames)
                # For now we don't STT yet; we just keep connection alive.
                # Optional: every ~6 seconds, send another short beep as a heartbeat.
                if stream_sid and (time.time() - last_beep_at) > 6.0:
                    beep_b64 = make_beep_mulaw(duration_ms=200, freq_hz=660)
                    await ws.send_text(json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {
                            "payload": beep_b64
                        }
                    }))
                    last_beep_at = time.time()
                continue

            if event == "stop":
                # Call ended / stream stopped
                break

            # Other events exist (dtmf, mark, etc.) depending on setup. :contentReference[oaicite:3]{index=3}

    except WebSocketDisconnect:
        return
    except Exception:
        # Keep it simple for now; you can add logging
        return
