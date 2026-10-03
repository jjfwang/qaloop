"""Deterministic WebSocket fixture for the Stone Age game client.

Speaks just enough of the wire protocol (apps/game-client/wire.mjs) to let
qaloop verify the live-session path without booting the real game server:

  client -> HELLO {kind, supportedProtocolVersions, ...}
  server -> WELCOME {kind: "WELCOME", disposition: "new",
                    resumeAnchor: {sessionId, signature}}

Anything else the client sends is logged and ignored; the socket stays open
so the client believes the session is live.

Usage:
    python3 fixtures/game_socket_fixture.py [--port 3123] [--snapshot camp-empty]

--snapshot camp-empty: after WELCOME, send one server-authored SNAPSHOT with a
player entity at camp and a pet entity carrying an empty campOccupancy and
empty cacheBands (empty food cache -> 用完了). Flows that render post-snapshot
surfaces (e.g. flows/game-camp-editor.yaml) boot the fixture with this flag;
the default (no flag) keeps the WELCOME-only behavior the other flows rely on.

--snapshot camp-play-aid: after WELCOME, send one server-authored SNAPSHOT
with the fetch stone placed in the play-training slot (campOccupancy ->
{"camp.play-training.slot-a": "play-aid:fetch-stone"}). Flows that render
the placed play aid (e.g. flows/game-play-aid-editor.yaml) boot the fixture
with this flag; the note line stays idle (no ACK cue ever lands from this
fixture), documenting the calm placement state.

--snapshot camp-comfort-item: after WELCOME, send one server-authored
SNAPSHOT with the shade mat placed in the rest slot (campOccupancy ->
{"camp.rest.slot-a": "comfort-item:shade-mat"}). Flows that render the
placed comfort item (e.g. flows/game-comfort-item-editor.yaml) boot the
fixture with this flag; the note line stays idle (no ACK cue ever lands
from this fixture), documenting the calm placement state.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    sys.exit("need the 'websockets' package: pip install websockets")


CAMP_EMPTY_SNAPSHOT = {
    "kind": "SNAPSHOT",
    "tick": 1,
    "projection": {
        "sessionId": "qa-fixture-session",
        "phase": "live",
        "entities": [
            {"kind": "player", "entityId": "qa-player", "placeId": "camp"},
            {
                "kind": "pet",
                "entityId": "qa-pet",
                "speciesId": "red-tyrant",
                "placeId": "camp",
                "campOccupancy": {},
                "cacheBands": {},
            },
        ],
    },
}

CAMP_COMFORT_ITEM_SNAPSHOT = {
    "kind": "SNAPSHOT",
    "tick": 1,
    "projection": {
        "sessionId": "qa-fixture-session",
        "phase": "live",
        "entities": [
            {"kind": "player", "entityId": "qa-player", "placeId": "camp"},
            {
                "kind": "pet",
                "entityId": "qa-pet",
                "speciesId": "red-tyrant",
                "placeId": "camp",
                "campOccupancy": {"camp.rest.slot-a": "comfort-item:shade-mat"},
                "cacheBands": {},
            },
        ],
    },
}


CAMP_PLAY_AID_SNAPSHOT = {
    "kind": "SNAPSHOT",
    "tick": 1,
    "projection": {
        "sessionId": "qa-fixture-session",
        "phase": "live",
        "entities": [
            {"kind": "player", "entityId": "qa-player", "placeId": "camp"},
            {
                "kind": "pet",
                "entityId": "qa-pet",
                "speciesId": "red-tyrant",
                "placeId": "camp",
                "campOccupancy": {"camp.play-training.slot-a": "play-aid:fetch-stone"},
                "cacheBands": {},
            },
        ],
    },
}


async def handle(ws, snapshot: dict | None) -> None:
    peer = ws.remote_address
    print(f"[fixture] connection from {peer}", flush=True)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                print(f"[fixture] non-JSON frame ({len(raw)} bytes), ignored", flush=True)
                continue
            kind = msg.get("kind")
            print(f"[fixture] <- {kind}", flush=True)
            if kind == "HELLO":
                welcome = {
                    "kind": "WELCOME",
                    "disposition": "new",
                    "resumeAnchor": {"sessionId": "qa-fixture-session",
                                     "signature": "qa-fixture"},
                }
                await ws.send(json.dumps(welcome))
                print("[fixture] -> WELCOME (new)", flush=True)
                if snapshot is not None:
                    await asyncio.sleep(0.2)
                    await ws.send(json.dumps(snapshot))
                    print("[fixture] -> SNAPSHOT (tick 1)", flush=True)
    except websockets.ConnectionClosed:
        pass
    print(f"[fixture] closed {peer}", flush=True)


async def main(port: int, snapshot: dict | None) -> None:
    # host=None: all interfaces (localhost may resolve to ::1 or 127.0.0.1)
    async with websockets.serve(lambda ws: handle(ws, snapshot), None, port):
        print(f"[fixture] listening ws://127.0.0.1:{port}/socket", flush=True)
        await asyncio.Future()  # forever


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=3123)
    ap.add_argument("--snapshot", choices=["camp-empty", "camp-play-aid", "camp-comfort-item"], default=None,
                    help="snapshot to emit after WELCOME (default: none)")
    args = ap.parse_args()
    snapshot = {"camp-empty": CAMP_EMPTY_SNAPSHOT, "camp-play-aid": CAMP_PLAY_AID_SNAPSHOT, "camp-comfort-item": CAMP_COMFORT_ITEM_SNAPSHOT}.get(args.snapshot)
    asyncio.run(main(args.port, snapshot))
