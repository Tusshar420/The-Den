// Data shapes the UI expects. JSDoc so it works in the existing no-build dashboard
// and still gives editor type hints. Field names match the FastAPI/SQLite backend.

/**
 * @typedef {'queued'|'running'|'done'|'error'|'needs_review'|'rate_limited'|'cancelled'} TaskStatus
 * @typedef {'agent'|'api'} Lane                 agent = Claude Code (acts on project), api = answer-only model
 * @typedef {'coding'|'general'|'simple'|'manual'} RouteCategory
 */

/**
 * @typedef {Object} Project           GET /projects
 * @property {number}  id
 * @property {string}  name
 * @property {string}  path             absolute repo path ("" for General)
 * @property {0|1}     active           0 = paused (queued tasks are skipped, kept)
 * @property {string}  created_at
 * @property {boolean} [is_general]     NEW · built-in "General" project for requests with no project; pin first
 * @property {string}  [color]          UI-only; assign from --p1…--p5 round-robin if the backend doesn't store it
 */

/**
 * @typedef {Object} Task              GET /tasks, GET /projects/{id}/tasks
 * @property {number}      id           shown as "#123"
 * @property {number}      project_id
 * @property {string}      prompt       card title (truncate with ellipsis)
 * @property {number}      priority     higher runs sooner; UI: 1 Urgent, 0 Normal, -1 Low
 * @property {TaskStatus}  status
 * @property {string|null} result_summary   Octopus / model reply text (plain text, keep line breaks)
 * @property {string|null} error_message
 * @property {string}      created_at
 * @property {string|null} started_at
 * @property {string|null} finished_at
 * @property {string|null} resume_at        rate-limit resume time → countdown
 * @property {number|null} thread_id        replies carry the root id; list shows roots only
 * @property {string|null} session_id
 * NEW (Octopus routing):
 * @property {Lane}          lane
 * @property {string}        model_id       e.g. "claude-code", "gpt-6-luna"
 * @property {RouteCategory} category
 * @property {string}        route_reason   short sentence shown as "Routed: …" under the first reply
 * @property {number|null}   duration_ms    hide if null
 * @property {number|null}   input_tokens
 * @property {number|null}   output_tokens
 * @property {number|null}   cost_usd       hide if null; "<$0.01" when tiny
 */

/**
 * @typedef {Object} Model             GET /octopus/models
 * @property {string}      id
 * @property {string}      label
 * @property {Lane}        lane
 * @property {boolean}     enabled
 * @property {boolean}     available
 * @property {string|null} unavailable_reason   e.g. "OPENAI_API_KEY is not set", "disabled in models.json"
 * @property {string|null} rate_limited_until   ISO time or null
 */

/**
 * @typedef {Object} ModelUsage        GET /octopus/usage  (last 30 days, keyed by model id)
 * @property {number} tasks
 * @property {number} done
 * @property {number} failed
 * @property {number} avg_duration_ms
 * @property {number} cost_usd
 */

/**
 * @typedef {Object} SendRequest       POST /octopus
 * @property {string}      text
 * @property {number|null} project_id   null → General
 * @property {'auto'|string} model      "auto" or a model id
 * @property {string[]}    [images]     NEW (optional) · uploaded image ids/urls, see README › Backend gaps
 *
 * @typedef {Object} SendResponse
 * @property {Task} task
 * @property {{ model_id: string, model_label: string, lane: Lane, category: RouteCategory, reason: string }} decision
 * Errors: HTTP 4xx/5xx with { detail: "No available model for 'general'…" } → show "Could not send: " + detail
 */

/**
 * @typedef {Object} Approval          GET /approvals
 * @property {number} id
 * @property {number} task_id
 * @property {string} tool_name
 * @property {string} tool_input       JSON; for Bash → { command, description? }
 * @property {'pending'|'approved'|'denied'|'timed_out'} status
 * @property {string} created_at
 */

/**
 * @typedef {Object} Todo              GET /projects/{id}/todos  (UI name: Backlog)
 * @property {number}  id
 * @property {number}  project_id
 * @property {string}  text
 * @property {number}  position
 * @property {0|1}     checked         UI: selected for "Run selected"
 * @property {string}  status          pending | queued | done | … (mirrors linked task)
 * @property {number|null} task_id
 */

/**
 * @typedef {Object} QueueStatus       GET /queue/status
 * @property {Record<TaskStatus, number>} counts   → Home rollups
 * @property {Task|null} running                    legacy single running; use GET /tasks?status=running for the list
 * @property {Task|null} next_up
 */

export {};
