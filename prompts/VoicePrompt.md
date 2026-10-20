I want to evolve the existing `claude-queue` project into the first version of a personal AI assistant called **Octopus**.

You already have access to the existing implementation, so first inspect the current codebase and understand what is already implemented. **Do not rewrite or replace working functionality unnecessarily.** Build on top of the current queue, worker, approval, project, todo, notification and dashboard systems.

The goal of this phase is NOT to build the complete futuristic JARVIS system yet.

The goal is to create a solid **V1 foundation** where I can talk to Octopus naturally, give it tasks, and let it decide how those tasks should be handled.

---

# 1. Change the mental model

The current system is primarily:

User → Task Queue → Claude Code → Result

I want it to evolve into:

User → Octopus → Understand Request → Route → Execute → Validate → Report

Claude should become one of the workers/providers underneath Octopus rather than Octopus itself.

The existing queue should remain as the execution layer.

---

# 2. Command Center

Change the dashboard so the main/home page becomes an **Octopus Command Center**.

The primary interaction should be a large natural-language command box.

Example:

> Fix the authentication bug in KodeAura7.

or:

> Write me a 1500-word essay about AI in education.

or:

> Check why the contact form is failing, but don't change anything yet.

The user should NOT have to manually choose the project, model, agent or tools for normal requests.

Octopus should make those decisions.

There can be an "Advanced" section where I can manually override decisions.

The Command Center should also show:

* Current task
* Current project
* Current agent
* Current model
* Progress
* Tasks waiting in queue
* Approvals requiring attention
* Recent activity
* Provider availability

---

# 3. Intelligent Task Router

Introduce a routing/orchestration layer.

When a request arrives, Octopus should determine:

* What type of task is this?
* Is it coding?
* Writing?
* Research?
* Image generation?
* General question?
* Salesforce work?
* Project management?
* System/Mac operation?
* Does it require a project?
* Does it require files?
* Does it require terminal access?
* Does it require Git?
* Does it require MCP tools?
* Does it require web access?
* Does it require human approval?
* Which model/agent is appropriate?

Example:

"Fix the login bug in KodeAura7."

Should become approximately:

Type:
Coding

Project:
KodeAura7

Agent:
Coding Agent

Model:
Claude Code

Tools:
Filesystem
Git
Terminal

Reason:
Requires project files and code execution.

---

Another example:

"Write me an essay about AI in education."

Should become:

Type:
Writing

Agent:
Writing Agent

Model:
Configured general-purpose provider

Tools:
None

Reason:
No project or coding tools required.

---

Another:

"Generate a futuristic image of an AI assistant."

Should become:

Type:
Image Generation

Agent:
Creative Agent

Provider:
Configured image-generation provider

Tools:
Image generation

---

# 4. Model Provider Registry

Create a provider/model abstraction.

Do NOT hard-code the system around Claude.

The architecture should allow multiple providers such as:

* Claude / Anthropic
* OpenAI
* Gemini / Google
* Image-generation providers
* Future providers

Each provider/model should have configurable metadata such as:

* Provider
* Model
* Capabilities
* Coding capability
* Reasoning capability
* Vision capability
* Context capability
* Speed
* Relative cost
* Availability
* Enabled/disabled
* Rate-limit information

The routing system should select among enabled providers.

I should be able to add/remove/change providers without rewriting the router.

---

# 5. Important distinction for Claude Code

The current Claude Code worker is already working and uses my existing Claude setup.

Keep that functionality.

Do NOT automatically replace Claude Code with the Anthropic API.

Treat Claude Code as one available execution provider/agent.

For example:

Coding → Claude Code

General writing → another configured provider

Image generation → image provider

This allows me to preserve my Claude usage for tasks where it provides the most value.

---

# 6. Usage Optimization

The router should consider resource usage when choosing a provider.

The objective is not simply:

"Which model is strongest?"

It should consider:

* Capability
* Task requirements
* Quality
* Cost
* Speed
* Availability
* Current rate limits
* Context requirements
* Historical success

