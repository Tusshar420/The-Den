# Den versions and updates

Every Den has a version number (`VERSION`, like `2.1.0`) and a list of what each version brought
(`CHANGELOG.md`). The owner's Den publishes versions. A Den given to someone else stays on its
version until its person presses **Update**.

Code: `modules/den_updates/` (a module, reload with `POST /modules/den_updates/reload`) and
`launcher.py`. Tests: `tests/test_den_updates.py`.

## Two kinds of Den

| | the owner's Den ("source") | a given-away Den ("install") |
|---|---|---|
| what it is | a git checkout | an unpacked release, no `.git` |
| version changes when | the owner publishes | its person presses Update |
| started by | `main.py` (launchd) | `launcher.py` (launchd) |

## Publishing a version (owner)

Updates page › Releases › Publish. You give the number (`patch` for small fixes, `minor` for new
features, `major` for big changes), a title, and one line per change.

1. `VERSION` and `CHANGELOG.md` are updated. Only those two files are committed, and the commit is
   tagged `v<version>`.
2. The bundle is built from that tag with `git archive`. Anything not saved in git is left out, and
   so is everything in `.releaseignore` (tests, design files, plists, databases). `RELEASE.json`
   inside the bundle lists every file with its sha256.
3. The manifest (version, notes, commit, sha256, size) is signed with the owner's Ed25519 key. The
   key lives in the Keychain, account `den-release-signing-key`. **Keep a copy:** if it's lost,
   every given-away Den needs the new public key.
4. Everything is written to `releases/` (gitignored).

Publishing is refused when `launcher.py` or `modules/den_updates/` aren't committed, because a
release without them couldn't update itself again.

## Giving someone updates (owner)

Updates page › Releases › Add a Den. This makes an update key for that person, shown once. Their
Den needs three things:

- the address, e.g. `http://<owner-mac>:8790`
- the key
- the owner's public key

The release server listens on its own port (`serve_port`, default 8790) and only while at least one
key exists. It answers two questions, and only for a valid key:

- `GET /den-updates/v1/latest`
- `GET /den-updates/v1/bundle/<version>`

Each call records that Den's version and when it was last seen. After 20 wrong keys in 10 minutes
from one address, that address is locked out. In Tailscale's access rules, allow other people's
Macs to reach only port 8790 on the owner's Mac.

## Updating (given-away Den)

The Den looks for a new version every `check_hours` (default 6). It only *tells* its person; it
never updates by itself. When they press Update:

1. The new version's manifest is fetched and its signature checked with the owner's public key.
2. The bundle is downloaded. Its size and sha256 must match the signed manifest.
3. It is unpacked into `.den-update/staged/`. Only plain files with safe paths are allowed, never
   the person's own data. Every file is checked against `RELEASE.json`.
4. The Den asks for a normal restart, so running tasks finish first.
5. On the way back up, `launcher.py` does four things:
   - It copies everything it is about to replace or remove, plus a copy of `queue.db`, into
     `.den-update/backup/`.
   - It writes the new files and removes the ones the new version dropped.
   - It installs Python packages if `requirements.txt` changed.
   - It starts `main.py`.
6. The new version has 2 minutes to answer `/health`. If it doesn't, everything goes back:
   - the files, the database and the packages are restored;
   - launchd starts the old version again;
   - the result shows in the Updates page's history (`.den-update/history.json`).

Never touched by an update (`launcher.KEEP_NAMES` / `KEEP_DIRS`):

- `queue.db`, `accounts.json`, `login.json`, `api_keys.json`
- `.venv`, `workspace/`, `vault/`, `backups/`, `static/project-images/`, `.git`

## launchd on a given-away Den

The plist's `ProgramArguments` are `<den>/.venv/bin/python3` and `<den>/launcher.py`, with
`KeepAlive` on. Everything else is as in the README.
