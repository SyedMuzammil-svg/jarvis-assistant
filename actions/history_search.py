"""
Action: recall_conversation
Allows JARVIS to search past conversation transcripts and remember exact previous exchanges.
"""

from memory.conversation_history import get_history

def search_conversation_history(parameters: dict, player=None, session_memory=None) -> str:
    """Search previous conversation history by query keyword or phrase."""
    query = parameters.get("query", "").strip()
    limit = int(parameters.get("limit", 5))

    if not query:
        # If no query, return the most recent turns
        h = get_history()
        recent = h.get_recent_turns(n=limit * 2)
        if not recent:
            return "No previous conversation history found, Sir."
        lines = []
        for t in recent:
            speaker = "Sir" if t["role"] == "user" else "JARVIS"
            lines.append(f"{speaker}: {t['content']}")
        return "Recent conversation excerpt:\n" + "\n".join(lines)

    h = get_history()
    matches = h.search(query, limit=limit)
    if not matches:
        return f"I found no previous conversations mentioning '{query}', Sir."

    lines = []
    for m in matches:
        speaker = "Sir" if m["role"] == "user" else "JARVIS"
        ts = m["timestamp"].split("T")[0] if "T" in m["timestamp"] else m["timestamp"]
        lines.append(f"[{ts}] {speaker}: {m['content']}")

    result = f"Here is what I found in our past conversations regarding '{query}':\n" + "\n".join(lines)
    if player:
        try:
            player.write_log(f"History search: '{query}' -> {len(matches)} matches")
        except Exception:
            pass
    return result

TOOL = {
    "name": "recall_conversation",
    "description": (
        "Search through past conversation history and transcripts. "
        "Use this when the user asks 'what did we talk about earlier?', "
        "'do you remember when I said...', or to recall previous discussions and facts from earlier sessions."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Keywords, topic, or phrase to search for in past conversations"
            },
            "limit": {
                "type": "INTEGER",
                "description": "Maximum number of matching conversation turns to return (default 5)"
            }
        },
        "required": ["query"]
    },
    "handler": search_conversation_history,
}
