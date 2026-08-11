"""Reactome data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only Reactome's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.reactome import return_reactome_result, get_reactome_db, _REACTOME_CAPABILITIES, _REACTOME_LIMITATIONS

app = build_app(
    db_short="reactome",
    return_result_fn=return_reactome_result,
    get_db_fn=get_reactome_db,
    display_name="Reactome",
)

app.include_router(build_chat_router(ChatSpec(
    db="reactome",
    display_name="Reactome",
    long_name="Reactome",
    return_result_fn=return_reactome_result,
    capabilities=_REACTOME_CAPABILITIES,
    limitations=_REACTOME_LIMITATIONS,
)))
