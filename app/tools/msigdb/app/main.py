"""MSigDB data-tool service — REST query endpoint + WebSocket chat (schema_kg)."""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.msigdb import return_msigdb_result, get_msigdb_db, _MSIGDB_CAPABILITIES, _MSIGDB_LIMITATIONS

app = build_app(
    db_short="msigdb",
    return_result_fn=return_msigdb_result,
    get_db_fn=get_msigdb_db,
    display_name="MSigDB",
)

app.include_router(build_chat_router(ChatSpec(
    db="msigdb",
    display_name="MSigDB",
    long_name="MSigDB",
    return_result_fn=return_msigdb_result,
    capabilities=_MSIGDB_CAPABILITIES,
    limitations=_MSIGDB_LIMITATIONS,
)))
