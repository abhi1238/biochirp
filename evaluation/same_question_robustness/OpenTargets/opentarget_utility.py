import json
import websockets
import requests
import pandas as pd
from pathlib import Path
from IPython.display import display
import io


BASE_WS_URL = "ws://localhost:8026/opentarget"
BASE_HTTP_URL = "http://localhost:8026"


import asyncio

async def retrieve_opentarget(query: str, timeout: float = 30.0) -> pd.DataFrame:
    csv_paths: list[str] = []

    async def _run():
        async with websockets.connect(BASE_WS_URL) as ws:
            init = json.loads(await ws.recv())
            connection_id = init.get("session_id")

            await ws.send(json.dumps({"user_input": query}))

            while True:
                raw = await ws.recv()
                data = json.loads(raw)

                event_type = data.get("type")
                tool_id = data.get("tool_id", "")
                tool_name = data.get("name")

                # Tool-level deltas
                if (
                    event_type == "delta"
                    and isinstance(tool_id, str)
                    and tool_id.endswith("_tool")
                ):
                    text = data.get("text", "")
                    for line in text.splitlines():
                        if line.strip():
                            print(f"[{tool_name}]: {line.strip()}")
                    print("-" * 40)

                # Collect CSV outputs
                if (
                    isinstance(event_type, str)
                    and event_type.endswith("_table")
                    and "csv_path" in data
                ):
                    csv_paths.append(data["csv_path"])

                if event_type == "final":
                    break

    try:
        await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"OpenTargets query exceeded {timeout} seconds")

    # -----------------------------
    # Download CSVs (with timeout)
    # -----------------------------
    final_df: pd.DataFrame | None = None

    for full_path in csv_paths:
        filename = Path(full_path).name

        response = requests.get(
            f"{BASE_HTTP_URL}/download",
            params={"path": filename},
            timeout=10,  # HTTP timeout (separate, important)
        )
        response.raise_for_status()

        df = pd.read_csv(io.BytesIO(response.content))
        final_df = df

        print(f"Entries retrieved: {df.shape[0]}")
        display(df.head(1))

    return final_df


# async def retrieve_opentarget(query: str, timeout: float = 30.0) -> pd.DataFrame:
#     """
#     Execute an OpenTargets query via WebSocket, stream tool-level explanations,
#     download resulting CSV outputs, and return the final DataFrame.

#     Behavior:
#     - Prints tool delta messages (only for *_tool executions)
#     - Collects CSV paths emitted by tools
#     - Downloads CSVs via HTTP
#     - Prints a preview of the retrieved data
#     - Returns the last retrieved DataFrame

#     Parameters
#     ----------
#     query : str
#         Natural language query to send to the OpenTargets orchestrator.

#     Returns
#     -------
#     pd.DataFrame
#         The final DataFrame retrieved from OpenTargets.
#         (If multiple CSVs are returned, the last one is returned.)
#     """

#     import os

#     csv_paths: list[str] = []

#     # --------------------------------------------------
#     # WebSocket interaction
#     # --------------------------------------------------
#     async with websockets.connect(BASE_WS_URL) as ws:
#         init = json.loads(await ws.recv())
#         connection_id = init.get("session_id")

#         # print(f" Connection ID: {connection_id}")
#         # print(f" Question: {query}")

#         await ws.send(json.dumps({"user_input": query}))

#         while True:
#             raw = await ws.recv()
#             data = json.loads(raw)

#             event_type = data.get("type")
#             tool_id = data.get("tool_id", "")
#             tool_name = data.get("name")

#             # ----------------------------------------------
#             # Stream tool-level explanations
#             # ----------------------------------------------
#             if (
#                 event_type == "delta"
#                 and isinstance(tool_id, str)
#                 and tool_id.endswith("_tool")
#             ):
#                 text = data.get("text", "")
#                 for line in text.splitlines():
#                     if line.strip():
#                         print(f"[{tool_name}]: {line.strip()}")
#                 print("-" * 40)

#             # ----------------------------------------------
#             # Collect CSV outputs
#             # ----------------------------------------------
#             if (
#                 isinstance(event_type, str)
#                 and event_type.endswith("_table")
#                 and "csv_path" in data
#             ):
#                 csv_paths.append(data["csv_path"])

#             # ----------------------------------------------
#             # Stop condition
#             # ----------------------------------------------
#             if event_type == "final":
#                 break

#     # --------------------------------------------------
#     # Download and load CSVs
#     # --------------------------------------------------
#     final_df: pd.DataFrame | None = None

#     for full_path in csv_paths:
#         filename = Path(full_path).name

#         response = requests.get(
#             f"{BASE_HTTP_URL}/download",
#             params={"path": filename},
#             timeout=60,
#         )
#         response.raise_for_status()

#         # with open(filename, "wb") as f:
#         #     f.write(response.content)

#         # df = pd.read_csv(filename)
#         df = pd.read_csv(io.BytesIO(response.content))
#         final_df = df

#         print(f"Entries retrieved: {df.shape[0]}")
#         # print(df.head(5), "\n")
#         display(df.head(1))

#         # os.remove(filename)

#     return final_df
