"""Orphanet data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only Orphanet's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.orphanet import return_orphanet_result, get_orphanet_db, _ORPHANET_CAPABILITIES, _ORPHANET_LIMITATIONS

app = build_app(
    db_short="orphanet",
    return_result_fn=return_orphanet_result,
    get_db_fn=get_orphanet_db,
    display_name="Orphanet",
)

app.include_router(build_chat_router(ChatSpec(
    db="orphanet",
    display_name="Orphanet",
    long_name="Orphanet",
    return_result_fn=return_orphanet_result,
    capabilities=_ORPHANET_CAPABILITIES,
    limitations=_ORPHANET_LIMITATIONS,
)))
