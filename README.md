# Context Intelligence Layer

A model-agnostic middleware that gives LLMs persistent memory and reusable skills via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

## Why this exists

Every time you start a new conversation with an LLM, it forgets everything — your preferences, past decisions, project context, and workflows you've already explained. You end up repeating yourself across sessions.

This project solves that. It gives any MCP-compatible model a **long-term memory** and a **skill library** backed by a vector database. Memories are stored semantically, so the model retrieves them by meaning — not exact keywords. Skills let you save multi-step procedures once and have the model follow them automatically in future sessions.

The key design choice: **model-agnostic**. This isn't locked to Claude or GPT. Any client that speaks MCP (Claude Desktop, Claude Code, Codex, LiteLLM, or anything built tomorrow) can plug in and instantly get persistent context. Switch models, keep your memory.

### What it can do

- Remember who you are, what you're working on, and how you like things done
- Recall decisions from weeks-old conversations without you repeating them
- Store deployment checklists, debugging workflows, or review processes as reusable skills
- Work across multiple AI clients simultaneously — same memory, different models

---

## Features

- **Persistent memory** — store facts, preferences, decisions, and goals across conversations
- **Semantic search** — retrieve memories by meaning, not just keywords
- **Reusable skills** — save step-by-step instructions that any LLM can find and follow
- **Domain-scoped storage** — memories organized into `identity`, `projects`, `code`, `general`
- **Bearer token auth** — API key protection on every tool call
- **Model-agnostic** — works with any MCP client (Claude Desktop, Claude Code, Codex, etc.)

---

## Architecture

```
MCP Client (Claude / Codex / etc.)
        │
        │  MCP (Streamable HTTP)
        ▼
 context-mcp server  ←─── FastMCP 3.x + Python
        │
        │  Qdrant client
        ▼
   Qdrant (vector DB)
```

---

## Project Structure

```
context-intelligence/
│
├── server/                        # ── Core Server ──
│   ├── main.py                    # MCP server entry point, tool definitions, auth setup
│   ├── qdrant_store.py            # Qdrant CRUD — store, search, delete operations
│   ├── schemas.py                 # Pydantic models (MemoryEntry, SkillEntry)
│   └── embeddings.py              # FastEmbed wrapper (all-MiniLM-L6-v2, 384 dims)
│
├── setup/                         # ── Setup & Config ──
│   └── init_collections.py        # One-time script to create Qdrant collections
│
├── docs/                          # ── Documentation ──
│   └── SYSTEM_PROMPT.md           # Drop-in system prompt for LLM clients
│
├── Dockerfile                     # Container build for the MCP server
├── requirements.txt               # Python dependencies
├── README.md                      # You are here
├── LICENSE                        # MIT
└── .gitignore
```

---

## MCP Tools

| Tool | Description |
|------|-------------|
| `store_memory_tool` | Store a memory in a domain collection |
| `search_memory_tool` | Semantic search across memories |
| `delete_memory_tool` | Delete a memory by ID |
| `store_skill_tool` | Save a reusable skill with instructions |
| `find_skill_tool` | Find relevant skills by intent |
| `list_skills_tool` | List all stored skills |

---

## Quick Start

### Prerequisites

- Docker + Docker Compose

### 1. Clone and build the image

```bash
git clone https://github.com/myselfvivek17/context-intelligence.git
cd context-intelligence
docker build -t context-mcp:latest .
```

### 2. Create `docker-compose.yml`

```yaml
services:
  qdrant:
    image: qdrant/qdrant
    network_mode: host
    volumes:
      - /data/qdrant:/qdrant/storage
    environment:
      - QDRANT__SERVICE__API_KEY=your-qdrant-key
    restart: unless-stopped

  context-mcp:
    image: context-mcp:latest
    network_mode: host
    environment:
      - QDRANT_URL=http://localhost:6333
      - QDRANT_API_KEY=your-qdrant-key
      - FASTMCP_HOST=0.0.0.0
      - FASTMCP_PORT=8083
      - MCP_API_KEY=your-mcp-api-key
      - MAX_SEARCH_LIMIT=50
    restart: unless-stopped
```
**Note:** Replace `your-qdrant-key` and `your-mcp-api-key` with your own random strings — these are secrets you create, not values you get from anywhere. Use a password generator or something like `openssl rand -base64 24`.

### 3. Start the stack

```bash
docker compose up -d
```

### 4. Initialize Qdrant collections (run once)

Wait a few seconds for Qdrant to start, then:

**With Docker:**
```bash
docker run --rm --network host \
  -e QDRANT_URL=http://localhost:6333 \
  -e QDRANT_API_KEY=your-qdrant-key \
  context-mcp:latest \
  python setup/init_collections.py
```

**With Python (if installed locally):**
```bash
pip install qdrant-client
QDRANT_URL=http://your-server:6333 QDRANT_API_KEY=your-qdrant-key python init_collections.py
```

This creates the 5 required Qdrant collections:

| Collection | Purpose |
|------------|---------|
| `memory_identity` | User preferences, personal facts, who the user is |
| `memory_projects` | Ongoing work, goals, decisions, project context |
| `memory_code` | Languages, frameworks, coding patterns, conventions |
| `memory_general` | Everything else that doesn't fit above |
| `skills` | Reusable step-by-step instructions for the LLM to follow |

---

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant server URL |
| `QDRANT_API_KEY` | _(none)_ | Qdrant API key |
| `MCP_API_KEY` | _(none)_ | Bearer token for MCP auth |
| `FASTMCP_HOST` | `0.0.0.0` | Server bind host |
| `FASTMCP_PORT` | `8083` | Server port |
| `MAX_SEARCH_LIMIT` | `50` | Max results per search query |

---

## Connecting MCP Clients

### Claude Code (`.mcp.json`)

```json
{
  "mcpServers": {
    "context-intelligence": {
      "command": "npx",
      "args": [
        "--yes", "mcp-remote",
        "http://your-server:8083/mcp",
        "--allow-http",
        "--header", "Authorization: Bearer your-mcp-api-key"
      ]
    }
  }
}
```

### Claude Desktop — Windows (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "context-intelligence": {
      "command": "cmd",
      "args": [
        "/c", "npx", "--yes", "mcp-remote",
        "http://your-server:8083/mcp",
        "--allow-http",
        "--header", "Authorization: Bearer your-mcp-api-key"
      ]
    }
  }
}
```

### Codex (`~/.codex/config.toml`)

```toml
[[mcp_servers]]
name = "context-intelligence"
command = "npx"
args = ["--yes", "mcp-remote", "http://your-server:8083/mcp", "--allow-http", "--header", "Authorization: Bearer your-mcp-api-key"]
```

---

## System Prompt

To enable automatic memory behavior in your AI client, see [SYSTEM_PROMPT.md](docs/SYSTEM_PROMPT.md). It instructs the model to proactively search and store memories without being asked.

---

## Memory Domains

| Domain | Use for |
|--------|---------|
| `identity` | User preferences, personal facts |
| `projects` | Ongoing work, goals, decisions |
| `code` | Languages, patterns, tools, conventions |
| `general` | Everything else |

---

## Tech Stack

- [FastMCP](https://gofastmcp.com/) — MCP server framework
- [Qdrant](https://qdrant.tech/) — Vector database
- [FastEmbed](https://github.com/qdrant/fastembed) — Local embeddings (all-MiniLM-L6-v2, 384 dims)
- [mcp-remote](https://github.com/geelen/mcp-remote) — stdio-to-HTTP bridge for MCP clients

---

## License

MIT
