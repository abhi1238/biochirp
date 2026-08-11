#!/usr/bin/env python3
"""
check_chat_ws.py — verify a per-DB chat WebSocket surfaces every tool call.

This is the endpoint behind  db_chat.html?db=<db>  →  /<db>_chat/  (served by
app/per_db_tool/schema_kg_chat.py). It connects, sends ONE question, and prints
the full event stream:

  • the orchestrator routing decision
  • each tool call:  query_db (the <db> tool)  /  web_search
  • the query_db sub-steps:  Schema Mapper → Schema Planner → Entity Expander → DB Execute
  • the streamed synthesizer answer (+ row_count)

Exit code 0 = tool calls surfaced and an answer was received.

Usage
-----
  pip install websockets

  # local backend (hcdt container port 8018, default)
  python scripts/check_chat_ws.py
  python scripts/check_chat_ws.py --query "What are the targets of imatinib?"

  # out-of-scope → should show the web_search card
  python scripts/check_chat_ws.py --query "Who painted the Mona Lisa?"

  # another DB / port
  python scripts/check_chat_ws.py --db ttd --port 8014

  # LIVE site (through nginx) — proves the public URL works end to end
  python scripts/check_chat_ws.py --url wss://biochirp.iiitd.edu.in/hcdt_chat/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency. Run:  pip install websockets")


def ws_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    return f"ws://{args.host}:{args.port}/{args.db}_chat/"


async def run(args: argparse.Namespace) -> int:
    uri = ws_url(args)
    print(f"→ connecting : {uri}")
    print(f"→ query      : {args.query}")
    print("=" * 74)

    tool_calls: list[str] = []
    answer: list[str] = []
    row_count = None
    synth_started = False
    t0 = time.monotonic()

    try:
        async with websockets.connect(uri, open_timeout=15, max_size=None) as ws:
            await ws.send(json.dumps({"user_input": args.query}))
            while True:
                # Use a short read window once the answer is streaming so we exit
                # promptly when the server stops sending; a longer one otherwise.
                read_to = 12 if synth_started else args.timeout
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=read_to)
                except asyncio.TimeoutError:
                    break  # no more events — done (or stalled)
                except websockets.ConnectionClosed:
                    break

                try:
                    m = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                typ = m.get("type")
                name = m.get("name")

                if typ == "tool_called":
                    tool_calls.append(name)
                    if name == "synthesizer":
                        synth_started = True
                    print(f"  ▶ tool_called : {name}")
                elif typ == "tool_result":
                    rc = m.get("row_count")
                    print(f"  ✓ tool_result : {name}  ok={m.get('ok')}"
                          + (f"  rows={rc}" if rc is not None else ""))
                    if rc is not None:
                        row_count = rc
                elif typ == "delta":
                    if name == "orch_step":
                        print(f"      ↳ step: {str(m.get('text'))[:90]}")
                    elif m.get("tool_id") == "synthesizer":
                        synth_started = True
                        answer.append(m.get("text") or "")
                elif typ == "error" or m.get("error"):
                    print(f"  ✗ ERROR: {json.dumps(m)[:200]}")
                # user_ack / heartbeat / pong: ignore
    except Exception as e:  # noqa: BLE001
        print(f"\n✗ WS connection failed: {e!r}")
        print("  → check the backend is up and (for --url) that nginx routes "
              f"/{args.db}_chat/ with WebSocket Upgrade headers.")
        return 2

    dt = time.monotonic() - t0
    text = "".join(answer).strip()
    print("=" * 74)
    print(f"tool cards   : {tool_calls}")
    print(f"row_count    : {row_count}")
    print(f"latency      : {dt:.1f}s")
    print(f"answer (head): {text[:300]}")

    ok = bool(tool_calls) and ("synthesizer" in tool_calls or bool(text))
    print("\nRESULT:", "✅ tool calls surfaced + answer received"
          if ok else "❌ no tool calls / no answer — investigate backend or nginx route")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Check a per-DB chat WebSocket surfaces every tool call.")
    p.add_argument("--db", default="hcdt", help="DB slug (default: hcdt)")
    p.add_argument("--query", default="What are the targets of imatinib?",
                   help="question to send")
    p.add_argument("--url", default="",
                   help="full WS URL, e.g. wss://biochirp.iiitd.edu.in/hcdt_chat/ "
                        "(overrides --host/--port/--db)")
    p.add_argument("--host", default="localhost", help="backend host (default: localhost)")
    p.add_argument("--port", default="8018",
                   help="backend port (default 8018 = hcdt; ttd=8014, ctd=…)")
    p.add_argument("--timeout", type=float, default=120,
                   help="seconds to wait for the first events (default 120)")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
