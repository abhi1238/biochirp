
# import asyncio
# import json
# import requests
# import pandas as pd
# from io import StringIO
# import websockets
# # ------------------------------------------------------------
# # CORE FUNCTION
# # ------------------------------------------------------------


# # ------------------------------------------------------------
# # CONFIG (MATCHES YOUR ORCHESTRATOR)
# # ------------------------------------------------------------
# # WS_URL = "ws://localhost:8010/chat"
# # DOWNLOAD_URL = "http://localhost:8010/download"


# # WS_URL = 
# WS_TTD_URL = 'wss://biochirp.iiitd.edu.in/ttd_chat/'
# WS_TTD_DOWNLOAD_URL = "http://localhost:8028/download"

# TABLE_EVENTS = {
#     "ttd_table": "ttd",
#     "ctd_table": "ctd",
#     "hcdt_table": "hcdt",
# }

# async def run_biochirp_query(query: str):
#     """
#     Returns
#     -------
#     final_answer : str
#     dfs          : dict[str, pd.DataFrame]   # keys: ttd / ctd / hcdt
#     csv_paths    : dict[str, str]
#     """

#     csv_paths = {}
#     final_answer = ""

#     async with websockets.connect(WS_TTD_URL) as ws:
#         # ---------------- Handshake ----------------
#         init = json.loads(await ws.recv())
#         connection_id = init["session_id"]
#         print(f" Connected to orchestrator | connection_id={connection_id}")

#         # ---------------- Send query ----------------
#         await ws.send(json.dumps({
#             "user_input": query,
#             "session_id": connection_id
#         }))

#         # ---------------- Listen ----------------
#         while True:
#             raw = await ws.recv()
#             msg = json.loads(raw)

#             msg_type = msg.get("type")

#             # ---- CSV EVENTS (Redis → WS) ----
#             if msg_type in TABLE_EVENTS:
#                 tool = TABLE_EVENTS[msg_type]
#                 csv_path = msg.get("csv_path")
#                 row_count = msg.get("row_count")

#                 if csv_path:
#                     csv_paths[tool] = csv_path
#                     print(f" {tool.upper()} CSV announced | rows={row_count}")

#             # ---- FINAL ANSWER ----
#             elif msg_type in {"final", "orchestrator_final"}:
#                 final_answer = msg.get("content", "")
#                 print(" Orchestrator finished")
#                 break

#             # ignore deltas, heartbeats, tool streaming, etc.

#     # --------------------------------------------------------
#     # DOWNLOAD CSVs
#     # --------------------------------------------------------
#     dfs = {}

#     for tool, path in csv_paths.items():
#         r = requests.get(WS_TTD_DOWNLOAD_URL, params={"path": path})
#         r.raise_for_status()
#         dfs[tool] = pd.read_csv(StringIO(r.text))
#         print(f"  Downloaded {tool.upper()} | shape={dfs[tool].shape}")

#     return final_answer, dfs, csv_paths


import asyncio
import json
import time
import requests
import pandas as pd
from io import StringIO
import websockets

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
WS_TTD_URL = "wss://biochirp.iiitd.edu.in/ttd_chat/"
WS_CTD_URL = "wss://biochirp.iiitd.edu.in/ctd_chat/"
WS_HCDT_URL = "wss://biochirp.iiitd.edu.in/hcdt_chat/"

# WS_TTD_DOWNLOAD_URL = "wss://biochirp.iiitd.edu.in/ttd_chat/download"


WS_TTD_DOWNLOAD_URL = "http://localhost:8028/download"
WS_CTD_DOWNLOAD_URL = "http://localhost:8031/download"
WS_HCDT_DOWNLOAD_URL = "http://localhost:8029/download"

TABLE_EVENTS = {
    "ttd_table": "ttd",
    "ctd_table": "ctd",
    "hcdt_table": "hcdt",
}


async def run_biochirp_query_ttd(query: str, ws_url:str = WS_TTD_URL, ws_url_csv:str = WS_TTD_DOWNLOAD_URL):
    """
    Returns
    -------
    final_answer : str
    dfs          : dict[str, pd.DataFrame]   # keys: ttd / ctd / hcdt
    csv_paths    : dict[str, str]
    """

    csv_paths: dict[str, str] = {}
    final_answer: str = ""

    async with websockets.connect(ws_url) as ws:
        # ---------------- Handshake ----------------
        init = json.loads(await ws.recv())
        connection_id = init.get("session_id")
        print(f"Connected to orchestrator | connection_id={connection_id}")

        # ---------------- Send query ----------------
        await ws.send(json.dumps({
            "user_input": query,
            "session_id": connection_id
        }))

        # ---------------- Listen ----------------
        completed = False

        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                msg_type = msg.get("type")

                # ---- CSV EVENTS (Redis → WS) ----
                if msg_type in TABLE_EVENTS:
                    tool = TABLE_EVENTS[msg_type]
                    csv_path = msg.get("csv_path")
                    row_count = msg.get("row_count")

                    if csv_path:
                        csv_paths[tool] = csv_path
                        print(f"{tool.upper()} CSV announced | rows={row_count}")

                # ---- FINAL ANSWER ----
                elif msg_type in {"final", "orchestrator_final"}:
                    final_answer = msg.get("content", "")
                    print("Orchestrator finished")
                    completed = True

                # ---- EXIT CONDITION ----
                if completed:
                    break

        except websockets.ConnectionClosed:
            # Defensive: server-side close
            print("WebSocket closed by server")

    # --------------------------------------------------------
    # DOWNLOAD CSVs
    # --------------------------------------------------------
    dfs: dict[str, pd.DataFrame] = {}

    for tool, path in csv_paths.items():
        r = requests.get(ws_url_csv, params={"path": path})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # print(f"Downloaded {tool.upper()} | shape={dfs[tool].shape}")

    return df



