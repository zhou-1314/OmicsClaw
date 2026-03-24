# Memory System

OmicsClaw features a built-in memory system that maintains context across different conversational sessions, eliminating the need to repeatedly provide the same data and background information.

## How It Works

OmicsClaw uses a **Graph Database Engine** (backed by SQLite/PostgreSQL) to store and connect different entities:

- **`session://`**: Conversation history, active context, and pipeline results.
- **`dataset://`**: Metadata about files (paths, dimensions, formats, preprocessing states).
- **`preference://`**: User-specific habits (e.g., preferred clustering methods).
- **`insight://`**: Biological knowledge deduced during analysis (e.g., "Cluster 3 = T-cells").

These memory nodes form a tree connected via edges (e.g., `ROOT` → `session://user1` → `session://user1/dataset_abc123`). This allows the agent to traverse relationships, like finding all datasets used in a specific session or tracing the lineage of a clustering result.

> **Privacy Note:** The memory system strictly stores *metadata* and *analysis parameters*. It **does not** store raw gene expression matrices or absolute system paths.

## What It Solves

| Stateless Tool (Without Memory) | OmicsClaw (With Memory) |
| :--- | :--- |
| Re-upload data for every session | **Zero re-uploads** (remembers file metadata) |
| Re-explain context repeatedly | **Automatic context restoration** |
| No analysis lineage tracking | **Tracks lineage** (preprocessing → clustering → DE) |
| Loses user preferences | **Learns habits** (e.g., auto-applies `leiden`) |
| Cannot resume workflows | **Resumes interrupted work** seamlessly |

## Managing Memory via Dashboard

You can visually inspect, search, and manage stored memories using the built-in React dashboard.

### 1. Start the Backend API
Start the REST API server via the CLI (runs on port `8766` by default):

```bash
oc memory-server
# or: make memory-server
```

### 2. Start the Frontend Dashboard
Launch the web interface (runs on port `3000` by default):

```bash
cd frontend
npm install   # First time only
npm run dev
```

Open `http://localhost:3000` in your browser. From here, you can:
- **Search:** Full-text and semantic search across all memories.
- **Browse:** Navigate the graph tree to view sessions, datasets, and insights.
- **Manage:** Delete outdated context to keep the agent's memory clean.

### 3. Remote Access (SSH Port Forwarding)

If OmicsClaw is deployed on a remote server, use SSH port forwarding to access the dashboard locally:

```bash
ssh -N -L 3000:localhost:3000 -L 8766:localhost:8766 <user>@<remote-ip>
```

Then visit `http://localhost:3000` on your local machine.

*(Tip: For convenience, you can add `LocalForward` rules to your `~/.ssh/config`).*

## Developer Guide

For developers building new agents or workflows, interacting with the memory graph is done via the `MemoryClient`:

```python
import asyncio
from omicsclaw.memory import MemoryClient, SessionContext

async def main():
    # 1. Initialize client
    client = MemoryClient("sqlite+aiosqlite:///bot/data/memory.db")
    await client.boot()
    
    # 2. Define memory context
    context = SessionContext(domain="session", path="user123/agent_pipeline")

    # 3. Store a memory
    await client.remember(
        context=context,
        name="pipeline_result",
        content={"status": "success", "file": "output.h5ad"},
        metadata={"skill": "spatial-preprocessing"}
    )

    # 4. Recall or search memories
    result = await client.recall(context, "pipeline_result")
    search_results = await client.search(context, query="pipeline_result")

asyncio.run(main())
```

> **Note:** For legacy chatbots (Telegram/Feishu), the `CompatMemoryStore` transparently maps old Pydantic memory interfaces to graph URIs.
