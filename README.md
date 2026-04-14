# investor-relations-mcp

A FastMCP server for tracking your seed raise investor pipeline from Claude Code. Syncs from Obsidian vault CRM files, generates meeting prep documents, tracks interactions, and monitors follow-up compliance.

Built for founders raising a seed round who want to manage their pipeline conversationally without switching to a separate CRM.

## Tools

| Tool | What it does |
|------|-------------|
| `investor_search` | Search pipeline by name, stage, priority, or days since last contact |
| `investor_profile` | Full investor profile + complete interaction history |
| `investor_prep` | Meeting prep doc: portfolio fit, top 3 objections + rebuttals, stage-appropriate agenda |
| `investor_update` | Update stage, log interactions, add investor-specific objections |
| `investor_analytics` | Pipeline health: stage breakdown, committed count, follow-up compliance |
| `investor_sync` | Re-sync from vault CRM markdown files |

## Install

```bash
pip install fastmcp pyyaml python-frontmatter
```

## Setup

1. Clone:
   ```bash
   git clone https://github.com/adelaidasofia/investor-relations-mcp.git
   cd investor-relations-mcp
   ```

2. Fill in `pitch_config.yaml` with your company's pitch positioning and global objections. This is what drives the `investor_prep` tool.

3. Set environment variables:
   ```bash
   export INVESTOR_MCP_VAULT_CRM="~/vault/CRM/"
   ```

4. Register with Claude Code:
   ```bash
   claude mcp add investor-relations -s user -- python3 /path/to/investor-relations-mcp/server.py
   ```

5. Restart Claude Code, then sync your CRM:
   > "Run investor_sync"
   > "Show me my investor pipeline"
   > "Prep me for my meeting with [name]"

## CRM file format

The server reads Obsidian markdown files where `relationship: investor` in the frontmatter:

```markdown
---
relationship: investor
company: Acme Ventures
role: Partner
email: partner@acme.com
location: New York
priority: high
next_step: Follow up after pitch deck revision
---

Bio paragraph goes here.

## Timeline
- 2026-03-15 — Met at TechCrunch NYC event
- 2026-03-22 — Sent intro email with one-pager
- 2026-04-01 — Zoom call, strong interest in the model
```

## pitch_config.yaml

This file configures your pitch positioning and global objection rebuttals:

```yaml
company_name: "Your Company"
raise_amount: "$500K"
raise_description: "seed round"

pitch_positioning:
  market: "Your market description..."
  model: "How you make money..."
  traction: "Your best proof point..."
  ask: "Use of funds + runway..."
  never_say: "Framings to avoid..."

global_objections:
  - - "The market is too small"
    - "Your rebuttal..."
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `INVESTOR_MCP_DB` | `./investors.db` | SQLite database path |
| `INVESTOR_MCP_VAULT_CRM` | `~/vault/CRM/` | Folder with investor CRM files |

## Pipeline stages

`not_contacted` → `outreach_sent` → `response_received` → `meeting_scheduled` → `pitched` → `decision_pending` → `committed` / `passed`

Stages can be customized in `pitch_config.yaml`.

## License

MIT
