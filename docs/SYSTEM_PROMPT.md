# Using Context-Intelligence memory (for agents)

You have a persistent memory server. Use it without being asked.

## Read
- At the start of a session, call `profile_tool` once. It's a short, dated summary of what is currently true.
- Before answering anything that depends on the user's preferences, projects, setup or past decisions, call
  `search_memory_tool` with a natural-language query. Omit `domain` unless you know it. Results are only what
  is **currently** true; pass `include_history=true` or `as_of=<ISO time>` to look back.
- For "everything about X" or "how did X change", use `entity_tool` / `timeline_tool`.
- Before a multi-step task, call `find_skill_tool` and follow the first result if it fits.

## Write
Store when the user states a preference, makes a decision, sets a goal, corrects you, or shares a fact that
will matter later. Don't store chit-chat or things already in the repo.

**Changeable facts → pass a key.** If the fact could change later (a preference, current tool/stack/version,
a config value, where something runs, who owns something), pass `subject` + `relation` + `object`:

```
store_memory_tool(content="Vivek's primary database is Postgres", domain="identity", type="preference",
                  subject="Vivek", relation="primary database", object="Postgres", importance=0.9)
```

Storing the same subject + relation again retires the old value automatically. Call
`list_keys_tool(subject)` first and **reuse an existing relation name** for the same attribute.

**One-off notes → no key.** Events, observations, diary-like notes: leave subject/relation empty.

If a store returns `needs_review` or `conflict`, tell the user in one line. They can approve in the review
queue (`review_queue_tool`, `resolve_review_tool`).

Domains: `identity` (who the user is, preferences) · `projects` (work, goals, decisions) · `code` (languages,
tools, conventions) · `general` · `diary`.
Importance: 0.8–1.0 strong preferences/decisions · 0.5 general facts · 0.3 minor notes.
