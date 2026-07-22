import ast
from pathlib import Path
from typing import List, Dict, Any, Optional

import streamlit as st
from streamlit_autorefresh import st_autorefresh

# --------------- CONFIG ---------------

DEFAULT_LOG_PATH = "assistant.log"
MAX_EVENTS = 300  # only display last N parsed events

# Strings that indicate a tool usage line
TOOL_KEYWORDS = [
    "tooluse",
    "tool_use",
    "tool call",
    "tool_call",
    "tool:",
    "tool :",
    "tool use:",
    "tool use :",
    "toolinvocation",
]


# --------------- PARSING ---------------


def parse_timestamp_and_rest(line: str):
    """
    Parse a line like:
    2025-11-23 19:28:34.255 initialize_stream Execution time...
    into (timestamp_str, rest_of_line).
    If it doesn't match that pattern, returns (None, stripped_line).
    """
    parts = line.split(" ", 2)
    if len(parts) < 3:
        return None, line.strip()
    ts = f"{parts[0]} {parts[1]}"
    rest = parts[2].strip()
    return ts, rest


def parse_usage_dict(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract the Python dict from a 'UsageEvent: {...}' line and parse it.
    Returns None if parsing fails.
    """
    try:
        start = text.index("{")
    except ValueError:
        return None
    try:
        d = ast.literal_eval(text[start:])
    except Exception:
        return None
    return d


def is_tool_line(text: str) -> bool:
    """Heuristic: does this line look like tool usage?"""
    lower = text.lower()
    return any(k in lower for k in TOOL_KEYWORDS)


def extract_tool_message(text: str) -> str:
    """
    Try to strip leading 'Tool:' or similar; otherwise return full text.
    """
    stripped = text.strip()
    lower = stripped.lower()

    for prefix in ["tool:", "tool use:", "tool use", "tool call:", "tool call"]:
        if lower.startswith(prefix):
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip() or stripped
    return stripped


def parse_line_to_event(line: str) -> Optional[Dict[str, Any]]:
    """
    Convert a single log line into a structured event:
    - type: 'user', 'assistant', 'event', 'usage', 'tool'
    - time: timestamp string (may be None for bare User/Assistant lines)
    - message / label / details...
    """
    line = line.rstrip("\n")
    if not line:
        return None

    stripped = line.strip()

    # --- 1) Bare User/Assistant lines (NO timestamp) ---
    if stripped.startswith("User:"):
        msg = stripped.split("User:", 1)[1].strip()
        return {
            "type": "user",
            "time": None,
            "message": msg,
        }

    if stripped.startswith("Assistant:"):
        msg = stripped.split("Assistant:", 1)[1].strip()
        return {
            "type": "assistant",
            "time": None,
            "message": msg,
        }

    # Bare tool line without timestamp
    if is_tool_line(stripped):
        return {
            "type": "tool",
            "time": None,
            "message": extract_tool_message(stripped),
        }

    # --- 2) Timestamped lines ---
    ts, rest = parse_timestamp_and_rest(line)

    # User speech/text embedded in a timestamped line
    if "User:" in rest:
        msg = rest.split("User:", 1)[1].strip()
        return {
            "type": "user",
            "time": ts,
            "message": msg,
        }

    # Assistant speech/text embedded in a timestamped line
    if "Assistant:" in rest:
        msg = rest.split("Assistant:", 1)[1].strip()
        return {
            "type": "assistant",
            "time": ts,
            "message": msg,
        }

    # Tool usage embedded in a timestamped line
    if is_tool_line(rest):
        return {
            "type": "tool",
            "time": ts,
            "message": extract_tool_message(rest),
        }

    # Usage events
    if "UsageEvent:" in rest:
        usage_dict = parse_usage_dict(rest)
        input_tokens = output_tokens = total_tokens = None
        completion_id = None

        if usage_dict and "usageEvent" in usage_dict:
            ue = usage_dict["usageEvent"]
            completion_id = ue.get("completionId")
            total = ue.get("details", {}).get("total", {})
            inp = total.get("input", {})
            out = total.get("output", {})

            input_tokens = (inp.get("speechTokens", 0) or 0) + (
                inp.get("textTokens", 0) or 0
            )
            output_tokens = (out.get("speechTokens", 0) or 0) + (
                out.get("textTokens", 0) or 0
            )
            total_tokens = ue.get("totalTokens")

        return {
            "type": "usage",
            "time": ts,
            "completion_id": completion_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    # Completion start
    if "completionStart" in rest:
        return {"type": "event", "time": ts, "label": "Completion started"}

    # Barge-in
    if "Barge-in detected" in rest:
        return {
            "type": "event",
            "time": ts,
            "label": "Barge-in detected (user interrupted)",
        }

    # Content markers
    if "Content start detected" in rest:
        return {"type": "event", "time": ts, "label": "Content start detected"}

    if "Content end" in rest:
        return {"type": "event", "time": ts, "label": "Content end"}

    # Ignore everything else
    return None


def parse_log_file(
    path: str, limit: Optional[int] = MAX_EVENTS
) -> List[Dict[str, Any]]:
    """
    Read the log and turn it into a list of structured events.
    Only keep the last `limit` interesting events.
    """
    events: List[Dict[str, Any]] = []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ev = parse_line_to_event(line)
                if ev is not None:
                    events.append(ev)
    except FileNotFoundError:
        return []

    if limit is not None and len(events) > limit:
        events = events[-limit:]

    return events


# --------------- STREAMLIT UI ---------------

st.set_page_config(page_title="Hotel Voice Agent – Conversation Viewer", layout="wide")

# Auto-refresh every 3 seconds
st_autorefresh(interval=3000, key="log_viewer_autorefresh")

st.title("Hotel Voice Agent – Conversation Viewer")

with st.sidebar:
    st.header("Settings")
    log_path = st.text_input("Log file path", value=DEFAULT_LOG_PATH)
    show_events = st.checkbox(
        "Show event markers (Content start/end etc.)", value=False
    )

log_file = Path(log_path)
if not log_file.exists():
    st.error(f"Log file not found at: `{log_file.resolve()}`")
    st.stop()

events = parse_log_file(str(log_file), limit=MAX_EVENTS)

if not events:
    st.warning("No parsed conversation events yet. Speak to the agent.")
    st.stop()

usage_events = [e for e in events if e["type"] == "usage"]
chat_events = [e for e in events if e["type"] in ("user", "assistant", "event", "tool")]

# ---- SUMMARY ----
st.markdown("### Summary")

cols = st.columns(3)
with cols[0]:
    st.metric("Parsed events", len(events))

latest_usage = usage_events[-1] if usage_events else None

if latest_usage:
    with cols[1]:
        st.metric("Latest total tokens", latest_usage.get("total_tokens") or "–")
else:
    with cols[1]:
        st.metric("Latest total tokens", "–")

st.markdown("---")

# ---- MAIN CONVERSATION VIEW ----
st.markdown("### Conversation")

for ev in chat_events:
    etype = ev["type"]
    time_str = ev.get("time") or ""

    if etype == "user":
        st.markdown(
            f"""
<div style="padding: 8px 12px; margin-bottom: 8px; border-radius: 8px; background-color: #1f2933;">
  <div style="font-size: 0.8rem; opacity: 0.7;">User{(" • " + time_str) if time_str else ""}</div>
  <div style="margin-top: 4px;">{ev['message']}</div>
</div>
""",
            unsafe_allow_html=True,
        )

    elif etype == "assistant":
        st.markdown(
            f"""
<div style="padding: 8px 12px; margin-bottom: 8px; border-radius: 8px; background-color: #111827;">
  <div style="font-size: 0.8rem; opacity: 0.7;">Assistant{(" • " + time_str) if time_str else ""}</div>
  <div style="margin-top: 4px;">{ev['message']}</div>
</div>
""",
            unsafe_allow_html=True,
        )

    elif etype == "tool":
        st.markdown(
            f"""
<div style="padding: 6px 10px; margin-bottom: 6px; border-radius: 6px; background-color: #052e16;">
  <div style="font-size: 0.8rem; opacity: 0.8;">Tool call{(" • " + time_str) if time_str else ""}</div>
  <div style="font-size: 0.9rem; margin-top: 4px;">{ev['message']}</div>
</div>
""",
            unsafe_allow_html=True,
        )

    elif etype == "event" and show_events:
        st.markdown(
            f"""
<div style="font-size: 0.75rem; opacity: 0.6; margin: 4px 0;">
  Event: {ev.get('label', 'Event')}{(" • " + time_str) if time_str else ""}
</div>
""",
            unsafe_allow_html=True,
        )

# ---- USAGE (DROPDOWN) ----
if usage_events:
    st.markdown("---")
    with st.expander("Token usage events (last 10)"):
        for ev in usage_events[-10:]:
            time_str = ev.get("time") or ""
            st.markdown(
                f"""
<div style="padding: 6px 10px; margin-bottom: 6px; border-radius: 6px; background-color: #020617;">
  <div style="font-size: 0.8rem; opacity: 0.7;">Usage{(" • " + time_str) if time_str else ""}</div>
  <div style="font-size: 0.85rem; margin-top: 4px;">
    Total tokens: <b>{ev.get('total_tokens') or '–'}</b><br/>
    Input tokens: {ev.get('input_tokens') or 0} • Output tokens: {ev.get('output_tokens') or 0}<br/>
    Completion ID: <code>{ev.get('completion_id') or '-'}</code>
  </div>
</div>
""",
                unsafe_allow_html=True,
            )

st.markdown(
    """
<hr style="margin-top: 2rem; margin-bottom: 0.5rem;" />
<div style="font-size: 0.8rem; opacity: 0.6;">
This UI only reads <code>assistant.log</code> and never changes how your script logs.
The page auto-refreshes every 3 seconds to show new conversation turns.
Use the sidebar checkbox if you want to see low-level event markers (Content start/end etc.).
</div>
""",
    unsafe_allow_html=True,
)
