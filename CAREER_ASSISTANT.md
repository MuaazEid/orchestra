# Career Assistant — personal add-on (do NOT push to the public GitHub repo)

This is a personal extension for job-search triage. It stays local — the
public Orchestra repo should remain a general-purpose orchestrator, not a
personal job-search tool, so don't include this file or `career_tools.py`
in that push.

## One-time setup

Save your real skills/background to the workspace once:

```powershell
$bg = @"
Senior AI Engineer, Riyadh. Python, LangChain, LangGraph, Prompt Engineering,
RAG, Local LLM Deployment (Ollama, LM Studio), FastAPI, SQL, Multi-Agent
Orchestration, ReAct, Whisper ASR, Arabic NLP, Dahua DSS, Dahua IVSS, Avigilon
VMS, NVR/NTP Synchronization, Access Control, Power BI, n8n, Flowise, Claude
Code, Linux, Git/GitHub, Computer Vision, Physical Security Systems
Integration, Government AI Projects, Data Quality Auditing, Excel Automation,
Bilingual Arabic/English.
"@
New-Item -ItemType Directory -Force "$env:USERPROFILE\.orchestra\workspace" | Out-Null
$bg | Out-File -Encoding utf8 "$env:USERPROFILE\.orchestra\workspace\background.txt"
```

Edit that text first if you want to add or remove anything — it's the only
thing the fit score is computed against.

## Using it

```
python -m orchestra chat
```

Then paste a job posting like:

```
Score this posting and draft a paragraph if it fits:
[paste the full job posting text here]
```

To keep a record:

```
Log this application: company SDAIA, role AI Engineer, fit STRONG 40%
```

Read the trail any time:

```
python -m orchestra ask "list files"
```
(or open `%USERPROFILE%\.orchestra\workspace\applications_log.md` directly)

## Job Scout — real web search (Tavily)

Adds a second capability: searching the live web for *current* postings,
not just scoring one you already have.

**One-time setup — never paste the key into chat, ever:**

```powershell
Add-Content "$env:USERPROFILE\Downloads\orchestra\.env" "`nORCHESTRA_TAVILY_API_KEY=your-key-here"
```

Get the key from your own Tavily dashboard (app.tavily.com → API Keys) and
paste it directly into that command in your own terminal. Free tier: 1,000
credits/month; each search here costs one credit, so ask for specific
searches rather than broad repeated ones.

**Using it:**

```
Find AI engineer jobs in Riyadh on Bayt
```

```
Find physical security systems engineer jobs in Riyadh
```

It returns titles, links, and snippets from a real, current web search —
you still open and read the actual posting yourself before applying.

## What this does NOT do

It does not browse job sites, fill forms, or submit anything on your behalf.
You paste the posting in, it scores and drafts, you review and apply
yourself. That's the deliberate boundary — see the chat for why.
