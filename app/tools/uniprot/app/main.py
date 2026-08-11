"""UniProt data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only UniProt's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.uniprot import return_uniprot_result, get_uniprot_db, _UNIPROT_CAPABILITIES, _UNIPROT_LIMITATIONS

app = build_app(
    db_short="uniprot",
    return_result_fn=return_uniprot_result,
    get_db_fn=get_uniprot_db,
    display_name="UniProt",
)

app.include_router(build_chat_router(ChatSpec(
    db="uniprot",
    display_name="UniProt",
    long_name="UniProt",
    return_result_fn=return_uniprot_result,
    capabilities=_UNIPROT_CAPABILITIES,
    limitations=_UNIPROT_LIMITATIONS,
)))
