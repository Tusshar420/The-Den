I want to extend the existing Octopus system with a **Personal Assistant layer**.

Do NOT rebuild the existing system. Inspect the current implementation first and integrate this into the existing Octopus architecture.

The purpose is to allow me to use Octopus as a personal secretary for everyday tasks, in addition to its existing coding capabilities.

The key principle is:

**I should talk to Octopus in natural language. Octopus should understand my intent, select the appropriate tool, perform the work, and report back.**

---

# 1. Personal Assistant Intent Router

Extend the existing Octopus router so it can recognize personal-assistant requests.

Examples:

"Check my calendar for tomorrow."

"Do I have anything scheduled at 3 PM?"

"Schedule a meeting with Rahul tomorrow at 3 PM for 30 minutes."

"Send Rahul an email saying I'll send the proposal tomorrow."

"Draft an email to the client explaining the delay."

"Find a free 30-minute slot with Rahul next week."

"Cancel my 4 PM meeting."

"Remind me tomorrow morning to call Rahul."

The router should determine:

* Intent
* Required tool
* Required permissions
* Whether approval is required
* Whether the request needs an AI model
* Whether the task can be executed automatically

---

# 2. Tool Architecture

Create a provider-independent tool architecture.

Personal tools should be treated as tools/capabilities rather than AI models.

Initial tool categories:

### Email

* Gmail
* Outlook/Microsoft 365

Capabilities:

* Read emails
* Search emails
* Get email details
* Create draft
* Send email
* Reply
* Forward

### Calendar

* Google Calendar
* Outlook Calendar

Capabilities:

* Read calendar
* Search events
* Check availability
* Create event
* Create meeting
* Modify event
* Cancel event

### Future tools

Design the architecture so these can be added later:

* Contacts
* Google Drive
* OneDrive
* Slack
* Teams
* Notes
* Reminders
* Salesforce
* GitHub
* Browser
* Web search

Do not implement all of these now.

---

# 3. OAuth

Use proper OAuth authentication.

Do NOT store Gmail/Outlook passwords.

The system should have a secure connection flow where I explicitly authorize Octopus to access the account.

Store credentials/tokens securely.

The UI should show:

Provider:
Google

Account:
[authorized account]

Status:
🟢 Connected

[Disconnect]

Do the same architecture for Microsoft/Outlook.

Do not assume that ChatGPT Plus, Claude Pro or Gemini Pro provides access to Gmail or Calendar.

These are separate integrations.

---

# 4. Email Capabilities

Implement Gmail and/or Outlook using their official APIs or an appropriate MCP integration.

Start with one provider if implementing both at once makes the architecture unnecessarily complicated.

For Gmail, support:

### Search

Example:

"Find the latest email from Rahul."

### Read

"Read Rahul's latest email."

### Draft

"Draft a response to Rahul saying I'll send the proposal tomorrow."

### Send

"Send Rahul an email saying I'll send the proposal tomorrow."

### Reply

"Reply to Rahul and tell him I'll check it today."

The system should preserve the original email context when replying.

---

# 5. Email Safety

Do NOT automatically send emails by default.

Recommended permissions:

Read email:
Allowed

Search email:
Allowed

Create draft:
Allowed

Send email:
Approval required

Reply:
Approval required

Forward:
Approval required

Delete:
Approval required

The approval UI should show:

EMAIL APPROVAL

To:
[rahul@example.com](mailto:rahul@example.com)

Subject:
Proposal update

Message:
I'll send the proposal tomorrow.

[Cancel] [Send]

Never send an email silently unless I explicitly configure that capability as trusted.

---

# 6. Calendar Capabilities

Support:

### Read

"What's on my calendar tomorrow?"

### Availability

"Am I free between 2 and 5 PM tomorrow?"

### Create event

"Block 3 PM tomorrow for project work."

### Meeting

"Schedule a 30-minute meeting with Rahul tomorrow at 3 PM."

### Modify

"Move my 3 PM meeting to 4 PM."

### Cancel

"Cancel my 3 PM meeting."

---

# 7. Calendar Safety

Recommended defaults:

Read calendar:
Allowed

Check availability:
Allowed

Create personal event:
Allowed

Create meeting with external attendees:
Approval required

Modify existing event:
Approval required

Cancel event:
Approval required

Send invitation:
Approval required

The approval screen should clearly show:

MEETING APPROVAL

Title:
Project discussion

Date:
September 25, 2026

Time:
3:00 PM – 3:30 PM

Attendees:
Rahul

Calendar:
Personal

Action:
Create meeting and send invitation

[Cancel] [Approve]

---

# 8. Smart Scheduling

Eventually Octopus should be able to understand requests such as:

"Find a free 30-minute slot with Rahul next week."

The workflow should be:

1. Identify Rahul.
2. Determine relevant calendars.
3. Check my availability.
4. Check attendee availability where permissions/API access allow it.
5. Find suitable slots.
6. Present the proposed slot.
7. Ask for approval before creating/sending the meeting.

Do NOT automatically create the meeting without approval.

---

# 9. Natural Conversation

Personal-assistant requests should support conversational context.

Example:

Me:
"What's on my calendar tomorrow?"

Octopus:
"You have three events..."

Me:
"Which one is at 3 PM?"

Octopus:
"Project review with Rahul."

Me:
"Move it to 4."

Octopus should understand that "it" refers to the 3 PM project review.

Then it should ask for approval before modifying the event.

---

# 10. Combining Tools

The important part is that Octopus should eventually be able to combine multiple tools.

Example:

"Find Rahul's latest email about the proposal and schedule a meeting with him next week."

Potential workflow:

Email
→ Search Rahul's emails

