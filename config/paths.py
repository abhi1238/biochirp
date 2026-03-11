

# app/config/paths.py

from pathlib import Path

current_dir = Path(__file__).parent.parent.parent.resolve()  # from app/config
resources_dir = current_dir / "resources"

DB_VALUE_PATH = resources_dir / "values/concept_values_by_db_and_field.pkl"