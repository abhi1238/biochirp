"""CTD data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only CTD's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.ctd import return_ctd_result, get_ctd_db, _CTD_CAPABILITIES, _CTD_LIMITATIONS

app = build_app(
    db_short="ctd",
    return_result_fn=return_ctd_result,
    get_db_fn=get_ctd_db,
    display_name="CTD",
)

app.include_router(build_chat_router(ChatSpec(
    db="ctd",
    display_name="CTD",
    long_name="Comparative Toxicogenomics Database",
    return_result_fn=return_ctd_result,
    capabilities=_CTD_CAPABILITIES,
    limitations=_CTD_LIMITATIONS,
)))
