"""
Run this once to create all required Qdrant collections.
Usage: python init_collections.py
"""
import os

from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", None)

COLLECTIONS = [
    "memory_identity",
    "memory_projects",
    "memory_code",
    "memory_general",
    "skills",
]

VECTOR_SIZE = 384  # all-MiniLM-L6-v2


def init(client: QdrantClient):
    existing = {c.name for c in client.get_collections().collections}
    for name in COLLECTIONS:
        if name in existing:
            print(f"  [skip] {name} already exists")
        else:
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            print(f"  [ok]   {name} created")


if __name__ == "__main__":
    client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    print(f"Connecting to Qdrant at {QDRANT_URL}...")
    init(client)
    print("Done.")
