
from pydantic import BaseModel
from typing import List

class EmbedInput(BaseModel):
    texts: List[str]
