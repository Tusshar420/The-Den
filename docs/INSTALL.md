# Giving The Den to someone (Windows or Mac)

One Den per person. This is how you build the download, what your colleague does, and where things live.

## 1. Build the download (you, on your Mac)

```
cd /Users/work/TheDen
python3 installer/build.py
```

This writes `dist/TheDen-Windows.zip` and `dist/TheDen-Mac.zip` (about 2 MB each) and prints a short report:
how many files went in, and what was left out. Run it again any time; it replaces the two files.

- It packs every file git knows about, plus new files you haven't committed yet. So **commit or remove half-finished
  work first** (for example a module you are still building), or leave one out with
  `python3 installer/build.py --exclude 'modules/den_collab/'`.
- It leaves out your own data and keys (database, logins, workspace, vault, backups, tests, design files, scratch folders).
  The full list is `EXCLUDE` at the top of `installer/build.py`.
- It searches every file for keys and tokens (and for your real API key). If it finds one it stops and writes nothing.
- `--only windows` or `--only mac` builds just one.

Send the zip to your colleague (email, a shared drive, AirDrop). The installer is not code-signed (see "Known limits").

## 2. What your colleague does

**Windows**
1. Right-click the zip, "Extract All", open the folder.
2. Double-click `Install-The-Den.bat`. If Windows says "Windows protected your PC": click **More info**, then **Run anyway**.
3. Wait 5 to 10 minutes. It checks for Python 3.11+, Git for Windows and Claude Code and installs what is missing
   (through winget; Claude Code through its official installer). It asks two things: whether other computers may
   connect (say No unless you are sharing), and a name and password for signing in.
4. The Den opens in the browser. Later, use the **The Den** icon on the Desktop or in the Start menu.

**Mac**
1. Double-click the zip, open the folder.
2. Double-click `Install The Den.command`. If the Mac says it is from an unidentified developer: right-click the file,
   choose **Open**, then **Open** again.
3. Same questions as Windows. Python 3.10+ must already be there (python.org); the installer says so if not.

Running the installer again **updates** the program and keeps everything the person made. After that, updates normally
come from the Updates page inside The Den (see `docs/UPDATES.md`).

## 3. Where things live

| | Windows | Mac |
|---|---|---|
| The program and the person's data | `%LOCALAPPDATA%\TheDen\app` | `~/Applications/TheDen` |
| Logs | `%LOCALAPPDATA%\TheDen\logs` (`stdout.log`, `install.log`) | `~/Library/Logs/TheDen` (`den-stdout.log`, `den-stderr.log`) |
| Settings (API key, port, network) | `app\den.env` | the launchd file `~/Library/LaunchAgents/com.theden.app.plist` |
| Keeps it running | Scheduled Task "The Den" (starts at sign-in, hidden), running `den_service.py` | launchd job `com.theden.app` |
| Secret keys (vault key, tokens) | Windows Credential Manager (`keyring`), or the file `app\den-keys.json` if that isn't available | the login Keychain |

The Den listens only on `127.0.0.1` (this computer) unless the person chose "other computers may connect"
(`HOST=0.0.0.0`). The installer makes a random API key; it is never shown. The person signs in with the name and
password chosen during the install.

The Mac install uses its own name (`com.theden.app`), so it can never clash with your own `com.tushar.claudequeue`.

## 4. Uninstall

- Windows: double-click `Uninstall-The-Den.bat` (kept in `%LOCALAPPDATA%\TheDen`; also in the unzipped folder).
- Mac: double-click `Uninstall The Den.command` in the unzipped folder.

Both stop The Den, remove the background job and shortcuts and the program, and **ask** before deleting the person's
data. Python, Git and Claude Code are left alone.

## 5. How it works (short)

- `den_platform.py` is the one file that knows the operating system: how to stop a program and what it started,
  where logs go, where secrets are kept, how to find `claude`. On a Mac it does exactly what the code did before.
- `den_service.py` is the Windows replacement for launchd's KeepAlive: it runs `launcher.py`, and runs it again
  whenever it exits (The Den restarts by exiting). It stops only when a `.den-stop` file exists in the app folder.
- `launcher.py` is unchanged in purpose: it applies waiting updates, starts `main.py`, rolls back a bad update.
- `mcp-config.json` (the approval helper) names your own Python; on another computer `den_platform.mcp_config_path`
  writes `mcp-config.local.json` pointing at that install's own Python instead.

## Known limits

- **Pause / resume of a running task does not work on Windows.** Windows has no way to freeze a program and
  everything it started. Stopping a task works, and tasks that are still waiting can be paused. The dashboard says so.
- Windows needs **Git for Windows** and **Claude Code** (the installer sets both up). Claude Code on Windows runs its
  commands through Git's bash.
- The installers are **not signed**. Windows SmartScreen shows "Windows protected your PC" (More info > Run anyway);
  macOS Gatekeeper asks you to right-click > Open. Signing needs a paid certificate / Apple developer account.
- Windows may ask to allow network access the first time, if "other computers may connect" was chosen.
- Some optional features are Mac-only for now: the dev-server module (`modules/dev_server`, uses zsh and Unix process
  groups), Octopus voice (uses `say`), and the "brew install ..." hints on the System page.
- The Windows uninstaller expects the default install folder. For a custom one, run
  `powershell -ExecutionPolicy Bypass -File uninstall.ps1 -InstallDir <folder>`.
