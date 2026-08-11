"""TTD data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only TTD's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router, register_execute_endpoint
from app.ttd import (
    return_ttd_result, get_ttd_db, _TTD_CAPABILITIES, _TTD_LIMITATIONS,
    SUMMARIZER_MODEL_NAME, prompt_md,
)

app = build_app(
    db_short="ttd",
    return_result_fn=return_ttd_result,
    get_db_fn=get_ttd_db,
    display_name="TTD",
)

app.include_router(build_chat_router(ChatSpec(
    db="ttd",
    display_name="TTD",
    long_name="Therapeutic Target Database",
    return_result_fn=return_ttd_result,
    capabilities=_TTD_CAPABILITIES,
    limitations=_TTD_LIMITATIONS,
)))

register_execute_endpoint(
    app,
    db="ttd",
    display_name="TTD",
    get_db=get_ttd_db,
    prompt_md=prompt_md,
    summarizer_model=SUMMARIZER_MODEL_NAME,
)
