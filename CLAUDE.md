# Agent Instructions
You’re inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concern so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
-	Markdown SOP (Standard Operating Procedure) stored in ‘projects/<project-name>/workflows/’
-	Each workflow defines the objective, required inputs, which tools to use, expected outputs and how to handle edge cases
-	Written in plain language, the same way you’d brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
-	This is your role. You’re responsible for intelligent coordination.
-	Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
-	You connect intend to execution without trying to do everything yourself
-	Example: If you need to pull data from a website, don’t attempt it directly. Read ‘projects/<project-name>/workflows/scrape_website.md’, figure out the required inputs then execute ‘tools/scrape_single_site.py’

**Layer 3: Tools (The Execution)**
-	Python scripts in ‘tools/’ that do the actual work
-	API calls, data transformations, file operations, database queries
-	Credentials and API keys are stored in ‘.env’
-	These scripts are consistent testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you’re down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate
**1. Look for existing tools first**
Before building anything new,  check ‘tools/’ based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
-	Read the full error message and trace
-	Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
-	Document what you learned in the workflow (rat limits, timing quirks, unexpected behavior)
-	Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don’t create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## The Self-Improvement Loop
Every failure is a change to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system
This look is how the framework improves over time.

## File Structure
**What goes where:**
-	**Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
-	**Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
‘’’
.env                            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json    # Google OAuth (gitignored)
tools/                          # Shared Python scripts for deterministic execution (reusable across all projects)
projects/                       # One subfolder per project
    <project-name>/
        CLAUDE.md               # Project goal, inputs, outputs, workflow links, gotchas (auto-loaded by Claude)
        data/                   # Input files for this project (Excel, CSV, etc.)
        workflows/              # Markdown SOPs specific to this project
        .tmp/                   # Temporary/intermediate outputs. Regenerated as needed.
‘’’

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in ‘.tmp/’ is disposable.

## Safety Protocol: Filters & Data Mutations
1. **Test both directions**: When writing a new filter, test it against both positive cases (should reject) AND negative cases (should pass through). A filter that only catches junk but wasn't checked against legitimate edge cases is incomplete.
2. **Never mutate on fresh code**: Do not run destructive or mutating operations (`--force-all`, `--fix`, bulk updates) on freshly written logic without first verifying edge cases. Run in audit/dry-run mode first.
3. **Check boundary values**: When a filter uses a numeric threshold (like character length), enumerate known values that sit near the boundary before committing. If the cutoff is ≤3 characters, list every 3-character value you know about and confirm they're handled correctly.

## Bottom Line
You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.
