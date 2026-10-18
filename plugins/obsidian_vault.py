"""
Obsidian Vault Integration Plugin for JARVIS.
Allows creating, reading, searching, and appending notes directly in your Obsidian vault.
Works natively on markdown files with zero third-party dependencies.
"""

import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).resolve().parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from memory.config_manager import get_plugin_setting, save_plugin_config

PLUGIN = {
    "name": "obsidian_vault",
    "description": (
        "Interact with your Obsidian markdown vault. "
        "Create new notes, append thoughts/tasks, read existing notes, "
        "manage daily notes, or search notes across the entire vault. "
        "Use when user says 'add to Obsidian', 'write a note in Obsidian', "
        "'check my notes on...', or 'daily note'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "create_note | append_note | read_note | search_vault | daily_note | list_recent"
            },
            "title": {
                "type": "STRING",
                "description": "Note title or filename (without or with .md)"
            },
            "content": {
                "type": "STRING",
                "description": "Text content, bullet point, or markdown body"
            },
            "folder": {
                "type": "STRING",
                "description": "Subfolder within vault (e.g., 'Projects', 'Daily', optional)"
            },
            "query": {
                "type": "STRING",
                "description": "Search keyword or phrase for searching vault"
            }
        },
        "required": ["action"]
    }
}

PLUGIN_SETTINGS = {
    "namespace": "obsidian_vault",
    "title": "Obsidian Vault Settings",
    "fields": [
        {
            "name": "vault_path",
            "label": "Obsidian Vault Directory Path",
            "type": "text",
            "default": str(Path.home() / "Documents" / "Obsidian Vault")
        },
        {
            "name": "daily_folder",
            "label": "Daily Notes Subfolder (optional)",
            "type": "text",
            "default": "Daily"
        }
    ]
}

def _get_vault_dir(params: dict) -> Path:
    # 1. From params if given
    v_path = params.get("vault_path")
    if not v_path:
        v_path = get_plugin_setting("obsidian_vault", "vault_path", "")
    
    if v_path:
        p = Path(v_path).expanduser().resolve()
        if p.exists() and p.is_dir():
            return p

    # 2. Common default locations
    common_locations = [
        Path.home() / "Documents" / "Obsidian Vault",
        Path.home() / "Obsidian",
        Path.home() / "Documents" / "Notes",
        Path.home() / "Vault",
    ]
    for loc in common_locations:
        if loc.exists() and loc.is_dir():
            return loc

    # Fallback: create default folder in Documents
    default_dir = Path.home() / "Documents" / "Obsidian Vault"
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir

def _action_create_note(vault: Path, title: str, content: str, folder: str = "") -> str:
    if not title:
        title = f"Note_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not title.endswith(".md"):
        title += ".md"

    target_dir = vault / folder if folder else vault
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / title

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"---\ncreated: {now_str}\ntags: [jarvis]\n---\n\n"
    full_content = header + (content or "")

    target_file.write_text(full_content, encoding="utf-8")
    rel_path = target_file.relative_to(vault)
    return f"Created note '{rel_path}' in Obsidian, Sir."

def _action_append_note(vault: Path, title: str, content: str, folder: str = "") -> str:
    if not title:
        return "Please specify which note to append to, Sir."
    if not title.endswith(".md"):
        title += ".md"

    # Search for matching file if folder not specified
    target_file = None
    if folder:
        f = vault / folder / title
        if f.exists():
            target_file = f
    else:
        for f in vault.rglob(f"*{title}"):
            if f.is_file():
                target_file = f
                break

    if not target_file or not target_file.exists():
        # Fallback to create if does not exist
        return _action_create_note(vault, title, content, folder)

    now_str = datetime.now().strftime("%H:%M")
    entry = f"\n- [{now_str}] {content.strip()}"
    with open(target_file, "a", encoding="utf-8") as fp:
        fp.write(entry)

    rel_path = target_file.relative_to(vault)
    return f"Appended entry to '{rel_path}', Sir."