AI
→ Understand proposal context

Calendar
→ Find availability

Calendar
→ Prepare meeting

Approval
→ Ask me before sending invitation

Do not automatically perform external side effects without the appropriate permission.

---

# 11. AI vs Tool Separation

Do not use an AI model when a deterministic API call is enough.

For example:

"What's on my calendar tomorrow?"

Should use:

Calendar API → return events

rather than:

AI → somehow guess calendar information.

The AI should primarily handle:

* Understanding natural language
* Planning
* Deciding which tool to use
* Summarizing results
* Generating email content
* Resolving conversational context

The actual action should be performed by the appropriate tool/API.

---

# 12. Model Routing

Integrate this with the existing Octopus model router.

Examples:

"Check my calendar."
→ Calendar tool
→ No external AI model necessary unless natural-language interpretation is required.

"Draft a professional email to Rahul."
→ Email context/tool
→ Writing-capable AI model

"Analyze this email and tell me what I need to do."
→ Email tool
→ AI model

"Fix the Salesforce bug mentioned in this email."
→ Email tool
→ Coding Agent
→ Claude Code
→ Salesforce/project tools

This demonstrates the real purpose of the Octopus architecture:

**One request can cross multiple capabilities.**

---

# 13. Personal Assistant Dashboard

Add a new section:

PERSONAL

* Email
* Calendar
* Reminders
* Connections

For Email:

Show:

* Connected account
* Recent activity
* Pending approvals
* Drafts created by Octopus

For Calendar:

Show:

* Today's events
* Upcoming events
* Pending meeting approvals

Do not build a complete replacement for Gmail or Google Calendar.

The dashboard is primarily for Octopus activity and control.

---

# 14. Activity Log

Every external action should be logged.

Example:

10:30
Octopus searched Gmail

10:31
Found email from Rahul

10:31
Draft created

10:32
Approval requested

10:33
User approved

10:33
Email sent

The activity log should include:

* Timestamp
* Tool
* Action
* Target
* User approval status
* Result
* Error if any

Do not store unnecessary sensitive content in logs.

---

# 15. Permissions System

Expand the existing approval system into capability-based permissions.

For example:

EMAIL_READ
EMAIL_DRAFT
EMAIL_SEND
EMAIL_DELETE

CALENDAR_READ
CALENDAR_CREATE
CALENDAR_UPDATE
CALENDAR_DELETE
CALENDAR_INVITE

Each capability should have a policy such as:

ALLOW
ASK
DENY

Example:

EMAIL_READ → ALLOW

EMAIL_DRAFT → ALLOW

EMAIL_SEND → ASK

EMAIL_DELETE → DENY

CALENDAR_READ → ALLOW

CALENDAR_CREATE → ALLOW

CALENDAR_INVITE → ASK

This should eventually be configurable from Settings.

---

# 16. Security

Follow these rules:

* Use OAuth.
* Never store account passwords.
* Store tokens securely.
* Never expose OAuth tokens through the dashboard.
* Never put tokens in task descriptions.
* Never put tokens in logs.
* Use least-privilege scopes wherever practical.
* External side effects should require appropriate permissions.
* Destructive actions should require approval.
* The existing API key protection and Tailscale architecture should remain intact.

---

# 17. Cost Safety

This integration should not introduce mandatory paid services.

Use official APIs and free/standard account capabilities where available.

Do not automatically enable paid services.

Do not create paid resources.

Do not purchase anything.

Do not enable billing.

If an integration requires a paid service or billing configuration, show that clearly in the UI and stop until I explicitly configure it.

The system should distinguish:

FREE
PAID
NOT CONFIGURED
DISCONNECTED

---

# 18. Voice Integration

Personal Assistant tools must work through the same voice interface as the rest of Octopus.

Examples:

"Octopus, what's on my calendar tomorrow?"

"Octopus, email Rahul and tell him I'll call him later."

"Octopus, schedule a meeting with Rahul tomorrow at 3."

The flow remains:

VOICE
→ Speech-to-Text
→ Octopus
→ Intent Router
→ Tool
→ Approval if required
→ Execute
→ Result
→ Text-to-Speech

Voice must NOT have separate business logic from the normal dashboard/API interface.

---

# 19. Future Architecture

The long-term architecture should look like:

```
                     OCTOPUS
                        │
                Intent / Router
                        │
      ┌─────────────────┼─────────────────┐
      │                 │                 │
   Agents             Tools             Models
      │                 │                 │
 Coding Agent        Gmail            Claude
 Writing Agent       Calendar         Gemini
 Research Agent      GitHub           OpenAI
 Creative Agent      Salesforce       Future
                     Browser
                     Filesystem
                     Terminal
                        │
                     Approval
                        │
                     Execute
                        │
                     Report
```

The important distinction is:

**Models think. Agents specialize. Tools perform actions. Octopus orchestrates everything.**

---

# 20. V1.5 Success Criteria

Consider this feature complete when I can naturally tell Octopus:

"What's on my calendar tomorrow?"

"Find Rahul's latest email."

"Draft a reply to Rahul."

"Schedule a meeting with Rahul tomorrow at 3."

"Move my 3 PM meeting to 4 PM."

"Send Rahul the email."

"Read this email and tell me what I need to do."

And Octopus correctly:

1. Understands the request.
2. Selects the correct tool.
3. Uses the connected account.
4. Preserves conversation context.
5. Requests approval for external side effects.
6. Performs the action.
7. Records the action.
8. Reports the result.
9. Can perform the same workflow through text or voice.

Do not try to build the entire personal-secretary vision in this phase.

Build the underlying architecture correctly so additional tools can be added later without redesigning Octopus.
