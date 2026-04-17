"""Inspect the Realtime API: mint ephemeral token, show session config,
open a WebSocket, send a text message, and print every event."""

import asyncio
import json
import os
import time

import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

# The deployed web app URL (fetches ephemeral token via managed identity)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rta-25966.azurewebsites.net")
RESOURCE = os.getenv("AZURE_RESOURCE")
DEPLOYMENT = os.getenv("REALTIME_DEPLOYMENT", "gpt-realtime-1.5")

# Sent over the WebSocket after connect to switch to text-only output
SESSION_UPDATE = {
    "type": "session.update",
    "session": {
        "modalities": ["text"],
    },
}


def banner(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def get_token_from_webapp() -> str:
    """Call the deployed web app's /token endpoint to get an ephemeral token."""
    url = f"{WEBAPP_URL}/token"
    print(f"GET {url}")
    resp = requests.get(url, timeout=30)
    print(f"Status: {resp.status_code}")
    resp.raise_for_status()
    data = resp.json()
    print(f"Response: {json.dumps(data, indent=2)}")
    return data["token"]


async def run_session(ephemeral_token: str) -> None:
    ws_url = (
        f"wss://{RESOURCE}.openai.azure.com"
        f"/openai/v1/realtime?model={DEPLOYMENT}"
    )

    banner("CONNECTING WEBSOCKET")
    print(f"URL: {ws_url}")
    print(f"Auth: ephemeral token from web app (ek_...)")

    async with websockets.connect(
        ws_url, additional_headers={"Authorization": f"Bearer {ephemeral_token}"}
    ) as ws:
        print("Connected!\n")

        # Read the session.created event
        msg = json.loads(await ws.recv())
        banner("SESSION.CREATED")
        print(json.dumps(msg, indent=2))

        # Update session to text-only so we can read output in terminal
        banner("SENDING SESSION.UPDATE (text-only mode)")
        print(json.dumps(SESSION_UPDATE, indent=2))
        await ws.send(json.dumps(SESSION_UPDATE))
        upd = json.loads(await ws.recv())
        print(f"\n<- {upd.get('type')}")

        # Send a text message
        banner("SENDING USER MESSAGE")
        user_msg = {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "What is WebRTC in 2 sentences?"}],
            },
        }
        print(json.dumps(user_msg, indent=2))
        await ws.send(json.dumps(user_msg))

        # Ask for a response
        await ws.send(json.dumps({"type": "response.create"}))
        print("\n→ response.create sent, waiting for events...\n")

        # Stream events until response.done
        banner("SERVER EVENTS")
        transcript_parts = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            evt = json.loads(raw)
            etype = evt.get("type", "unknown")

            # Print every event type
            if etype == "response.text.delta":
                delta = evt.get("delta", "")
                transcript_parts.append(delta)
                print(delta, end="", flush=True)
            elif etype == "response.text.done":
                print()  # newline after streaming text
                print(f"\n[{etype}]")
            elif etype == "response.done":
                print(f"\n[{etype}] — response complete")
                break
            else:
                # Show compact summary for other events
                compact = {k: v for k, v in evt.items() if k != "type"}
                detail = f" → {json.dumps(compact)}" if compact else ""
                print(f"[{etype}]{detail}")

        # Final transcript
        if transcript_parts:
            banner("FULL RESPONSE")
            print("".join(transcript_parts))


def main() -> None:
    if not RESOURCE:
        raise SystemExit("Set AZURE_RESOURCE in .env")

    banner(f"FETCHING TOKEN FROM WEB APP ({WEBAPP_URL})")
    token = get_token_from_webapp()
    print(f"\nEphemeral token: {token}")

    # Now open a WebSocket and have a conversation
    asyncio.run(run_session(token))


if __name__ == "__main__":
    main()
