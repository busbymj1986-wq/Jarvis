# Use Jarvis from Claude Desktop (no API key needed)

Your Claude subscription covers the **Claude Desktop** app. Claude Desktop can use programs on
your own PC, so Jarvis plugs into it. You chat in the Claude app, and Claude books jobs, checks
clashes, reads your Outlook and drafts replies through Jarvis. There's no API key and no extra cost.

## Setup

Run the normal setup line in PowerShell. It now connects Claude Desktop for you:

```powershell
irm https://raw.githubusercontent.com/busbymj1986-wq/Jarvis/main/setup.ps1 | iex
```

Then **fully quit Claude Desktop** (right-click the Claude icon by the clock → **Quit**) and open it again.
Start a new chat and ask "What's on today?". The first time Claude uses a Jarvis tool, it asks
your permission. Choose **Allow always** for the tools you're happy with.

## What you can say

- "Add Sipho to the crew, 082 222 0000, extension 201"
- "Book Sipho for kerbs at Main Rd tomorrow 7 to 3"
- "Who's free Thursday afternoon?" / "Move job 14 to Friday 8 to 12"
- "Morning brief" (uses the `daily_brief` tool)
- With Outlook connected: "Anything urgent in my email?", "Reply to Pieter: quote by Friday", "What's on my calendar tomorrow?"

**Email is never sent from Claude Desktop.** Claude writes a draft. You send it from the Jarvis page
(http://localhost:5000 → *Emails waiting for you* → **Send**).

## Without an API key, these still work
- The Jarvis page: today's jobs, crew and drafts panels. Only the chat box there needs a key.
- Morning brief and evening wrap, sent as plain lists. Email alerts list new mail without AI sorting.
- Outlook calendar sync.

## Needs an API key (skip for now)
- The chat box on the Jarvis page
- The phone line (SIP/voice)

## Add it by hand (if the setup couldn't)

Claude Desktop → **Settings → Developer → Edit Config**, or open `%APPDATA%\Claude\claude_desktop_config.json`
in Notepad. Add this, replacing `MJ` with your Windows user name:

```json
{
  "mcpServers": {
    "jarvis": {
      "command": "C:\\Users\\MJ\\Jarvis\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\MJ\\Jarvis\\jarvis\\mcp_server.py"]
    }
  }
}
```

Save, then quit and reopen Claude Desktop.

## For a scheduler's PC
Add `"env": {"JARVIS_MCP_AS": "Lerato"}` to the `jarvis` entry. Claude then works with Lerato's
scheduling-only rights. If the name matches nobody, it gets read-only access, never admin.
