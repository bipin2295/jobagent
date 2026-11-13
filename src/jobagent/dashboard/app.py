"""Streamlit dashboard – Phase 0 baseline.

Shows system health, quick-action cheat-sheet, and today's stats.
Fleshed out progressively in Phases 3–6 (scored jobs, application list,
live browser feed, manual queue, Telegram controls).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Job Application Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Sidebar: quick links ──────────────────────────────────────────────────────

with st.sidebar:
    st.header("Links")
    st.markdown("**[noVNC live view](http://localhost:6080/vnc.html?autoconnect=1)**")
    st.markdown("Run agent via terminal:")
    st.code("make run", language="bash")
    st.divider()
    st.caption("Dashboard expands in Phases 3–6.")

# ── Page header ───────────────────────────────────────────────────────────────

st.title("🤖 Job Application Agent")
st.caption(f"Running in Docker  ·  {date.today().strftime('%A, %d %B %Y')}")

# ── Health check cards ────────────────────────────────────────────────────────

st.subheader("System health")
col1, col2, col3, col4, col5 = st.columns(5)

# 1. Profile
with col1:
    profile_path = Path(os.environ.get("JOBAGENT_DATA_DIR", "/app/data")) / "profile" / "master_profile.yaml"
    if profile_path.exists():
        mtime = datetime.fromtimestamp(profile_path.stat().st_mtime).strftime("%d %b")
        st.metric("Profile", "✓ Loaded", delta=f"Updated {mtime}")
    else:
        st.metric("Profile", "✗ Missing", delta="run: make import")

# 2. Database
with col2:
    db_path = Path(os.environ.get("JOBAGENT_DB_PATH", "/app/data/jobagent.db"))
    if db_path.exists():
        size_kb = db_path.stat().st_size / 1024
        st.metric("Database", "✓ Ready", delta=f"{size_kb:.1f} KB")
    else:
        st.metric("Database", "✗ Not init", delta="run: make db-init")

# 3. LLM key
with col3:
    has_key = bool(os.environ.get("JOBAGENT_PORTKEY_API_KEY", ""))
    st.metric("LLM (Portkey)", "✓ Key set" if has_key else "⚠ No key",
              delta=None if has_key else "add to .env")

# 4. Ollama
with col4:
    import httpx
    ollama_url = os.environ.get("JOBAGENT_OLLAMA_BASE_URL", "http://ollama:11434")
    try:
        resp = httpx.get(f"{ollama_url}/api/tags", timeout=2.0)
        models = [m["name"] for m in resp.json().get("models", [])]
        has_embed = any("nomic" in m for m in models)
        label = "✓ Running"
        note = "embed ✓" if has_embed else "⚠ run: make pull-models"
        st.metric("Ollama", label, delta=note)
    except Exception:
        st.metric("Ollama", "✗ Offline", delta="is ollama started?")

# 5. Gmail
with col5:
    token_path = Path(os.environ.get("JOBAGENT_DATA_DIR", "/app/data")) / "secrets" / "gmail_token.json"
    if token_path.exists():
        mtime = datetime.fromtimestamp(token_path.stat().st_mtime).strftime("%d %b")
        st.metric("Gmail", "✓ Connected", delta=f"Token from {mtime}")
    else:
        st.metric("Gmail", "⚠ Not auth'd", delta="run: make gmail-connect")

st.divider()

# ── Today's stats (requires DB) ───────────────────────────────────────────────

st.subheader("Today's stats")
try:
    from jobagent.db.engine import get_session
    from jobagent.db.models import Application, Job, Run
    from sqlmodel import func, select

    with get_session() as session:
        today_start = datetime.combine(date.today(), datetime.min.time())

        total_jobs    = session.exec(select(func.count(Job.id))).one()  # type: ignore[call-overload]
        today_jobs    = session.exec(select(func.count(Job.id)).where(Job.first_seen_at >= today_start)).one()  # type: ignore[call-overload]
        total_apps    = session.exec(select(func.count(Application.id))).one()  # type: ignore[call-overload]
        submitted     = session.exec(select(func.count(Application.id)).where(Application.status == "submitted")).one()  # type: ignore[call-overload]
        blocked       = session.exec(select(func.count(Application.id)).where(Application.status == "blocked")).one()  # type: ignore[call-overload]

    from jobagent.llm.cost import today_cost
    cost = today_cost()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Jobs discovered today", today_jobs)
    c2.metric("Jobs in DB (total)", total_jobs)
    c3.metric("Applications submitted", submitted)
    c4.metric("Blocked / skipped", blocked)
    c5.metric("LLM cost today", f"${cost:.4f}")

except Exception as exc:
    st.info(f"Stats not available yet — run `make db-init` first.  ({exc})")

st.divider()

# ── Quick-start cheat-sheet ───────────────────────────────────────────────────

st.subheader("Quick-start")

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.markdown("**🛠 First-time setup**")
    st.code("""\
make build
make up
make pull-models
make import
""", language="bash")

with col2:
    st.markdown("**▶ Run the pipeline**")
    st.code("""\
make run
# or one stage at a time:
make discover
make score
make tailor
make apply
""", language="bash")

with col3:
    st.markdown("**⏸ Control**")
    st.code("""\
make pause
make resume
make stop
make status
""", language="bash")

with col4:
    st.markdown("**🔑 Auth & keys**")
    st.code("""\
# Edit .env then:
make llm-ping
make gmail-connect

# Watch the browser:
make novnc
""", language="bash")

st.divider()
st.caption(
    "Phase 0 dashboard.  "
    "Full functionality (scored job list, application detail view, live browser feed, "
    "manual queue, Telegram controls) arrives in Phases 3–6."
)
