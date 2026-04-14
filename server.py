"""
Investor Relations MCP — Seed raise pipeline tracker for founders.

FastMCP server with 6 tools:
  - investor_search    : Search and filter investor pipeline
  - investor_profile   : Full investor profile with interaction history
  - investor_prep      : Meeting prep with objections, rebuttals, agenda
  - investor_update    : Update stage, log interactions, add objections
  - investor_analytics : Pipeline health and follow-up compliance
  - investor_sync      : Re-sync investor data from vault CRM files

Configuration:
  pitch_config.yaml   — your pitch positioning and global objections (fill this in)

Environment variables:
  INVESTOR_MCP_DB         — path to SQLite database (default: ./investors.db)
  INVESTOR_MCP_VAULT_CRM  — path to vault CRM folder (default: ~/vault/CRM/)
"""

import json
import os
import re
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent / "pitch_config.yaml"
with open(_CONFIG_PATH) as f:
    _CONFIG = yaml.safe_load(f) or {}

COMPANY_NAME = _CONFIG.get("company_name", "Your Company")
RAISE_AMOUNT = _CONFIG.get("raise_amount", "TBD")
RAISE_DESCRIPTION = _CONFIG.get("raise_description", "seed round")

PITCH_POSITIONING = _CONFIG.get("pitch_positioning", {})
GLOBAL_OBJECTIONS_CONFIG = _CONFIG.get("global_objections", [])

PIPELINE_STAGES = _CONFIG.get("pipeline_stages", [
    "not_contacted", "outreach_sent", "response_received",
    "meeting_scheduled", "pitched", "decision_pending",
    "committed", "passed",
])

DB_PATH = Path(os.environ.get(
    "INVESTOR_MCP_DB",
    str(Path(__file__).parent / "investors.db"),
))
CRM_PATH = Path(os.environ.get(
    "INVESTOR_MCP_VAULT_CRM",
    os.path.expanduser("~/vault/CRM"),
))

# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db():
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS investors (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL UNIQUE,
            vault_file      TEXT,
            email           TEXT,
            phone           TEXT,
            company         TEXT,
            role            TEXT,
            location        TEXT,
            linkedin        TEXT,
            relationship    TEXT DEFAULT 'investor',
            pipeline_stage  TEXT DEFAULT 'not_contacted',
            priority        TEXT DEFAULT 'medium',
            status          TEXT DEFAULT 'active',
            source          TEXT,
            check_size_min  REAL DEFAULT 0,
            check_size_max  REAL DEFAULT 0,
            thesis          TEXT,
            portfolio_notes TEXT,
            next_step       TEXT,
            notes           TEXT,
            intro_path      TEXT DEFAULT 'cold',
            mutual_connection TEXT DEFAULT '',
            deck_version_sent TEXT DEFAULT '',
            apollo_id       TEXT DEFAULT '',
            hubspot_deal_id TEXT DEFAULT '',
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id     INTEGER NOT NULL REFERENCES investors(id),
            interaction_date TEXT NOT NULL,
            interaction_type TEXT NOT NULL,
            summary         TEXT,
            source          TEXT DEFAULT 'manual',
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS objections (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id     INTEGER REFERENCES investors(id),
            objection       TEXT NOT NULL,
            rebuttal        TEXT,
            is_global       INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_investors_stage ON investors(pipeline_stage);
        CREATE INDEX IF NOT EXISTS idx_investors_priority ON investors(priority);
        CREATE INDEX IF NOT EXISTS idx_interactions_investor ON interactions(investor_id);
        CREATE INDEX IF NOT EXISTS idx_interactions_date ON interactions(interaction_date);
    """)

    # Seed global objections from pitch_config.yaml (only if table is empty)
    count = conn.execute("SELECT COUNT(*) FROM objections WHERE is_global=1").fetchone()[0]
    if count == 0 and GLOBAL_OBJECTIONS_CONFIG:
        conn.executemany(
            "INSERT INTO objections (objection, rebuttal, is_global) VALUES (?, ?, 1)",
            [(pair[0], pair[1]) for pair in GLOBAL_OBJECTIONS_CONFIG if len(pair) == 2],
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Vault CRM sync
# ---------------------------------------------------------------------------

def _parse_frontmatter(content: str) -> dict:
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _parse_timeline(content: str) -> list[dict]:
    entries = []
    in_timeline = False
    for line in content.split("\n"):
        stripped = line.strip()
        if re.match(r"^##\s+Timeline", stripped, re.IGNORECASE):
            in_timeline = True
            continue
        if in_timeline and stripped.startswith("## "):
            break
        if in_timeline and stripped.startswith("- "):
            m = re.match(r"^-\s+(\d{4}-\d{2}-\d{2})\s*[-\u2014:]+\s*(.*)", stripped)
            if m:
                summary = m.group(2).strip()
                lower = summary.lower()
                if any(w in lower for w in ["meeting", "met", "call", "zoom", "coffee"]):
                    itype = "meeting"
                elif any(w in lower for w in ["email", "sent", "replied", "followed up"]):
                    itype = "email"
                elif any(w in lower for w in ["event", "conference", "networking", "panel"]):
                    itype = "event"
                elif any(w in lower for w in ["whatsapp", "message", "text", "dm"]):
                    itype = "message"
                else:
                    itype = "note"
                entries.append({"date": m.group(1), "type": itype, "summary": summary})
    return entries


def _extract_bio(content: str) -> str:
    cleaned = re.sub(r"^---.*?---\s*", "", content, flags=re.DOTALL)
    lines = []
    for line in cleaned.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            if lines:
                break
            continue
        if stripped:
            lines.append(stripped)
        elif lines:
            break
    return " ".join(lines)[:500] if lines else ""


def _map_status_to_stage(fm: dict) -> str:
    status = str(fm.get("status", "")).lower()
    next_step = str(fm.get("next_step", "")).lower()
    if status == "committed":
        return "committed"
    if status in ("passed", "declined"):
        return "passed"
    if "meeting" in next_step or "call" in next_step:
        return "meeting_scheduled"
    if "pitch" in next_step or "deck" in next_step:
        return "pitched"
    if "decision" in next_step or "waiting" in next_step:
        return "decision_pending"
    if "follow" in next_step or "reconnect" in next_step:
        return "response_received"
    if "outreach" in next_step or "intro" in next_step or "reach out" in next_step:
        return "outreach_sent"
    if status in ("warm", "active"):
        return "response_received"
    if status == "inactive":
        return "not_contacted"
    return "not_contacted"


def _sync_from_vault(crm_path: Optional[Path] = None) -> dict:
    crm = crm_path or CRM_PATH
    if not crm.exists():
        return {"error": f"CRM directory not found: {crm}", "synced": 0, "skipped": 0, "errors": []}

    conn = _connect()
    synced = 0
    skipped = 0
    errors = []

    for filename in sorted(os.listdir(crm)):
        if not filename.endswith(".md"):
            continue
        filepath = crm / filename
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            errors.append(f"{filename}: {e}")
            continue

        fm = _parse_frontmatter(content)
        rel = fm.get("relationship", "")
        if isinstance(rel, list):
            rel = rel[0] if rel else ""
        if str(rel).lower() != "investor":
            skipped += 1
            continue

        name = filename.replace(".md", "")
        data = {
            "name": name,
            "vault_file": filename,
            "email": str(fm.get("email", "")),
            "phone": str(fm.get("phone", "")),
            "company": fm.get("company", ""),
            "role": fm.get("role", fm.get("title", "")),
            "location": fm.get("location", fm.get("city", "")),
            "linkedin": fm.get("linkedin", ""),
            "pipeline_stage": _map_status_to_stage(fm),
            "priority": fm.get("priority", "medium"),
            "status": fm.get("status", "active"),
            "source": fm.get("source", ""),
            "next_step": fm.get("next_step", ""),
            "intro_path": fm.get("intro_path", fm.get("intro", "cold")),
            "mutual_connection": fm.get("mutual_connection", ""),
            "notes": _extract_bio(content),
        }

        existing = conn.execute("SELECT id FROM investors WHERE name=?", (name,)).fetchone()
        if existing:
            sets = []
            vals = []
            for k, v in data.items():
                if k == "name":
                    continue
                sets.append(f"{k}=?")
                vals.append(v)
            sets.append("updated_at=?")
            vals.append(datetime.now().isoformat())
            vals.append(existing["id"])
            conn.execute(f"UPDATE investors SET {', '.join(sets)} WHERE id=?", vals)
            investor_id = existing["id"]
        else:
            cols = list(data.keys())
            placeholders = ", ".join(["?"] * len(cols))
            conn.execute(
                f"INSERT INTO investors ({', '.join(cols)}) VALUES ({placeholders})",
                [data[c] for c in cols],
            )
            investor_id = conn.execute("SELECT id FROM investors WHERE name=?", (name,)).fetchone()["id"]

        # Sync timeline entries
        timeline = _parse_timeline(content)
        existing_interactions = set()
        for row in conn.execute(
            "SELECT interaction_date, summary FROM interactions WHERE investor_id=? AND source='vault'",
            (investor_id,),
        ).fetchall():
            existing_interactions.add((row["interaction_date"], row["summary"]))

        for entry in timeline:
            key = (entry["date"], entry["summary"])
            if key not in existing_interactions:
                conn.execute(
                    "INSERT INTO interactions (investor_id, interaction_date, interaction_type, summary, source) "
                    "VALUES (?, ?, ?, ?, 'vault')",
                    (investor_id, entry["date"], entry["type"], entry["summary"]),
                )

        synced += 1

    conn.commit()
    conn.close()
    return {"synced": synced, "skipped": skipped, "errors": errors, "crm_path": str(crm)}


# ---------------------------------------------------------------------------
# Meeting prep generator
# ---------------------------------------------------------------------------

def _rank_objections(investor: dict, objections: list[dict]) -> list[dict]:
    specific = [o for o in objections if o.get("investor_id") and not o.get("is_global")]
    global_objs = [o for o in objections if o.get("is_global")]
    location = (investor.get("location") or "").lower()
    notes = (investor.get("notes") or "").lower()
    scored = []
    for obj in global_objs:
        score = 0
        text = obj["objection"].lower()
        if "market" in text and any(w in location for w in ["us", "nyc", "new york", "london"]):
            score += 3
        if "take rate" in text:
            score += 1
        scored.append((score, obj))
    scored.sort(key=lambda x: x[0], reverse=True)
    return specific + [obj for _, obj in scored]


def _generate_meeting_prep(investor_name: str) -> str:
    conn = _connect()
    row = conn.execute("SELECT * FROM investors WHERE name=?", (investor_name,)).fetchone()
    if not row:
        conn.close()
        return f"Investor '{investor_name}' not found. Run investor_search first."

    investor = dict(row)
    interactions = [
        dict(r) for r in conn.execute(
            "SELECT * FROM interactions WHERE investor_id=? ORDER BY interaction_date DESC",
            (investor["id"],),
        ).fetchall()
    ]
    objections = [
        dict(r) for r in conn.execute(
            "SELECT * FROM objections WHERE investor_id=? OR is_global=1 ORDER BY is_global ASC",
            (investor["id"],),
        ).fetchall()
    ]
    conn.close()

    sections = []
    sections.append(f"# Meeting Prep: {investor['name']}")
    sections.append(f"**Generated:** {date.today().isoformat()}")
    sections.append(f"**Company:** {COMPANY_NAME} | **Raise:** {RAISE_AMOUNT} {RAISE_DESCRIPTION}")
    sections.append("")

    # 1. Profile
    sections.append("## 1. Investor Profile")
    sections.append("")
    for label, key in [
        ("Company", "company"), ("Role", "role"), ("Location", "location"),
        ("Email", "email"), ("LinkedIn", "linkedin"), ("Pipeline Stage", "pipeline_stage"),
        ("Priority", "priority"), ("Source", "source"), ("Intro Path", "intro_path"),
        ("Mutual Connection", "mutual_connection"),
    ]:
        val = investor.get(key)
        if val:
            sections.append(f"- **{label}:** {val}")
    if investor.get("thesis"):
        sections.append(f"- **Investment Thesis:** {investor['thesis']}")
    if investor.get("notes"):
        sections.append(f"\n**Bio:** {investor['notes']}")
    sections.append("")

    # 2. Interaction history
    sections.append(f"## 2. Interaction History ({len(interactions)} total)")
    sections.append("")
    for ix in interactions[:10]:
        sections.append(f"- **{ix['interaction_date']}** ({ix['interaction_type']}): {ix['summary']}")
    if not interactions:
        sections.append("*No interactions logged yet.*")
    sections.append("")

    # 3. Portfolio fit — based on thesis/notes keywords
    sections.append("## 3. Portfolio Fit Analysis")
    sections.append("")
    combined = f"{investor.get('thesis', '')} {investor.get('company', '')} {investor.get('notes', '')}".lower()
    location_lower = (investor.get("location") or "").lower()
    fit_notes = []
    if any(w in combined for w in ["latam", "latin", "emerging", "developing"]):
        fit_notes.append("STRONG FIT: Emerging market / LATAM focus matches your geography")
    if any(w in combined for w in ["marketplace", "platform", "b2b", "enterprise"]):
        fit_notes.append("STRONG FIT: Marketplace / B2B thesis matches your model")
    if any(w in combined for w in ["ai", "ml", "data", "automation", "tech"]):
        fit_notes.append("MODERATE FIT: AI/tech interest — emphasize your AI augmentation angle")
    if any(w in combined for w in ["seed", "pre-seed", "early", "angel"]):
        fit_notes.append(f"STAGE FIT: Early-stage investor matches your {RAISE_DESCRIPTION}")
    if any(w in combined for w in ["impact", "social", "sustainability"]):
        fit_notes.append("VALUES FIT: Impact orientation — highlight job creation and market development angles")
    if not fit_notes:
        fit_notes.append("No strong thesis signals found — lead with traction and unit economics")
    for note in fit_notes:
        sections.append(f"- {note}")
    sections.append("")

    # 4. Top objections
    sections.append("## 4. Top 3 Likely Objections")
    sections.append("")
    ranked = _rank_objections(investor, objections)
    for i, obj in enumerate(ranked[:3], 1):
        sections.append(f"### Objection {i}: {obj['objection']}")
        sections.append(f"**Rebuttal:** {obj['rebuttal']}")
        sections.append("")

    # 5. Pitch positioning
    sections.append("## 5. Pitch Positioning Reminders")
    sections.append("")
    for key, label in [("market", "Market"), ("model", "Model"), ("traction", "Traction"), ("ask", "The Ask")]:
        if PITCH_POSITIONING.get(key):
            sections.append(f"**{label}:** {PITCH_POSITIONING[key]}")
    if PITCH_POSITIONING.get("never_say"):
        sections.append("")
        sections.append(f"**NEVER SAY:** {PITCH_POSITIONING['never_say']}")
    sections.append("")

    # 6. Suggested agenda
    sections.append("## 6. Suggested Agenda")
    sections.append("")
    stage = investor.get("pipeline_stage", "not_contacted")
    if stage in ("not_contacted", "outreach_sent"):
        agenda = [
            "Intro + rapport building (5 min)",
            f"{COMPANY_NAME} overview: problem, solution, traction (10 min)",
            "Key client case study + unit economics (5 min)",
            "Their investment thesis + portfolio fit discussion (5 min)",
            "Q&A + next steps (5 min)",
        ]
    elif stage in ("response_received", "meeting_scheduled"):
        agenda = [
            "Quick check-in + updates since last touch (3 min)",
            "Deep dive on market opportunity + competitive landscape (8 min)",
            "Product demo or walkthrough (7 min)",
            "Business model + financial projections (7 min)",
            f"The ask: {RAISE_AMOUNT} {RAISE_DESCRIPTION}, use of funds, timeline (3 min)",
            "Q&A + next steps (2 min)",
        ]
    elif stage == "pitched":
        agenda = [
            "Address outstanding questions from pitch (5 min)",
            "New traction updates or milestones (5 min)",
            "Terms discussion if applicable (10 min)",
            "Timeline and decision process (5 min)",
            "Next steps + commitment ask (5 min)",
        ]
    elif stage == "decision_pending":
        agenda = [
            "Check in on decision timeline (3 min)",
            "Address any new concerns (10 min)",
            "Share latest wins / momentum (5 min)",
            "Soft close: what would move you to commit? (7 min)",
            "Confirm next steps (5 min)",
        ]
    else:
        agenda = [
            "Relationship maintenance (5 min)",
            "Company updates (10 min)",
            "Ask for intros / referrals (5 min)",
            "Next steps (5 min)",
        ]
    for i, item in enumerate(agenda, 1):
        sections.append(f"{i}. {item}")
    sections.append("")

    if investor.get("next_step"):
        sections.append("## 7. Current Next Step")
        sections.append(investor["next_step"])

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Initialize DB
# ---------------------------------------------------------------------------

_init_db()

mcp = FastMCP(
    "investor-relations",
    instructions=(
        f"Seed raise pipeline tracker for {COMPANY_NAME}'s {RAISE_AMOUNT} {RAISE_DESCRIPTION}. "
        "Use investor_search to find investors, investor_profile for details, "
        "investor_prep for meeting preparation, investor_update to log interactions "
        "and stage changes, investor_analytics for pipeline health and follow-up compliance. "
        "Run investor_sync to refresh data from vault CRM files."
    ),
)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def investor_search(
    query: str = "",
    stage: str = "",
    priority: str = "",
    stale_days: int = 0,
) -> str:
    """Search the investor pipeline with optional filters.

    Args:
        query: Text search across name, company, notes. Leave empty for all.
        stage: Filter by pipeline stage (not_contacted, outreach_sent, response_received,
               meeting_scheduled, pitched, decision_pending, committed, passed).
        priority: Filter by priority: high, medium, low.
        stale_days: Only show investors with no interaction in N+ days.
    """
    conn = _connect()
    conditions = []
    params: list = []

    if query:
        conditions.append("(name LIKE ? OR company LIKE ? OR notes LIKE ?)")
        q = f"%{query}%"
        params.extend([q, q, q])
    if stage:
        if stage not in PIPELINE_STAGES:
            return f"Invalid stage. Valid: {', '.join(PIPELINE_STAGES)}"
        conditions.append("pipeline_stage=?")
        params.append(stage)
    if priority:
        conditions.append("priority=?")
        params.append(priority)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM investors {where} ORDER BY priority DESC, updated_at DESC", params
    ).fetchall()
    results = [dict(r) for r in rows]

    if stale_days > 0:
        filtered = []
        for inv in results:
            last = conn.execute(
                "SELECT MAX(interaction_date) as last_date FROM interactions WHERE investor_id=?",
                (inv["id"],),
            ).fetchone()
            if last and last["last_date"]:
                try:
                    last_dt = datetime.strptime(last["last_date"], "%Y-%m-%d").date()
                    days = (date.today() - last_dt).days
                    if days >= stale_days:
                        inv["days_since_contact"] = days
                        filtered.append(inv)
                except ValueError:
                    inv["days_since_contact"] = None
                    filtered.append(inv)
            else:
                inv["days_since_contact"] = None
                filtered.append(inv)
        results = filtered

    conn.close()

    if not results:
        return "No investors found matching your criteria."

    lines = [f"**Found {len(results)} investor(s):**\n"]
    for inv in results:
        stale_info = ""
        if "days_since_contact" in inv:
            d = inv["days_since_contact"]
            stale_info = f" | {d}d since contact" if d else " | never contacted"
        lines.append(
            f"- **{inv['name']}** ({inv['pipeline_stage']}) [{inv['priority']}]{stale_info}"
        )
        if inv.get("company"):
            lines.append(f"  Company: {inv['company']}")
        if inv.get("next_step"):
            lines.append(f"  Next step: {inv['next_step']}")

    return "\n".join(lines)


@mcp.tool()
def investor_profile(name: str) -> str:
    """Get the full profile for an investor including interaction history.

    Args:
        name: Investor name (exact match preferred, partial match as fallback).
    """
    conn = _connect()
    row = conn.execute("SELECT * FROM investors WHERE name=?", (name,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM investors WHERE name LIKE ?", (f"%{name}%",)).fetchone()
    if not row:
        conn.close()
        return f"Investor '{name}' not found. Run investor_sync to refresh from vault."

    investor = dict(row)
    interactions = [
        dict(r) for r in conn.execute(
            "SELECT * FROM interactions WHERE investor_id=? ORDER BY interaction_date DESC",
            (investor["id"],),
        ).fetchall()
    ]
    conn.close()

    lines = [f"# {investor['name']}", ""]
    for label, key in [
        ("Pipeline Stage", "pipeline_stage"), ("Priority", "priority"),
        ("Status", "status"), ("Company", "company"), ("Role", "role"),
        ("Location", "location"), ("Email", "email"), ("Phone", "phone"),
        ("LinkedIn", "linkedin"), ("Source", "source"), ("Intro Path", "intro_path"),
        ("Mutual Connection", "mutual_connection"), ("Investment Thesis", "thesis"),
        ("Portfolio Notes", "portfolio_notes"), ("Next Step", "next_step"),
        ("Deck Version Sent", "deck_version_sent"),
    ]:
        val = investor.get(key)
        if val:
            lines.append(f"**{label}:** {val}")

    min_c = investor.get("check_size_min", 0)
    max_c = investor.get("check_size_max", 0)
    if min_c or max_c:
        lines.append(f"**Check Size:** ${min_c:,.0f} - ${max_c:,.0f}")

    if investor.get("notes"):
        lines.append("")
        lines.append(f"**Notes:** {investor['notes']}")
    lines.append("")

    lines.append(f"## Interaction History ({len(interactions)} total)")
    lines.append("")
    if interactions:
        for ix in interactions:
            lines.append(
                f"- {ix['interaction_date']} ({ix['interaction_type']}): "
                f"{ix['summary']} [{ix['source']}]"
            )
    else:
        lines.append("*No interactions logged.*")

    return "\n".join(lines)


@mcp.tool()
def investor_prep(name: str) -> str:
    """Generate a meeting prep document for an investor.

    Includes: profile, portfolio fit analysis, top 3 objections with rebuttals,
    pitch positioning reminders (from pitch_config.yaml), and a stage-appropriate agenda.

    Args:
        name: Investor name.
    """
    return _generate_meeting_prep(name)


@mcp.tool()
def investor_update(
    name: str,
    stage: str = "",
    priority: str = "",
    next_step: str = "",
    interaction_date: str = "",
    interaction_type: str = "",
    interaction_summary: str = "",
    thesis: str = "",
    portfolio_notes: str = "",
    check_size_min: float = 0,
    check_size_max: float = 0,
    deck_version: str = "",
    objection: str = "",
    objection_rebuttal: str = "",
) -> str:
    """Update an investor's pipeline stage, priority, or log a new interaction.

    Args:
        name: Investor name.
        stage: New pipeline stage.
        priority: New priority: high, medium, low.
        next_step: What to do next with this investor.
        interaction_date: Date of interaction (YYYY-MM-DD).
        interaction_type: email, meeting, call, event, message, note.
        interaction_summary: What happened.
        thesis: Update investor's investment thesis notes.
        portfolio_notes: Update portfolio/relevant companies notes.
        check_size_min: Minimum check size in USD.
        check_size_max: Maximum check size in USD.
        deck_version: Pitch deck version sent.
        objection: New objection raised by this investor.
        objection_rebuttal: Rebuttal for the objection.
    """
    conn = _connect()
    row = conn.execute("SELECT * FROM investors WHERE name=?", (name,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM investors WHERE name LIKE ?", (f"%{name}%",)).fetchone()
    if not row:
        conn.close()
        return f"Investor '{name}' not found. Use investor_search to find the correct name."

    investor_id = row["id"]
    actual_name = row["name"]
    changes = []

    update_fields = {}
    if stage:
        if stage not in PIPELINE_STAGES:
            conn.close()
            return f"Invalid stage '{stage}'. Valid: {', '.join(PIPELINE_STAGES)}"
        update_fields["pipeline_stage"] = stage
        changes.append(f"Stage -> {stage}")
    if priority:
        update_fields["priority"] = priority
        changes.append(f"Priority -> {priority}")
    if next_step:
        update_fields["next_step"] = next_step
        changes.append(f"Next step -> {next_step}")
    if thesis:
        update_fields["thesis"] = thesis
        changes.append("Thesis updated")
    if portfolio_notes:
        update_fields["portfolio_notes"] = portfolio_notes
        changes.append("Portfolio notes updated")
    if check_size_min > 0:
        update_fields["check_size_min"] = check_size_min
        changes.append(f"Check size min -> ${check_size_min:,.0f}")
    if check_size_max > 0:
        update_fields["check_size_max"] = check_size_max
        changes.append(f"Check size max -> ${check_size_max:,.0f}")
    if deck_version:
        update_fields["deck_version_sent"] = deck_version
        changes.append(f"Deck version -> {deck_version}")

    if update_fields:
        update_fields["updated_at"] = datetime.now().isoformat()
        sets = ", ".join(f"{k}=?" for k in update_fields)
        vals = list(update_fields.values()) + [investor_id]
        conn.execute(f"UPDATE investors SET {sets} WHERE id=?", vals)

    if interaction_date and interaction_summary:
        conn.execute(
            "INSERT INTO interactions (investor_id, interaction_date, interaction_type, summary, source) "
            "VALUES (?, ?, ?, ?, 'manual')",
            (investor_id, interaction_date, interaction_type or "note", interaction_summary),
        )
        changes.append(f"Logged {interaction_type or 'note'} on {interaction_date}: {interaction_summary}")

    if objection:
        conn.execute(
            "INSERT INTO objections (investor_id, objection, rebuttal, is_global) VALUES (?, ?, ?, 0)",
            (investor_id, objection, objection_rebuttal or ""),
        )
        changes.append(f"Objection logged: {objection}")

    conn.commit()
    conn.close()

    if not changes:
        return "No updates provided. Specify at least one field to update."

    return f"Updated **{actual_name}**:\n" + "\n".join(f"- {c}" for c in changes)


@mcp.tool()
def investor_analytics() -> str:
    """Get pipeline analytics: stage breakdown, committed count, follow-up compliance.

    Shows investors who haven't been contacted in 7+ days.
    """
    conn = _connect()
    stage_counts = {}
    for row in conn.execute(
        "SELECT pipeline_stage, COUNT(*) as cnt FROM investors GROUP BY pipeline_stage"
    ).fetchall():
        stage_counts[row["pipeline_stage"]] = row["cnt"]

    total = conn.execute("SELECT COUNT(*) as cnt FROM investors").fetchone()["cnt"]
    committed_count = stage_counts.get("committed", 0)

    stale = []
    active = conn.execute(
        "SELECT * FROM investors WHERE pipeline_stage NOT IN ('committed', 'passed')"
    ).fetchall()
    for inv in active:
        last = conn.execute(
            "SELECT MAX(interaction_date) as last_date FROM interactions WHERE investor_id=?",
            (inv["id"],),
        ).fetchone()
        if last and last["last_date"]:
            try:
                last_dt = datetime.strptime(last["last_date"], "%Y-%m-%d").date()
                days = (date.today() - last_dt).days
                if days >= 7:
                    stale.append({
                        "name": inv["name"], "days_since_contact": days,
                        "stage": inv["pipeline_stage"], "next_step": inv["next_step"],
                    })
            except ValueError:
                stale.append({
                    "name": inv["name"], "days_since_contact": None,
                    "stage": inv["pipeline_stage"], "next_step": inv["next_step"],
                })
        else:
            stale.append({
                "name": inv["name"], "days_since_contact": None,
                "stage": inv["pipeline_stage"], "next_step": inv["next_step"],
            })

    conn.close()

    lines = [f"# {COMPANY_NAME} Investor Pipeline Analytics", ""]
    lines.append(f"**Raise:** {RAISE_AMOUNT} {RAISE_DESCRIPTION}")
    lines.append(f"**Total investors:** {total} | **Committed:** {committed_count}")
    lines.append("")

    lines.append("## Pipeline Stages")
    for s in PIPELINE_STAGES:
        count = stage_counts.get(s, 0)
        bar = "\u2588" * count
        lines.append(f"- **{s}:** {count} {bar}")
    lines.append("")

    lines.append(f"## Follow-up Compliance ({len(stale)} need attention)")
    lines.append("")
    if stale:
        for s in sorted(stale, key=lambda x: x.get("days_since_contact") or 999, reverse=True):
            days = s["days_since_contact"]
            days_str = f"{days}d ago" if days else "never contacted"
            lines.append(f"- **{s['name']}** ({s['stage']}): last contact {days_str}")
            if s.get("next_step"):
                lines.append(f"  Next step: {s['next_step']}")
    else:
        lines.append("All active investors contacted within 7 days.")

    return "\n".join(lines)


@mcp.tool()
def investor_sync(crm_path: str = "") -> dict:
    """Sync investor data from vault CRM markdown files.

    Reads all .md files in the CRM folder where relationship: investor.
    Parses frontmatter for profile data and ## Timeline sections for interaction history.

    Args:
        crm_path: Override the CRM folder path. Leave empty to use config/env default.
    """
    path = Path(crm_path).expanduser() if crm_path else None
    return _sync_from_vault(path)


if __name__ == "__main__":
    mcp.run()
