import os
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.auth.providers.debug import DebugTokenVerifier

from schemas import MemoryEntry, SkillEntry
from qdrant_store import (
    get_client,
    store_memory,
    search_memory,
    store_skill,
    find_skill,
    list_skills,
    delete_memory,
    MEMORY_COLLECTIONS,
)

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", None)

MCP_API_KEY = os.environ.get("MCP_API_KEY")
MAX_SEARCH_LIMIT = int(os.environ.get("MAX_SEARCH_LIMIT", 50))

auth = None
if MCP_API_KEY:
    auth = DebugTokenVerifier(
        validate=lambda token: token == MCP_API_KEY,
        client_id="mcp-client",
        scopes=["full"],
    )

mcp = FastMCP("context-intelligence", auth=auth)
client = get_client(QDRANT_URL, QDRANT_API_KEY)



@mcp.tool()
def store_memory_tool(
    content: str,
    domain: str,
    type: str,
    tags: str = "",
    importance: float = 0.5,
    source: str = "user_stated",
    expires_at: Optional[str] = None,
) -> str:
    """
    Store a memory entry in the specified domain collection.
    domain must be one of: identity, projects, code, general.
    type should be one of: preference, fact, decision, goal, note.
    importance is 0.0 to 1.0 (default 0.5).
    tags: comma-separated keywords (e.g. "python,backend,language").
    """
    collection = f"memory_{domain}"
    if collection not in MEMORY_COLLECTIONS:
        return f"Invalid domain '{domain}'. Choose from: identity, projects, code, general."
    importance = max(0.0, min(1.0, importance))
    entry = MemoryEntry(
        content=content,
        type=type,
        tags=[t.strip() for t in tags.split(",") if t.strip()],
        importance=importance,
        source=source,
        expires_at=expires_at,
    )
    point_id = store_memory(client, collection, entry)
    return f"Stored in {collection} with id {point_id}"


@mcp.tool()
def search_memory_tool(
    query: str,
    domain: str,
    limit: int = 5,
    type_filter: Optional[str] = None,
    tag_filter: Optional[str] = None,
    min_importance: Optional[float] = None,
) -> list[dict]:
    """
    Search memories in the specified domain using semantic similarity.
    domain must be one of: identity, projects, code, general.
    Optionally filter by type, a single tag, or minimum importance score.
    Returns top matching memories with their scores.
    """
    collection = f"memory_{domain}"
    if collection not in MEMORY_COLLECTIONS:
        return [{"error": f"Invalid domain '{domain}'. Choose from: identity, projects, code, general."}]
    limit = min(limit, MAX_SEARCH_LIMIT)
    return search_memory(client, collection, query, limit, type_filter, tag_filter, min_importance)


@mcp.tool()
def delete_memory_tool(domain: str, point_id: str) -> str:
    """
    Delete a specific memory entry by its ID.
    domain must be one of: identity, projects, code, general.
    """
    collection = f"memory_{domain}"
    if collection not in MEMORY_COLLECTIONS:
        return f"Invalid domain '{domain}'."
    delete_memory(client, collection, point_id)
    return f"Deleted {point_id} from {collection}"


@mcp.tool()
def store_skill_tool(
    name: str,
    description: str,
    instructions: str,
    trigger_tags: str = "",
    examples: str = "",
) -> str:
    """
    Store a reusable skill with its instruction set.
    trigger_tags: comma-separated keywords/intents that should activate this skill (e.g. "docker,deploy,portainer").
    examples: comma-separated example prompts that would trigger this skill.
    instructions should be step-by-step markdown guidance.
    """
    entry = SkillEntry(
        name=name,
        description=description,
        trigger_tags=[t.strip() for t in trigger_tags.split(",") if t.strip()],
        instructions=instructions,
        examples=[e.strip() for e in examples.split(",") if e.strip()],
    )
    point_id = store_skill(client, entry)
    return f"Skill '{name}' stored with id {point_id}"


@mcp.tool()
def find_skill_tool(query: str, limit: int = 3) -> list[dict]:
    """
    Find relevant skills based on a query or user intent.
    Returns the top matching skills with their instructions.
    """
    limit = min(limit, MAX_SEARCH_LIMIT)
    return find_skill(client, query, limit)


@mcp.tool()
def list_skills_tool() -> list[dict]:
    """
    List all available skills with their names, descriptions, and trigger tags.
    """
    return list_skills(client)


if __name__ == "__main__":
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "sse"
    mcp.run(transport=transport)
