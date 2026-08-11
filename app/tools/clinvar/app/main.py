"""ClinVar data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only ClinVar's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.clinvar import return_clinvar_result, get_clinvar_db, _CLINVAR_CAPABILITIES, _CLINVAR_LIMITATIONS

app = build_app(
    db_short="clinvar",
    return_result_fn=return_clinvar_result,
    get_db_fn=get_clinvar_db,
    display_name="ClinVar",
)

app.include_router(build_chat_router(ChatSpec(
    db="clinvar",
    display_name="ClinVar",
    long_name="ClinVar",
    return_result_fn=return_clinvar_result,
    capabilities=_CLINVAR_CAPABILITIES,
    limitations=_CLINVAR_LIMITATIONS,
)))
