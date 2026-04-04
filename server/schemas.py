from pydantic import BaseModel
from typing import Optional


class MemoryEntry(BaseModel):
    content: str
    type: str  # preference | fact | decision | goal | note
    tags: list[str] = []
    importance: float = 0.5  # 0.0 to 1.0
    source: str = "user_stated"  # user_stated | inferred | auto
    expires_at: Optional[str] = None  # ISO8601 or null


class SkillEntry(BaseModel):
    name: str
    description: str
    trigger_tags: list[str] = []
    instructions: str
    examples: list[str] = []
    version: int = 1
