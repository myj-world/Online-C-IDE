# C Online IDE

A browser-based C IDE with a VS Code–style editor (Monaco), live multi-user editing, and a Run button that compiles and executes code with `gcc`.

## Features
- Syntax highlighting, autocomplete, snippets, bracket colouring, minimap
- Live collaboration: share a room link, any number of users, join or leave anytime
- Rooms are stored permanently in Postgres (e.g. Neon), so they work even when nobody is online
- Files and session history are saved in your browser; reloading resumes your last session
- Multi-file projects (`.c` and `.h`), stdin input, compile errors shown in the output panel

## Files
| File | Purpose |
|---|---|
| `app.py` | Flask + Socket.IO server and the whole frontend |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Installs `gcc` and starts the app (used by Render) |

## Run locally
Requires Python 3.9+ and `gcc` (Windows: `winget install BrechtSanders.WinLibs.POSIX.UCRT`, then reopen the terminal).

```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000.

To use Postgres locally, set `DATABASE_URL` first (PowerShell: `$env:DATABASE_URL="postgresql://..."`). Without it, rooms are saved to `rooms.txt`.

## Database
The app creates its tables on startup. To create them manually, run this in your Postgres SQL editor:

```sql
CREATE TABLE IF NOT EXISTS rooms (
  code TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS room_files (
  room TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
  name TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (room, name)
);
```

## Deploy on Render
1. Push this folder to GitHub.
2. Render → **New → Web Service** → pick the repo, set **Language: Docker**.
3. Add environment variables: `DATABASE_URL` (Neon connection string) and `SECRET_KEY` (any long random string).
4. Create the service. Keep it at **1 instance**, since live room state is held in memory.

## Using it
- **New session** creates a room and shares your current files into it.
- **Join** takes a room code. **Copy link** shares the room URL (`/?room=CODE`).
- **Sessions** lists your history; click one to switch, or choose Local workspace to work offline.
- Shortcuts: `Ctrl+Enter` run, `Ctrl+S` save.

## Security note
`/run` compiles and executes code sent by visitors. It uses a non-root user, a 5-second time limit and a memory cap, but this is not a full sandbox. Before exposing it publicly, add rate limiting or a proper sandbox (e.g. nsjail), or keep room links private. Anyone with a room code can edit that room.
