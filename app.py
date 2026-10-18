import os, re, json, time, atexit, shutil, subprocess, tempfile, threading
try:
    import resource  # Linux/macOS only; skipped on Windows
except ImportError:
    resource = None
IS_WIN = os.name == "nt"
from flask import Flask, request, jsonify, Response
from flask_socketio import SocketIO, join_room, leave_room, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")
sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

ROOMS_FILE = os.environ.get("ROOMS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rooms.txt"))
DATABASE_URL = os.environ.get("DATABASE_URL")   # Neon/Postgres; if unset, falls back to rooms.txt
CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
ROOMS, SID_ROOM = {}, {}        # in-memory cache: code -> {"files": {...}, "users": {sid: name}}
RL = threading.RLock()          # guards ROOMS / SID_ROOM
DIRTY, _dl = set(), threading.Lock()   # rooms waiting to be written by the background thread

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS rooms (
         code TEXT PRIMARY KEY,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS room_files (
         room TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
         name TEXT NOT NULL,
         content TEXT NOT NULL DEFAULT '',
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         PRIMARY KEY (room, name))""",
]
pool, _FILE_DB = None, {}
if DATABASE_URL:
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=4, timeout=15, open=True,
                          kwargs={"autocommit": True}, check=ConnectionPool.check_connection)
    with pool.connection() as c:
        for q in SCHEMA:
            c.execute(q)
    print("Storage: Postgres")
else:
    try:
        with open(ROOMS_FILE, encoding="utf-8") as f:
            _FILE_DB = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    print("Storage: local file", ROOMS_FILE)


def store_load(code):
    """Return {name: content} for a saved room, or None if it doesn't exist."""
    if pool:
        with pool.connection() as c:
            if not c.execute("SELECT 1 FROM rooms WHERE code=%s", (code,)).fetchone():
                return None
            return dict(c.execute("SELECT name, content FROM room_files WHERE room=%s", (code,)).fetchall())
    return _FILE_DB.get(code)


def store_save(code, files):
    if pool:
        with pool.connection() as c, c.transaction():
            c.execute("INSERT INTO rooms(code) VALUES (%s) ON CONFLICT (code) DO UPDATE SET updated_at=now()", (code,))
            c.execute("DELETE FROM room_files WHERE room=%s AND NOT (name = ANY(%s))", (code, list(files)))
            for n, t in files.items():
                c.execute("""INSERT INTO room_files(room, name, content) VALUES (%s,%s,%s)
                             ON CONFLICT (room, name) DO UPDATE SET content=EXCLUDED.content, updated_at=now()""",
                          (code, n, t))
    else:
        _FILE_DB[code] = files
        with open(ROOMS_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(_FILE_DB, f)
        os.replace(ROOMS_FILE + ".tmp", ROOMS_FILE)


def mark(code):
    with _dl:
        DIRTY.add(code)


def flush(codes):
    for code in codes:
        with RL:
            r = ROOMS.get(code)
            files = dict(r["files"]) if r else None
        if files is None:
            continue
        try:
            store_save(code, files)
        except Exception as e:
            print("save failed:", e)
            mark(code)
            continue
        with RL:  # free memory for empty rooms once safely stored
            r = ROOMS.get(code)
            if r and not r["users"] and code not in DIRTY:
                ROOMS.pop(code, None)


def writer():  # asynchronous persistence: socket handlers never wait on the database
    while True:
        time.sleep(1)
        with _dl:
            batch = list(DIRTY)
            DIRTY.clear()
        if batch:
            flush(batch)


threading.Thread(target=writer, daemon=True).start()
atexit.register(lambda: flush(list(ROOMS)))


def fname(n):
    return os.path.basename(str(n or ""))[:100]


def clean(files):
    out = {}
    if isinstance(files, dict):
        for n, c in list(files.items())[:50]:
            if fname(n) and isinstance(c, str):
                out[fname(n)] = c[:200_000]
    return out


# ---------- Collaboration (Socket.IO) ----------
@sio.on("join")
def on_join(d):
    room = str((d or {}).get("room", ""))
    if not CODE_RE.match(room):
        return emit("err", "Room code must be 3-40 letters, numbers, - or _.")
    name = str(d.get("name") or "Guest")[:24]
    with RL:
        r = ROOMS.get(room)
        if r is None:
            try:
                saved = store_load(room)
            except Exception as e:
                print("load failed:", e)
                return emit("err", "Database unavailable, try again in a moment.")
            r = ROOMS[room] = {"files": saved if saved is not None else clean(d.get("files")), "users": {}}
            if saved is None:
                mark(room)  # brand-new room: persist it
        r["users"][request.sid] = name
        SID_ROOM[request.sid] = room
        join_room(room)
        files, users = dict(r["files"]), list(r["users"].values())
    emit("state", {"files": files})
    emit("users", users, to=room)


@sio.on("edit")
def on_edit(d):
    emit("edit", d, to=SID_ROOM.get(request.sid), include_self=False)


@sio.on("snap")  # debounced full-content snapshot
def on_snap(d):
    room = SID_ROOM.get(request.sid)
    n, c = fname(d.get("file")), d.get("content")
    with RL:
        r = ROOMS.get(room)
        if r and n and isinstance(c, str):
            r["files"][n] = c[:200_000]
    if r:
        mark(room)


@sio.on("fileop")
def on_fileop(d):
    room = SID_ROOM.get(request.sid)
    op, n, new = d.get("op"), fname(d.get("name")), fname(d.get("new"))
    with RL:
        r = ROOMS.get(room)
        if not r or not n:
            return
        f = r["files"]
        if op == "create":
            f.setdefault(n, "")
        elif op == "delete":
            f.pop(n, None)
        elif op == "rename" and n in f and new:
            f[new] = f.pop(n)
        else:
            return
    mark(room)
    emit("fileop", {"op": op, "name": n, "new": new}, to=room, include_self=False)


@sio.on("disconnect")
def on_disconnect():
    with RL:
        room = SID_ROOM.pop(request.sid, None)
        r = ROOMS.get(room)
        if r:
            r["users"].pop(request.sid, None)
            users = list(r["users"].values())
    if r:
        emit("users", users, to=room)
        mark(room)  # writer saves it, then evicts it from memory if empty


# ---------- Compile & run ----------
def limits():
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (256 * 2**20, 256 * 2**20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2**20, 2**20))
    os.setsid()


@app.post("/run")
def run():
    d = request.get_json(force=True)
    tmp = tempfile.mkdtemp()
    try:
        srcs = []
        for n, c in (d.get("files") or {}).items():
            n = os.path.basename(n)
            if n.endswith((".c", ".h")) and len(c) < 200_000:
                with open(os.path.join(tmp, n), "w") as fh:
                    fh.write(c)
                if n.endswith(".c"):
                    srcs.append(n)
        if not srcs:
            return jsonify(out="No .c files to compile.", ok=False)
        EXE = "prog.exe" if IS_WIN else "prog"
        CC = next((c for c in ("gcc", "cc", "clang", "tcc") if shutil.which(c)), None)
        if not CC:
            return jsonify(ok=False, out="No C compiler found on the server PATH.\n"
                "Install gcc (Windows: winget install BrechtSanders.WinLibs.POSIX.UCRT), "
                "then restart the terminal and the server.")
        cc = subprocess.run([CC, "-Wall", "-O1", "-o", EXE, *srcs, "-lm"],
                            cwd=tmp, capture_output=True, text=True, timeout=20,
                            encoding="utf-8", errors="replace")
        if cc.returncode:
            return jsonify(out=cc.stderr[:20000], ok=False)
        try:
            p = subprocess.run([os.path.join(tmp, EXE)], cwd=tmp, input=(d.get("stdin") or "")[:20000],
                               capture_output=True, text=True, timeout=5,
                               preexec_fn=None if IS_WIN else limits, encoding="utf-8", errors="replace")
            out = (cc.stderr + p.stdout + p.stderr)[:20000]
            out += f"\n[exited with code {p.returncode}]"
        except subprocess.TimeoutExpired:
            out = "[timed out after 5s]"
        return jsonify(out=out, ok=True)
    except Exception as e:
        return jsonify(out=f"Server error: {e}", ok=False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>C IDE</title>
<script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs/loader.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
<style>
:root{--bg:#1e1e1e;--side:#252526;--bar:#333;--tab:#2d2d2d;--fg:#ccc;--ac:#007acc;--bd:#3c3c3c}
*{box-sizing:border-box}html,body{height:100%;margin:0;background:var(--bg);color:var(--fg);font:13px "Segoe UI",system-ui,sans-serif}
#app{display:flex;height:calc(100% - 22px)}
#side{width:250px;background:var(--side);display:flex;flex-direction:column;border-right:1px solid var(--bd);overflow:auto}
.h{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;font-size:11px;letter-spacing:.04em}
button{background:var(--ac);color:#fff;border:0;border-radius:2px;padding:4px 10px;cursor:pointer;font:inherit}
button.g{background:none;color:var(--fg);padding:2px 6px}button:hover{filter:brightness(1.2)}
.f{padding:3px 12px;cursor:pointer;display:flex;justify-content:space-between}.f:hover{background:#2a2d2e}.f.on{background:#37373d}
.f span.x{visibility:hidden}.f:hover span.x{visibility:visible}
input,textarea{background:#3c3c3c;border:1px solid var(--bd);color:#fff;padding:4px 6px;font:inherit;width:100%;margin-bottom:6px}
#collab{padding:0 10px 10px}#users div::before{content:"● ";color:#4ec9b0}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#top{display:flex;background:var(--bar);align-items:center}#tabs{flex:1;display:flex;overflow-x:auto}
.t{padding:8px 14px;background:var(--tab);border-right:1px solid var(--bg);cursor:pointer;white-space:nowrap}
.t.on{background:var(--bg);border-top:1px solid var(--ac);color:#fff}
#run{margin:0 8px;background:#2ea043}
#ed{flex:1;min-height:0}#panel{height:200px;border-top:1px solid var(--bd);display:flex;flex-direction:column}
#ptabs{display:flex;gap:14px;padding:6px 12px}#ptabs b{cursor:pointer;font-weight:400;opacity:.6}#ptabs b.on{opacity:1;border-bottom:1px solid var(--ac)}
#out{flex:1;margin:0;padding:6px 12px;overflow:auto;font:12px Consolas,monospace;white-space:pre-wrap}
#stdin{flex:1;display:none;margin:0;resize:none;font-family:Consolas,monospace}
#status{height:22px;background:var(--ac);color:#fff;display:flex;gap:18px;align-items:center;padding:0 10px;font-size:12px}
@media(max-width:700px){#side{width:150px}}
</style></head><body>
<div id="app">
 <div id="side">
  <div class="h"><span>EXPLORER</span><span><button class="g" onclick="newFile()" title="New file">＋</button></span></div>
  <div id="files"></div>
  <div class="h"><span>LIVE SHARE</span></div>
  <div id="collab">
   <input id="name" placeholder="Your name">
   <input id="room" placeholder="Room code">
   <button onclick="joinCode()">Join</button>
   <button onclick="newSession()">New session</button>
   <button class="g" onclick="copyLink()">Copy link</button>
   <div id="users" style="margin-top:8px"></div>
  </div>
  <div class="h"><span>SESSIONS</span></div>
  <div id="sess"></div>
 </div>
 <div id="main">
  <div id="top"><div id="tabs"></div><button id="run" onclick="run()">▶ Run (Ctrl+Enter)</button></div>
  <div id="ed"></div>
  <div id="panel"><div id="ptabs"><b class="on" onclick="ptab(0)">OUTPUT</b><b onclick="ptab(1)">INPUT (stdin)</b></div>
   <pre id="out">Press Run to compile and execute.</pre><textarea id="stdin" placeholder="Program input…"></textarea></div>
 </div>
</div>
<div id="status"><span id="pos">Ln 1, Col 1</span><span>C</span><span id="rs">Offline · files saved in this browser</span></div>
<script>
const $=id=>document.getElementById(id);
const DEF={"main.c":'#include <stdio.h>\n\nint main(void) {\n    printf("Hello, World!\\n");\n    return 0;\n}\n'};
const ls=(k,v)=>{try{return v===undefined?JSON.parse(localStorage.getItem(k)):localStorage.setItem(k,JSON.stringify(v))}catch(e){return null}};
const fkey=c=>c=="local"?"cide.files":"cide.files."+c;
let ws="local", files={}, open=[], cur=null, models={}, ed, sock=null, remote=false, snapT={};
const save=()=>ls(fkey(ws),files);

function ptab(i){document.querySelectorAll("#ptabs b").forEach((b,j)=>b.classList.toggle("on",i==j));$("out").style.display=i?"none":"block";$("stdin").style.display=i?"block":"none"}
function lang(n){return "c"}
function model(n){
  if(models[n])return models[n];
  const m=monaco.editor.createModel(files[n],lang(n),monaco.Uri.parse("file:///"+n));
  m.onDidChangeContent(e=>{
    if(remote)return;
    files[n]=m.getValue();save();
    if(sock){sock.emit("edit",{file:n,changes:e.changes.map(c=>({range:c.range,text:c.text}))});
      clearTimeout(snapT[n]);snapT[n]=setTimeout(()=>sock.emit("snap",{file:n,content:files[n]}),400)}
  });
  return models[n]=m;
}
function show(n){if(!files.hasOwnProperty(n))return;cur=n;if(!open.includes(n))open.push(n);ed.setModel(model(n));renderUI();ed.focus()}
function renderUI(){
  $("files").innerHTML=Object.keys(files).sort().map(n=>`<div class="f ${n==cur?"on":""}" onclick="show('${n}')" ondblclick="ren('${n}')">${n}<span class="x" onclick="event.stopPropagation();del('${n}')">✕</span></div>`).join("");
  $("tabs").innerHTML=open.filter(n=>files.hasOwnProperty(n)).map(n=>`<div class="t ${n==cur?"on":""}" onclick="show('${n}')">${n}</div>`).join("");
}
function newFile(){let n=prompt("File name (e.g. utils.c, utils.h):");if(!n||files[n]!==undefined)return;files[n]="";save();sock&&sock.emit("fileop",{op:"create",name:n});show(n)}
function ren(n){const x=prompt("Rename to:",n);if(!x||x==n||files[x]!==undefined)return;doRename(n,x);sock&&sock.emit("fileop",{op:"rename",name:n,new:x})}
function doRename(n,x){files[x]=models[n]?models[n].getValue():files[n];delete files[n];if(models[n]){models[n].dispose();delete models[n]}open=open.map(o=>o==n?x:o);save();cur==n?show(x):renderUI()}
function del(n){if(!confirm("Delete "+n+"?"))return;doDelete(n);sock&&sock.emit("fileop",{op:"delete",name:n})}
function doDelete(n){delete files[n];if(models[n]){models[n].dispose();delete models[n]}open=open.filter(o=>o!=n);save();if(cur==n){const k=Object.keys(files);if(k.length)show(k[0]);else{files["main.c"]="";show("main.c")}}else renderUI()}

async function run(){
  ptab(0);$("out").textContent="Compiling…";
  try{const r=await fetch("/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({files,stdin:$("stdin").value})});
    $("out").textContent=(await r.json()).out||"(no output)"}catch(e){$("out").textContent="Request failed: "+e}
}

// ---- Sessions & live share ----
const U=ls("cide.user")||{name:"User"+Math.floor(Math.random()*900+100)};
$("name").value=U.name;$("name").onchange=()=>{U.name=$("name").value.trim()||U.name;ls("cide.user",U)};
let hist=ls("cide.hist")||[];
const code6=()=>Math.random().toString(36).slice(2,8);
function addHist(c){hist=[{code:c,t:Date.now()},...hist.filter(h=>h.code!=c)].slice(0,30);ls("cide.hist",hist)}
function rmHist(c){hist=hist.filter(h=>h.code!=c);ls("cide.hist",hist);renderSess()}
function renderSess(){
  $("sess").innerHTML=`<div class="f ${ws=="local"?"on":""}" onclick="enter('local')">Local workspace</div>`+
  hist.map(h=>`<div class="f ${h.code==ws?"on":""}" onclick="enter('${h.code}')" title="Last opened ${new Date(h.t).toLocaleString()}">${h.code}<span class="x" onclick="event.stopPropagation();rmHist('${h.code}')">✕</span></div>`).join("")}
function reset(f){
  Object.values(models).forEach(m=>m.dispose());models={};files=f;
  if(!Object.keys(files).length)files={"main.c":DEF["main.c"]};
  save();open=[Object.keys(files)[0]];show(open[0])}
function enter(c){
  c=String(c||"local").replace(/[^\w-]/g,"").slice(0,40)||"local";
  if(sock){sock.disconnect();sock=null}
  const seed=ls(fkey(c))||(c=="local"?{...DEF}:{...files});
  ws=c;ls("cide.ws",c);reset(seed);
  $("room").value=c=="local"?"":c;$("users").innerHTML="";
  if(c=="local"){$("rs").textContent="Local workspace · saved in this browser";history.replaceState(0,"","/")}
  else{addHist(c);history.replaceState(0,"","?room="+c);connect(c)}
  renderSess();
}
function connect(c){
  sock=io();
  sock.on("connect",()=>{$("rs").textContent="Connecting…";sock.emit("join",{room:c,name:$("name").value.trim()||U.name,files})});
  sock.on("state",s=>reset(s.files));
  sock.on("edit",d=>{const m=models[d.file];if(!m)return;
    remote=true;m.applyEdits(d.changes.map(x=>({range:x.range,text:x.text,forceMoveMarkers:true})));remote=false;files[d.file]=m.getValue();save()});
  sock.on("fileop",d=>{if(d.op=="create"){files[d.name]="";save();renderUI()}else if(d.op=="delete")doDelete(d.name);else doRename(d.name,d.new)});
  sock.on("users",u=>{$("users").innerHTML=u.map(x=>`<div>${x.replace(/[<>&]/g,"")}</div>`).join("");$("rs").textContent=`Room ${c} · ${u.length} online`});
  sock.on("err",m=>{alert(m);enter("local")});
}
function joinCode(){const c=$("room").value.trim();if(c)enter(c)}
function newSession(){enter(code6())}
function copyLink(){ws=="local"?alert("Join or create a room first."):navigator.clipboard.writeText(location.origin+"/?room="+ws)}
window.addEventListener("pagehide",()=>{if(sock&&cur)sock.emit("snap",{file:cur,content:files[cur]})});
// ---- Monaco ----
require.config({paths:{vs:"https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs"}});
require(["vs/editor/editor.main"],()=>{
  ed=monaco.editor.create($("ed"),{theme:"vs-dark",automaticLayout:true,fontSize:14,fontFamily:"Consolas,'Courier New',monospace",minimap:{enabled:true},
    bracketPairColorization:{enabled:true},quickSuggestions:true,suggestOnTriggerCharacters:true,tabSize:4,smoothScrolling:true,cursorSmoothCaretAnimation:"on",model:null});
  const K=monaco.languages.CompletionItemKind,S=monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet;
  const kw="auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while NULL bool size_t uint8_t uint32_t int64_t".split(" ");
  const fn={printf:'printf("${1:%d}\\n", ${2:value});',scanf:'scanf("${1:%d}", &${2:var});',malloc:"malloc(${1:size})",calloc:"calloc(${1:n}, ${2:size})",realloc:"realloc(${1:ptr}, ${2:size})",free:"free(${1:ptr})",strlen:"strlen(${1:s})",strcpy:"strcpy(${1:dst}, ${2:src})",strncpy:"strncpy(${1:dst}, ${2:src}, ${3:n})",strcmp:"strcmp(${1:a}, ${2:b})",strcat:"strcat(${1:dst}, ${2:src})",memset:"memset(${1:p}, ${2:0}, ${3:n})",memcpy:"memcpy(${1:dst}, ${2:src}, ${3:n})",fgets:"fgets(${1:buf}, ${2:size}, ${3:stdin})",fopen:'fopen(${1:"file"}, ${2:"r"})',fclose:"fclose(${1:fp})",fprintf:"fprintf(${1:stderr}, ${2:\"\"})",puts:"puts(${1:s})",putchar:"putchar(${1:c})",getchar:"getchar()",sqrt:"sqrt(${1:x})",pow:"pow(${1:x}, ${2:y})",abs:"abs(${1:x})",atoi:"atoi(${1:s})",rand:"rand()",srand:"srand(${1:time(NULL)})",exit:"exit(${1:0})",qsort:"qsort(${1:arr}, ${2:n}, sizeof(${3:type}), ${4:cmp})",isdigit:"isdigit(${1:c})",toupper:"toupper(${1:c})"};
  const snip={for:"for (int ${1:i} = 0; ${1:i} < ${2:n}; ${1:i}++) {\n\t$0\n}",while:"while (${1:cond}) {\n\t$0\n}",if:"if (${1:cond}) {\n\t$0\n}",ifelse:"if (${1:cond}) {\n\t$2\n} else {\n\t$0\n}",main:"int main(void) {\n\t$0\n\treturn 0;\n}",struct:"typedef struct {\n\t$0\n} ${1:Name};",switch:"switch (${1:x}) {\ncase ${2:1}:\n\t$0\n\tbreak;\ndefault:\n\tbreak;\n}",inc:"#include <${1:stdio}.h>","#include":"#include <${1:stdio}.h>"};
  monaco.languages.registerCompletionItemProvider("c",{triggerCharacters:["#","<"],provideCompletionItems(m,p){
    const w=m.getWordUntilPosition(p),range={startLineNumber:p.lineNumber,endLineNumber:p.lineNumber,startColumn:w.startColumn,endColumn:w.endColumn};
    const hdr="stdio stdlib string math ctype time stdint stdbool limits assert errno signal".split(" ").map(h=>({label:`#include <${h}.h>`,kind:K.Module,insertText:`#include <${h}.h>`,range}));
    return{suggestions:[...hdr,
      ...kw.map(k=>({label:k,kind:K.Keyword,insertText:k,range})),
      ...Object.entries(fn).map(([k,v])=>({label:k,kind:K.Function,insertText:v,insertTextRules:S,detail:"libc",range})),
      ...Object.entries(snip).map(([k,v])=>({label:k,kind:K.Snippet,insertText:v,insertTextRules:S,detail:"snippet",range}))]}}});
  ed.onDidChangeCursorPosition(e=>$("pos").textContent=`Ln ${e.position.lineNumber}, Col ${e.position.column}`);
  ed.addCommand(monaco.KeyMod.CtrlCmd|monaco.KeyCode.Enter,run);
  ed.addCommand(monaco.KeyMod.CtrlCmd|monaco.KeyCode.KeyS,()=>{save();$("rs").textContent="Saved to browser"});
  enter(new URLSearchParams(location.search).get("room")||ls("cide.ws")||"local");
});
</script></body></html>
"""

if __name__ == "__main__":
    sio.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            allow_unsafe_werkzeug=True)
