"""HPO data-tool service — REST query endpoint + WebSocket chat (schema_kg).

Uses the shared schema_kg pipeline (planner, worker, chat) from
`app.per_db_tool`. Only HPO's identity + capability blurb are injected here.
"""
from app.per_db_tool import build_app, ChatSpec, build_chat_router
from app.hpo import return_hpo_result, get_hpo_db, _HPO_CAPABILITIES, _HPO_LIMITATIONS

app = build_app(
    db_short="hpo",
    return_result_fn=return_hpo_result,
    get_db_fn=get_hpo_db,
    display_name="HPO",
)

app.include_router(build_chat_router(ChatSpec(
    db="hpo",
    display_name="HPO",
    long_name="Human Phenotype Ontology",
    return_result_fn=return_hpo_result,
    capabilities=_HPO_CAPABILITIES,
    limitations=_HPO_LIMITATIONS,
)))
