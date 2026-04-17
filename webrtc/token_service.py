"""Flask token service for Azure OpenAI Realtime API (WebRTC, GA protocol).

Mints an ephemeral client secret from
  POST https://<resource>.openai.azure.com/openai/v1/realtime/client_secrets
using DefaultAzureCredential (no API keys), then the browser uses that secret
to negotiate a WebRTC session against /openai/v1/realtime/calls.

Endpoints:
  GET  /            -> serves static/index.html
  GET  /token       -> { "token": "<ephemeral>" }
  POST /connect     -> proxies the SDP offer and returns the SDP answer (text/plain).
                       Also opens a background WebSocket observer on the call.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import requests
import websockets
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()  # loads .env from CWD
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))  # also try parent

AZURE_RESOURCE = os.getenv("AZURE_RESOURCE")
REALTIME_DEPLOYMENT = os.getenv("REALTIME_DEPLOYMENT", "gpt-realtime-mini")
REALTIME_VOICE = os.getenv("REALTIME_VOICE", "marin")
REALTIME_INSTRUCTIONS = os.getenv(
    "REALTIME_INSTRUCTIONS", "You are a helpful, concise voice assistant."
)
PORT = int(os.getenv("PORT", "5000"))

SESSION_CONFIG = {
    "session": {
        "type": "realtime",
        "model": REALTIME_DEPLOYMENT,
        "instructions": REALTIME_INSTRUCTIONS,
        "audio": {"output": {"voice": REALTIME_VOICE}},
    }
}

app = Flask(__name__, static_folder="static", static_url_path="")

_credential = DefaultAzureCredential()
_cached_token: str | None = None
_token_expiry: float = 0.0
_token_lock = threading.Lock()


def get_bearer_token(scope: str = "https://ai.azure.com/.default") -> str:
    """Return a cached AAD bearer token, refreshing 5 min before expiry."""
    global _cached_token, _token_expiry
    now = time.time()
    with _token_lock:
        if _cached_token and now < (_token_expiry - 300):
            return _cached_token
        token = _credential.get_token(scope)
        _cached_token = token.token
        _token_expiry = token.expires_on
        return _cached_token


def mint_ephemeral_token() -> str:
    url = f"https://{AZURE_RESOURCE}.openai.azure.com/openai/v1/realtime/client_secrets"
    headers = {
        "Authorization": f"Bearer {get_bearer_token()}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=SESSION_CONFIG, timeout=30)
    if resp.status_code != 200:
        print(f"[client_secrets] {resp.status_code} {resp.reason}: {resp.text}")
    resp.raise_for_status()
    value = resp.json().get("value", "")
    if not value:
        raise RuntimeError(f"No ephemeral token in response: {resp.json()}")
    return value


def negotiate_sdp(ephemeral: str, sdp_offer: str) -> tuple[str, str]:
    url = f"https://{AZURE_RESOURCE}.openai.azure.com/openai/v1/realtime/calls"
    headers = {
        "Authorization": f"Bearer {ephemeral}",
        "Content-Type": "application/sdp",
    }
    resp = requests.post(url, data=sdp_offer, headers=headers, timeout=30)
    if resp.status_code != 201:
        raise RuntimeError(f"SDP negotiation failed: {resp.status_code} - {resp.text}")
    return resp.text, resp.headers.get("Location", "")


async def _observe_websocket(location: str, bearer: str) -> None:
    call_id = location.rstrip("/").split("/")[-1]
    ws_url = (
        f"wss://{AZURE_RESOURCE}.openai.azure.com/openai/v1/realtime?call_id={call_id}"
    )
    print(f"[ws] connecting {ws_url}")
    try:
        async with websockets.connect(
            ws_url, additional_headers={"Authorization": f"Bearer {bearer}"}
        ) as ws:
            print("[ws] connected")
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    print(f"[ws] {data.get('type', 'unknown')}")
                except json.JSONDecodeError:
                    print(f"[ws] non-json: {msg[:100]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ws] error: {exc}")


def spawn_ws_observer(location: str) -> None:
    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_observe_websocket(location, get_bearer_token()))
        finally:
            loop.close()

    threading.Thread(target=runner, daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/config")
def config():
    return jsonify({"azureResource": AZURE_RESOURCE})


@app.route("/token", methods=["GET"])
def token():
    try:
        return jsonify({"token": mint_ephemeral_token()})
    except Exception as exc:  # noqa: BLE001
        print(f"[/token] error: {exc}")
        return jsonify({"error": str(exc)}), 500


@app.route("/connect", methods=["POST"])
def connect():
    sdp_offer = request.form.get("sdp") or request.get_data(as_text=True)
    if not sdp_offer:
        return jsonify({"error": "Missing SDP offer"}), 400
    try:
        ephemeral = mint_ephemeral_token()
        sdp_answer, location = negotiate_sdp(ephemeral, sdp_offer)
        if location:
            spawn_ws_observer(location)
        return sdp_answer, 201, {"Content-Type": "application/sdp"}
    except Exception as exc:  # noqa: BLE001
        print(f"[/connect] error: {exc}")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    if not AZURE_RESOURCE:
        raise SystemExit("AZURE_RESOURCE env var is required")
    print(f"Starting token service for resource '{AZURE_RESOURCE}' on :{PORT}")
    print(f"Realtime deployment: {REALTIME_DEPLOYMENT}  voice: {REALTIME_VOICE}")
    app.run(host="0.0.0.0", port=PORT, debug=True)
