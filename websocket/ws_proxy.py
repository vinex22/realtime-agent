"""WebSocket proxy for Azure OpenAI Realtime API.

All audio flows through this server — nothing goes directly from browser to Azure.
Authenticates to Azure with DefaultAzureCredential (managed identity / az login).

Browser <--ws--> This Server <--ws--> Azure OpenAI Realtime
                 (your VNet)         (via Private Endpoint if configured)

Run:  uvicorn ws_proxy:app --port 5001
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import threading

import websockets
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()  # loads .env from CWD
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))  # also try parent

AZURE_RESOURCE = os.getenv("AZURE_RESOURCE")
DEPLOYMENT = os.getenv("REALTIME_DEPLOYMENT", "gpt-realtime-1.5")
VOICE = os.getenv("REALTIME_VOICE", "marin")
INSTRUCTIONS = os.getenv(
    "REALTIME_INSTRUCTIONS", "You are a helpful, concise voice assistant."
)
WS_PORT = int(os.getenv("WS_PORT", "5001"))

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- credential caching ---
_credential = DefaultAzureCredential()
_cached_token: str | None = None
_token_expiry: float = 0.0
_token_lock = threading.Lock()


def get_bearer_token(scope: str = "https://ai.azure.com/.default") -> str:
    global _cached_token, _token_expiry
    now = time.time()
    with _token_lock:
        if _cached_token and now < (_token_expiry - 300):
            return _cached_token
        token = _credential.get_token(scope)
        _cached_token = token.token
        _token_expiry = token.expires_on
        return _cached_token


@app.get("/")
async def index():
    return FileResponse("static/ws.html")


@app.websocket("/ws")
async def websocket_relay(client_ws: WebSocket):
    """Relay messages between the browser and Azure OpenAI Realtime API."""
    await client_ws.accept()

    azure_url = (
        f"wss://{AZURE_RESOURCE}.openai.azure.com"
        f"/openai/v1/realtime?model={DEPLOYMENT}"
    )
    bearer = get_bearer_token()

    print(f"[relay] connecting to Azure: {azure_url}")

    try:
        async with websockets.connect(
            azure_url,
            additional_headers={"Authorization": f"Bearer {bearer}"},
        ) as azure_ws:
            print("[relay] connected to Azure")

            # Wait for session.created from Azure
            session_msg = await azure_ws.recv()
            session_data = json.loads(session_msg)
            print(f"[relay] <- Azure: {session_data.get('type')}")
            await client_ws.send_text(session_msg)

            # Send session.update to configure voice, instructions, etc.
            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": DEPLOYMENT,
                    "instructions": INSTRUCTIONS,
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "transcription": {"model": "gpt-4o-mini-transcription"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 500,
                            },
                        },
                        "output": {
                            "format": {"type": "audio/pcm", "rate": 24000},
                            "voice": VOICE,
                        },
                    },
                },
            }
            await azure_ws.send(json.dumps(session_update))
            print("[relay] -> Azure: session.update")

            # Wait for session.updated confirmation
            upd_msg = await azure_ws.recv()
            upd_data = json.loads(upd_msg)
            print(f"[relay] <- Azure: {upd_data.get('type')}")
            await client_ws.send_text(upd_msg)

            # Bidirectional relay
            async def browser_to_azure():
                """Forward messages from browser to Azure."""
                try:
                    while True:
                        msg = await client_ws.receive_text()
                        data = json.loads(msg)
                        etype = data.get("type", "")
                        if etype != "input_audio_buffer.append":
                            print(f"[relay] browser -> Azure: {etype}")
                        await azure_ws.send(msg)
                except WebSocketDisconnect:
                    print("[relay] browser disconnected")
                except Exception as e:
                    print(f"[relay] browser->azure error: {e}")

            async def azure_to_browser():
                """Forward messages from Azure to browser."""
                try:
                    async for msg in azure_ws:
                        data = json.loads(msg)
                        etype = data.get("type", "")
                        if etype not in (
                            "response.output_audio.delta",
                            "response.output_audio_transcript.delta",
                        ):
                            print(f"[relay] Azure -> browser: {etype}")
                        await client_ws.send_text(msg)
                except Exception as e:
                    print(f"[relay] azure->browser error: {e}")

            # Run both directions concurrently
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(browser_to_azure()),
                    asyncio.create_task(azure_to_browser()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        print(f"[relay] error: {e}")
        try:
            await client_ws.close(code=1011, reason=str(e))
        except Exception:
            pass

    print("[relay] session ended")
