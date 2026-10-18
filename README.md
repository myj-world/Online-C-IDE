---
title: C Online IDE
emoji: 💻
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# C Online IDE

A browser-based C IDE with a VS Code–style editor (Monaco), live multi-user editing, and a Run button that compiles and executes code with `gcc`. Built with Flask + Socket.IO, deployable as a Hugging Face Space (Docker SDK).

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
| `Dockerfile` | Installs `gcc`, runs as UID 1000, serves on port 7860 |
| `README.md` | This file; the YAML block at the top configures the Space |

## Deploy on Hugging Face Spaces
1. Create a Space at https://huggingface.co/new-space: **SDK: Docker**, template **Blank**, hardware **CPU basic** (free).
2. In the Space, open **Settings → Variables and secrets** and add two **secrets**:
   - `DATABASE_URL`: your Neon connection string (with `?sslmode=require`)
   - `SECRET_KEY`: any long random string
3. Upload `app.py`, `requirements.txt`, `Dockerfile`, `README.md` and `.gitignore` (Files tab → Add file), or push with git:
   ```bash
   git clone https://huggingface.co/spaces/<user>/<space-name>
   # copy the project files in, then:
   git add . && git commit -m "Add C IDE" && git push
   ```
   Use a Hugging Face access token with write permission as the git password.
4. The Space builds automatically. Check the **Logs** for `Storage: Postgres`.
5. Open the direct app URL `https://<user>-<space-name>.hf.space` for the best experience (browser storage and the Copy link button work most reliably outside the Hugging Face page frame).

Notes:
- Free Spaces sleep after a period of inactivity and wake on the next visit. The disk is ephemeral, which is why rooms live in Postgres.
- Keep the Space on a single instance; live room state is held in memory.
- A public Space shows your code to everyone but never your secrets. Make it private if you prefer.

## Run locally
Requires Python 3.9+ and `gcc` (Windows: `winget install BrechtSanders.WinLibs.POSIX.UCRT`, then reopen the terminal).

```bash
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000. To use Postgres locally, set `DATABASE_URL` first (PowerShell: `$env:DATABASE_URL="postgresql://..."`). Without it, rooms are saved to `rooms.txt`.

## Database
The app creates its tables on startup. To create them manually:

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

## Using it
- **New session** creates a room and shares your current files into it.
- **Join** takes a room code. **Copy link** shares the room URL (`/?room=CODE`).
- **Sessions** lists your history; click one to switch, or choose Local workspace to work offline.
- Shortcuts: `Ctrl+Enter` run, `Ctrl+S` save.

## Security note
`/run` compiles and executes code sent by visitors. It uses a non-root user, a 5-second time limit and a memory cap, but this is not a full sandbox. Before exposing it publicly, add rate limiting or a proper sandbox (e.g. nsjail), or keep the Space private. Anyone with a room code can edit that room.
