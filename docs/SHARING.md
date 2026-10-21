# Sharing projects and chats between Dens

Built as the module `modules/den_collab` (no restart needed). Tests: `tests/test_den_collab.py`.

## How it works (simple version, built)
- **The owner's Den does all the work.** Files stay on the owner's computer and tasks run there.
  The owner's computer has to be on. If it is off, guests see "Their Den is off" and can read what
  is already on their screen, but cannot send.
- A guest gets a private **invite link** (`http://their-mac:8791/#join=...`). The link holds a secret
  token. Only its hash is stored. "New link" in the Sharing page kills the old one at once, and
  "Remove" locks the person out at once.
- The guest opens the link in any browser (nothing to install), or pastes it into their own Den
  under **Sharing > Shared with me**, which lists everything shared with them and opens it.
- Guests only ever reach a small separate web server (port 8791). The Den's own API and the
  owner's login are never opened to them. They never see folder paths, run steps, keys or costs.

## What can be shared
- A **whole project**: its tasks and chats, notes, calendar and files (read, download).
- **One chat** on its own.

## Roles (per person, per project or chat)
| role | can do |
|---|---|
| viewer | read tasks, chats, notes, calendar, files |
| editor | also start tasks, ask questions, write in chats |
| admin | also invite and remove people on that project |

The owner can change a role with one click (the small role button on the person's card).

## Chatting at the same time
Everyone in a chat sees new messages within a second or two (long polling, no extra software),
sees who else is in the chat and who is typing. Each message shows who wrote it. A chat still runs
one message at a time on the owner's Den, like any chat.

## "Ask me before their tasks run"
Per person. Their new tasks are held (paused) until the owner presses **Run it** (or Decline) in the
Sharing page. Recommended for people you do not fully trust yet, because an editor's task runs
Claude Code on the owner's computer, in that project's folder.

## Turning it on
Sharing is **off by default** and opens no network port. In **Sharing**, press **Turn on**. This sets
the module's `serve` setting. Guests need to reach the owner's computer: same Wi-Fi works as it is;
from anywhere use Tailscale (free), with the guest joined to the owner's tailnet.
Traffic is plain HTTP, so on a shared Wi-Fi prefer Tailscale (it encrypts everything).

## Not built yet (planned, needs a middle server)
- **Take over / Take back**: when the owner is offline, another person's Den fetches the latest code
  from GitHub, runs the tasks, and pushes. When the owner is back they see a "while you were away"
  summary and press Take back. Needs a middle server so the shared data is still reachable while
  the owner's computer is off. The Code host setting belongs to this step.
- A guest's shared projects appearing inside their own Projects list (today they open from the
  Sharing page or the link).
