import uuid
from datetime import datetime, timezone
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue, Range

from embeddings import embed
from schemas import MemoryEntry, SkillEntry

MEMORY_COLLECTIONS = ["memory_identity", "memory_projects", "memory_code", "memory_general", "memory_diary"]
SKILLS_COLLECTION = "skills"


def get_client(url: str, api_key: Optional[str] = None) -> QdrantClient:
    return QdrantClient(url=url, api_key=api_key)


def store_memory(client: QdrantClient, collection: str, entry: MemoryEntry) -> str:
    point_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "content": entry.content,
        "type": entry.type,
        "tags": entry.tags,
        "importance": entry.importance,
        "source": entry.source,
        "created_at": now,
        "updated_at": now,
        "expires_at": entry.expires_at,
    }
    vector = embed(entry.content)
    try:
        client.upsert(
            collection_name=collection,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to store memory: {e}") from e
    return point_id


def search_memory(
    client: QdrantClient,
    collection: str,
    query: str,
    limit: int = 5,
    type_filter: Optional[str] = None,
    tag_filter: Optional[str] = None,
    min_importance: Optional[float] = None,
) -> list[dict]:
    vector = embed(query)
    conditions = []

    if type_filter:
        conditions.append(FieldCondition(key="type", match=MatchValue(value=type_filter)))
    if tag_filter:
        conditions.append(FieldCondition(key="tags", match=MatchValue(value=tag_filter)))
    if min_importance is not None:
        conditions.append(FieldCondition(key="importance", range=Range(gte=min_importance)))

    search_filter = Filter(must=conditions) if conditions else None

    try:
        results = client.query_points(
            collection_name=collection,
            query=vector,
            limit=limit,
            query_filter=search_filter,
            with_payload=True,
        ).points
    except Exception as e:
        raise RuntimeError(f"Failed to search memories: {e}") from e
    return [{"id": str(r.id), "score": r.score, **r.payload} for r in results]


def list_memories(client: QdrantClient, collection: str, limit: int = 100, offset: Optional[str] = None) -> dict:
    # ponytail: cursor pagination via Qdrant's native scroll offset; no fake integer paging.
    try:
        results, next_offset = client.scroll(
            collection_name=collection,
            with_payload=True,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to list memories: {e}") from e
    return {
        "items": [{"id": str(r.id), **r.payload} for r in results],
        "next_offset": str(next_offset) if next_offset is not None else None,
    }


def store_skill(client: QdrantClient, entry: SkillEntry) -> str:
    point_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "name": entry.name,
        "description": entry.description,
        "domain": entry.domain,
        "trigger_tags": entry.trigger_tags,
        "instructions": entry.instructions,
        "examples": entry.examples,
        "version": entry.version,
        "created_at": now,
    }
    vector = embed(f"{entry.name} {entry.description} {' '.join(entry.trigger_tags)}")
    try:
        client.upsert(
            collection_name=SKILLS_COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to store skill: {e}") from e
    return point_id


def find_skill(client: QdrantClient, query: str, limit: int = 3) -> list[dict]:
    vector = embed(query)
    try:
        results = client.query_points(
            collection_name=SKILLS_COLLECTION,
            query=vector,
            limit=limit,
            with_payload=True,
        ).points
    except Exception as e:
        raise RuntimeError(f"Failed to find skills: {e}") from e
    return [{"id": str(r.id), "score": r.score, **r.payload} for r in results]


def list_skills(client: QdrantClient) -> list[dict]:
    try:
        results, _ = client.scroll(
            collection_name=SKILLS_COLLECTION,
            with_payload=True,
            limit=100,
        )
    except Exception as e:
        raise RuntimeError(f"Failed to list skills: {e}") from e
    return [{"id": str(r.id), **r.payload} for r in results]


def delete_memory(client: QdrantClient, collection: str, point_id: str) -> bool:
    try:
        client.delete(
            collection_name=collection,
            points_selector=[point_id],
        )
    except Exception as e:
        raise RuntimeError(f"Failed to delete memory: {e}") from e
    return True