def _action_daily_note(vault: Path, content: str) -> str:
    daily_subfolder = get_plugin_setting("obsidian_vault", "daily_folder", "Daily")
    today_str = datetime.now().strftime("%Y-%m-%d")
    title = f"{today_str}.md"
    target_dir = vault / daily_subfolder if daily_subfolder else vault
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / title

    now_time = datetime.now().strftime("%H:%M")
    if not target_file.exists():
        header = f"# Daily Note — {today_str}\n\n## Log\n"
        body = f"- [{now_time}] {content.strip()}\n" if content else ""
        target_file.write_text(header + body, encoding="utf-8")
        return f"Initialized today's daily note ({today_str}) in Obsidian, Sir."
    else:
        if content:
            with open(target_file, "a", encoding="utf-8") as fp:
                fp.write(f"\n- [{now_time}] {content.strip()}")
            return f"Updated daily note for {today_str}, Sir."
        else:
            text = target_file.read_text(encoding="utf-8")
            return f"Daily note for {today_str}:\n{text[:600]}"

def _action_read_note(vault: Path, title: str) -> str:
    if not title:
        return "Please specify the note title to read, Sir."
    clean_title = title.replace(".md", "")
    matches = list(vault.rglob(f"*{clean_title}*.md"))
    if not matches:
        return f"I could not find a note matching '{title}' in your vault, Sir."

    target = matches[0]
    try:
        content = target.read_text(encoding="utf-8")
        rel_path = target.relative_to(vault)
        preview = content[:800] + ("\n... [truncated]" if len(content) > 800 else "")
        return f"Contents of '{rel_path}':\n\n{preview}"
    except Exception as e:
        return f"Error reading note '{title}': {e}"

def _action_search_vault(vault: Path, query: str) -> str:
    if not query:
        return "Please provide a search query, Sir."

    q_lower = query.lower()
    matches = []
    for md_file in vault.rglob("*.md"):
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
            if q_lower in md_file.name.lower() or q_lower in content.lower():
                rel = md_file.relative_to(vault)
                # Find matching line snippet
                for line in content.splitlines():
                    if q_lower in line.lower():
                        snippet = line.strip()[:100]
                        matches.append(f"• [[{rel.stem}]]: {snippet}")
                        break
                else:
                    matches.append(f"• [[{rel.stem}]]")
                if len(matches) >= 5:
                    break
        except Exception:
            continue

    if not matches:
        return f"No notes found in your vault matching '{query}', Sir."
    return f"Found {len(matches)} matching note(s) for '{query}':\n" + "\n".join(matches)

def _action_list_recent(vault: Path) -> str:
    files = list(vault.rglob("*.md"))
    if not files:
        return "Your Obsidian vault currently has no markdown notes, Sir."
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    recent = files[:6]
    lines = []
    for f in recent:
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d %H:%M")
        lines.append(f"• {f.stem} ({mtime})")
    return "Most recently modified notes in Obsidian:\n" + "\n".join(lines)

def run(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "list_recent").lower().strip()
    title = parameters.get("title", "")
    content = parameters.get("content", "")
    folder = parameters.get("folder", "")
    query = parameters.get("query", "")

    vault = _get_vault_dir(parameters)

    try:
        if action in ("create_note", "create", "new"):
            result = _action_create_note(vault, title, content, folder)
        elif action in ("append_note", "append", "add"):
            result = _action_append_note(vault, title, content, folder)
        elif action in ("daily_note", "daily", "journal"):
            result = _action_daily_note(vault, content)
        elif action in ("read_note", "read", "view"):
            result = _action_read_note(vault, title)
        elif action in ("search_vault", "search", "find"):
            result = _action_search_vault(vault, query or title)
        elif action in ("list_recent", "recent", "list"):
            result = _action_list_recent(vault)
        else:
            result = f"Unknown Obsidian action '{action}'. Supported: create_note, append_note, daily_note, read_note, search_vault, list_recent."
    except Exception as e:
        result = f"Obsidian error: {e}"

    if player:
        try:
            player.write_log(f"[Obsidian] {result}")
        except Exception:
            pass

    return result
