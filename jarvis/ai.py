"""Claude and Grok agents that manage staff through the shared tools.

Both run the same loop: send the conversation + tools, execute any tool calls
against the store, send results back, stop when the model answers in text.
"""
import json
import os
from datetime import datetime

import requests

from . import tools

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
GROK_MODEL = os.environ.get("XAI_MODEL", "grok-4")
XAI_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1") + "/chat/completions"
MAX_STEPS = 8


class AgentError(Exception):
    pass


def system_prompt(caller=None, voice=False):
    who = "the operations manager (API/dashboard)"
    if caller:
        who = f"{caller['name']} ({'manager' if caller.get('is_manager') else 'crew'}, staff id {caller['id']})"
    parts = [
        "You are Jarvis, the dispatch assistant for a field-service team. You manage staff (crew), "
        "book and assign jobs, spot schedule clashes and message staff using the tools provided.",
        f"Current local time: {datetime.now().strftime('%A %Y-%m-%d %H:%M')}. You are talking to {who}.",
        "Always use tools to read or change data; never invent staff, jobs or ids. If a name is ambiguous, "
        "ask. If a tool reports a clash, tell the user and only retry with force=true when they confirm.",
        "Confirm what you changed in one line.",
    ]
    if voice:
        parts.append("This is a phone call: reply in one or two short spoken sentences, no lists, "
                     "no markdown, say times like 'eight thirty'.")
    return "\n".join(parts)


def _role(caller):
    return "manager" if caller is None or caller.get("is_manager") else "crew"


# ---------------- Claude ----------------

_claude_client = None


def _claude():
    global _claude_client
    if _claude_client is None:
        import anthropic
        _claude_client = anthropic.Anthropic()
    return _claude_client


def run_claude(message, history=None, caller=None, voice=False):
    import anthropic

    messages = [*(history or []), {"role": "user", "content": message}]
    actions = []
    for _ in range(MAX_STEPS):
        try:
            resp = _claude().beta.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=4000 if voice else 16000,
                system=system_prompt(caller, voice),
                tools=tools.claude_tools(_role(caller)),
                messages=messages,
                output_config={"effort": "low" if voice else "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIStatusError as e:
            raise AgentError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AgentError("Could not reach the Claude API") from e

        if resp.stop_reason == "refusal":
            return {"reply": "Sorry, I can't help with that request.", "actions": actions}
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue

        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            return {"reply": text, "actions": actions}

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for call in calls:
            out, is_error = tools.execute_tool(call.name, call.input, caller)
            actions.append({"tool": call.name, "input": call.input, "result": json.loads(out)})
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return {"reply": "I ran out of steps on that one — please try a simpler request.", "actions": actions}


# ---------------- Grok (xAI) ----------------

def run_grok(message, history=None, caller=None, voice=False):
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise AgentError("XAI_API_KEY is not set")
    messages = [{"role": "system", "content": system_prompt(caller, voice)},
                *(history or []), {"role": "user", "content": message}]
    actions = []
    for _ in range(MAX_STEPS):
        try:
            r = requests.post(XAI_URL, timeout=90, headers={"Authorization": f"Bearer {key}"},
                              json={"model": GROK_MODEL, "messages": messages,
                                    "tools": tools.grok_tools(_role(caller)), "tool_choice": "auto"})
        except requests.RequestException as e:
            raise AgentError("Could not reach the xAI API") from e
        if r.status_code >= 400:
            raise AgentError(f"xAI API error {r.status_code}: {r.text[:300]}")
        msg = r.json()["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"reply": (msg.get("content") or "").strip(), "actions": actions}

        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                out, args = json.dumps({"ok": False, "error": "Invalid JSON arguments"}), {}
            else:
                out, _ = tools.execute_tool(fn["name"], args, caller)
            actions.append({"tool": fn["name"], "input": args, "result": json.loads(out)})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
    return {"reply": "I ran out of steps on that one — please try a simpler request.", "actions": actions}


PROVIDERS = {"claude": run_claude, "grok": run_grok}


def default_provider():
    return os.environ.get("JARVIS_PROVIDER", "claude")


def run_agent(message, provider=None, history=None, caller=None, voice=False):
    provider = (provider or default_provider()).lower()
    if provider not in PROVIDERS:
        raise AgentError(f"Unknown provider '{provider}', use claude or grok")
    result = PROVIDERS[provider](message, history=history, caller=caller, voice=voice)
    result["provider"] = provider
    return result


def desk_note(day="today", provider="grok"):
    """The 'Grok desk note': a short manager briefing for the day."""
    prompt = (f"Write the desk note for {day}: call day_sheet and list_staff, then give a short briefing — "
              "who is working where, unassigned jobs, clashes or gaps, and anyone with nothing booked. "
              "Plain text, under 120 words. Do not change any data.")
    return run_agent(prompt, provider=provider)