For example:

A simple writing task should not consume a limited Claude coding resource unnecessarily.

A complex coding task should still use Claude Code if that is the configured best option.

Log the routing decision so I can understand why a provider was selected.

---

# 7. Routing Decision UI

Before execution, show a compact decision preview.

Example:

OCTOPUS PLAN

Task:
Fix authentication bug

Project:
KodeAura7

Agent:
Coding Agent

Model:
Claude Code

Tools:
Filesystem
Git
Terminal

Reason:
This task requires project files, code changes and test execution.

Actions:

[Cancel] [Run]

For simple low-risk tasks, this can eventually be skipped through settings.

For higher-risk tasks, always show the plan.

---

# 8. Routing History

Add a Routing page.

Show:

* Original request
* Classification
* Selected project
* Selected agent
* Selected model
* Selected tools
* Reason for routing
* Execution result
* Duration
* Estimated usage/cost where available

Example:

"Fix KodeAura7 login bug"

→ Coding
→ KodeAura7
→ Claude Code
→ Filesystem + Git + Terminal

This is important because I want to understand and improve Octopus's routing decisions.

---

# 9. MCP / Tool Registry

Design a tool registry so Octopus can eventually work with multiple MCP servers.

Potential tools:

* Filesystem
* Git
* GitHub
* Terminal
* Browser
* Web/Search
* Salesforce
* Database
* Google Drive
* Gmail
* Calendar
* Slack
* Custom tools

Do not expose every tool to every task.

Octopus should select only the tools required.

Example:

Coding:

Filesystem
Git
Terminal
GitHub

Salesforce:

Salesforce
Filesystem
Git
Terminal

Email:

Gmail
Calendar

Research:

Web/Search
Browser

This should also become part of the permission/security system.

---

# 10. Agents

Introduce the concept of agents, but keep it simple initially.

Initial possible agents:

* Coding Agent
* Writing Agent
* Research Agent
* Creative/Image Agent
* General Assistant

The existing Claude worker should become the first Coding Agent.

An agent should conceptually contain:

* Instructions
* Preferred models
* Available tools
* Permissions
* Context requirements
* Execution policy

Do not create a huge multi-agent framework yet.

---

# 11. Voice Interface

Add voice as another input/output interface for Octopus.

The important architectural rule is:

**Voice should NOT create a separate assistant.**

It should feed into the exact same Octopus pipeline.

Architecture:

Voice
→ Speech-to-Text
→ Octopus
→ Router
→ Agent
→ Tools
→ Result
→ Text-to-Speech
→ Voice response

The same request should be possible through:

* Dashboard text
* Voice
* Future iOS Shortcut
* API

---

# 12. Start Voice With Local/Low-Cost Providers

Do not require a paid voice API for the initial implementation if avoidable.

Design STT and TTS as provider abstractions.

Initial possible implementation:

STT:
Local Whisper or another local speech-to-text solution.

TTS:
macOS native speech synthesis.

The system should later allow cloud providers to be plugged in without changing the Octopus architecture.

Do not make the architecture dependent on Whisper or macOS TTS either.

---

# 13. Voice Conversation

Voice should support conversational context.

Example:

Me:
"Check KodeAura7."

Octopus:
"I found three issues."

Me:
"Fix the second one."

Octopus should understand what "the second one" refers to.

Then:

Me:
"Run the tests afterward."

Octopus should understand that this belongs to the same conversation/task.

The existing task conversation system should be reused where possible.

---

# 14. Voice Commands

Support natural commands such as:

"Octopus, check the KodeAura7 project."

"Octopus, fix the login bug."

"Octopus, stop the current task."

"Octopus, what's happening?"

"Octopus, what are you currently working on?"

"Octopus, run the tests."

"Octopus, continue the previous task."

"Octopus, cancel that."

The user should not have to use a rigid command syntax.

---

# 15. Autonomy Levels

Introduce an autonomy setting.

### Assist