async def run_biochirp_query_ctd(query: str, ws_url:str = WS_CTD_URL, ws_url_csv:str = WS_CTD_DOWNLOAD_URL):
    """
    Returns
    -------
    final_answer : str
    dfs          : dict[str, pd.DataFrame]   # keys: ttd / ctd / hcdt
    csv_paths    : dict[str, str]
    """

    csv_paths: dict[str, str] = {}
    final_answer: str = ""

    async with websockets.connect(ws_url) as ws:
        # ---------------- Handshake ----------------
        init = json.loads(await ws.recv())
        connection_id = init.get("session_id")
        print(f"Connected to orchestrator | connection_id={connection_id}")

        # ---------------- Send query ----------------
        await ws.send(json.dumps({
            "user_input": query,
            "session_id": connection_id
        }))

        # ---------------- Listen ----------------
        completed = False

        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                msg_type = msg.get("type")

                # ---- CSV EVENTS (Redis → WS) ----
                if msg_type in TABLE_EVENTS:
                    tool = TABLE_EVENTS[msg_type]
                    csv_path = msg.get("csv_path")
                    row_count = msg.get("row_count")

                    if csv_path:
                        csv_paths[tool] = csv_path
                        print(f"{tool.upper()} CSV announced | rows={row_count}")

                # ---- FINAL ANSWER ----
                elif msg_type in {"final", "orchestrator_final"}:
                    final_answer = msg.get("content", "")
                    print("Orchestrator finished")
                    completed = True

                # ---- EXIT CONDITION ----
                if completed:
                    break

        except websockets.ConnectionClosed:
            # Defensive: server-side close
            print("WebSocket closed by server")

    # --------------------------------------------------------
    # DOWNLOAD CSVs
    # --------------------------------------------------------
    dfs: dict[str, pd.DataFrame] = {}

    for tool, path in csv_paths.items():
        r = requests.get(ws_url_csv, params={"path": path})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # print(f"Downloaded {tool.upper()} | shape={dfs[tool].shape}")

    return df





async def run_biochirp_query_hcdt(query: str, ws_url:str = WS_HCDT_URL, ws_url_csv:str = WS_HCDT_DOWNLOAD_URL):
    """
    Returns
    -------
    final_answer : str
    dfs          : dict[str, pd.DataFrame]   # keys: ttd / ctd / hcdt
    csv_paths    : dict[str, str]
    """

    csv_paths: dict[str, str] = {}
    final_answer: str = ""

    async with websockets.connect(ws_url) as ws:
        # ---------------- Handshake ----------------
        init = json.loads(await ws.recv())
        connection_id = init.get("session_id")
        print(f"Connected to orchestrator | connection_id={connection_id}")

        # ---------------- Send query ----------------
        await ws.send(json.dumps({
            "user_input": query,
            "session_id": connection_id
        }))

        # ---------------- Listen ----------------
        completed = False

        try:
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)

                msg_type = msg.get("type")

                # ---- CSV EVENTS (Redis → WS) ----
                if msg_type in TABLE_EVENTS:
                    tool = TABLE_EVENTS[msg_type]
                    csv_path = msg.get("csv_path")
                    row_count = msg.get("row_count")

                    if csv_path:
                        csv_paths[tool] = csv_path
                        print(f"{tool.upper()} CSV announced | rows={row_count}")

                # ---- FINAL ANSWER ----
                elif msg_type in {"final", "orchestrator_final"}:
                    final_answer = msg.get("content", "")
                    print("Orchestrator finished")
                    completed = True

                # ---- EXIT CONDITION ----
                if completed:
                    break

        except websockets.ConnectionClosed:
            # Defensive: server-side close
            print("WebSocket closed by server")

    # --------------------------------------------------------
    # DOWNLOAD CSVs
    # --------------------------------------------------------
    dfs: dict[str, pd.DataFrame] = {}

    for tool, path in csv_paths.items():
        r = requests.get(ws_url_csv, params={"path": path})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        # print(f"Downloaded {tool.upper()} | shape={dfs[tool].shape}")

    return df