Octopus investigates and proposes actions.

### Execute

Octopus can perform normal actions automatically but requires approval for risky operations.

### Autonomous

Octopus can execute broader tasks automatically within configured boundaries.

Regardless of mode, dangerous operations such as production deployments, destructive operations or other explicitly protected actions should remain behind approval unless I explicitly configure otherwise.

---

# 16. Existing Approval System

Keep the existing approval system.

Expand it into a capability-based permission system over time.

Examples:

Low risk:

* Read files
* Analyze code
* Edit files
* Run tests

Higher risk:

* Git push
* Deploy
* Send email
* Modify production
* Destructive commands

The UI should clearly explain what Octopus wants to do and why.

---

# 17. Task Detail UI

Improve the task page so I can see:

* Task
* Project
* Agent
* Model
* Tools
* Current status
* Plan
* Current step
* Activity
* Files changed
* Git branch
* Git diff
* Test results
* Approvals
* Final result

Example:

TASK

Fix authentication

STATUS
Running

PROJECT
KodeAura7

AGENT
Coding Agent

MODEL
Claude Code

PLAN

✓ Inspect authentication
✓ Identify problem
→ Modify implementation
○ Run tests
○ Review changes

LIVE ACTIVITY

Reading authentication middleware...

---

# 18. Activity Timeline

Add an Activity section showing what Octopus actually did.

Example:

10:42
Received request

10:42
Classified as coding task

10:42
Selected KodeAura7

10:42
Selected Claude Code

10:43
Started investigation

10:45
Modified auth middleware

10:46
Tests passed

10:47
Approval requested

This will eventually become part of Octopus's history/memory.

---

# 19. Keep Existing Features

Do NOT remove the existing:

* Queue
* Tasks
* Projects
* Todos
* Approvals
* Notifications
* Rate-limit handling
* Git-based workflow
* Tailscale access
* SQLite database
* launchd service
* Mobile dashboard

These are the foundation.

The new architecture should sit above them.

---

# 20. UI Navigation

Change the main navigation to approximately:

JARVIS / OCTOPUS

Home / Command Center
Tasks
Projects
Approvals
AI Providers
Routing
Activity
Settings

The Command Center should be the default landing page.

The queue should remain accessible but should not dominate the UI.

The user should feel like they are talking to an assistant rather than managing a background worker.

---

# 21. Important V1 Scope

Do NOT attempt to build the entire long-term JARVIS vision now.

V1 should prove these things:

1. I can give Octopus a natural-language request.
2. Octopus can classify the request.
3. Octopus can select an appropriate provider/agent.
4. Octopus can select appropriate tools.
5. Coding requests can continue using the existing Claude Code worker.
6. General requests can be routed to another configured provider.
7. Voice can submit the same requests as text.
8. Octopus can respond through text and voice.
9. I can see why Octopus made a routing decision.
10. Existing approvals and queue execution continue working.
11. I can override Octopus's model/provider decision.
12. Usage and provider information can be monitored.

Do not build complex autonomous planning, advanced memory, dozens of agents, or complicated multi-agent workflows yet.

The goal is to create the foundation correctly.

---

# Final architectural goal

The long-term architecture should become:

USER
↓
VOICE / DASHBOARD / API
↓
OCTOPUS
↓
UNDERSTAND REQUEST
↓
PLAN IF NECESSARY
↓
SELECT AGENT
↓
SELECT MODEL
↓
SELECT TOOLS / MCP SERVERS
↓
CHECK PERMISSIONS
↓
EXECUTE THROUGH EXISTING QUEUE
↓
VALIDATE
↓
REPORT RESULT
↓
TEXT / VOICE RESPONSE

The important principle is:

**I should talk to Octopus, not directly to Claude, Gemini or OpenAI.**

Those should become interchangeable workers/providers underneath Octopus.

Before modifying anything, inspect the existing implementation and identify which existing components can be reused. Then implement this incrementally, keeping the current system working throughout the upgrade.
