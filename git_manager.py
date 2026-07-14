"""
git_manager - Version control and AI workspace management tool
Routes: /tool/git_manager/

Data layout in data/git_manager/:
  connections/{id}.json  - remote credentials (gitea, github, generic)
  projects/{id}.json     - local path + remote binding
  workspaces/{id}.json   - AI branch workspace config

AI workspace contract:
  - A workspace is a named branch off the project's main branch
  - Workspace branch is never main/master - enforced at the route level
  - AI can stage and commit to its branch only
  - No merge-to-main route exists in this tool
  - Pull (sync from main) is user-controlled per workspace
  - PR creation pushes the branch and opens a PR via gitea API if configured
"""

import os, subprocess, shutil, json, uuid, asyncio, httpx
from pathlib import Path
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

TOOL_META = {"label": "Git Manager", "icon": "&#x2387;", "description": "Project version control and AI workspace management"}

router = APIRouter()
ENV = {}
_P = "/tool/git_manager"

DATA_DIR = Path("./data/git_manager")
CONN_DIR = DATA_DIR / "connections"
PROJ_DIR = DATA_DIR / "projects"
WS_DIR = DATA_DIR / "workspaces"

_GIT = shutil.which("git") or "git"

decrypt_token = None
encrypt_token = None

def init_module(env: dict):
    global ENV, _P
    ENV = env
    _P  = env.get("meta", {}).get("prefix", _P)
    for d in (CONN_DIR, PROJ_DIR, WS_DIR): d.mkdir(parents=True, exist_ok=True)
    _git_global_config()
    encrypt_token = ENV["encrypt_token"]
    decrypt_token = ENV["decrypt_token"]
    print(f"[git_manager] Ready | git: {_GIT}")

def _git_global_config():
    for args in [["config","--global","safe.directory","*"],
                 ["config","--global","init.defaultBranch","main"],
                 ["config","--global","user.email","git_manager@portal.local"],
                 ["config","--global","user.name","Portal Git Manager"]]:
        subprocess.run([_GIT]+args, capture_output=True, timeout=5)

# -- Storage helpers --

def _load(path: Path) -> Optional[dict]:
    try: return json.loads(path.read_text()) if path.exists() else None
    except Exception: return None

def _save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def _list_dir(d: Path) -> list:
    if not d.exists(): return []
    out = []
    for f in sorted(d.glob("*.json")):
        data = _load(f)
        if data: data["_id"] = f.stem; out.append(data)
    return out

def list_connections() -> list:  return _list_dir(CONN_DIR)
def list_projects()    -> list:  return _list_dir(PROJ_DIR)
def list_workspaces()  -> list:  return _list_dir(WS_DIR)

def get_connection(cid: str) -> Optional[dict]:  return _load(CONN_DIR / f"{cid}.json")
def get_project(pid: str)    -> Optional[dict]:  return _load(PROJ_DIR / f"{pid}.json")
def get_workspace(wid: str)  -> Optional[dict]:  return _load(WS_DIR   / f"{wid}.json")

# -- Git operations --

def _run(args: list, cwd: Path, timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run([_GIT]+args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env={**os.environ,"GIT_TERMINAL_PROMPT":"0"})
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError: return -1, "git binary not found"
    except subprocess.TimeoutExpired: return -1, "git timed out"
    except Exception as e: return -1, str(e)

def _repo_path(project: dict) -> Path: return Path(project["local_path"]).expanduser().resolve()

def _remote_url(conn: dict, repo_name: str) -> str:
    base   = conn["url"].rstrip("/").replace("https://","").replace("http://","")
    scheme = "https" if conn.get("tls") else "http"
    token  = decrypt_token(conn.get("token",""))
    return f"{scheme}://{conn['user']}:{token}@{base}/{conn['user']}/{repo_name}.git"

def git_status(pid: str) -> dict:
    p = get_project(pid)
    if not p: return {"error": "project not found"}
    cwd = _repo_path(p)
    if not (cwd / ".git").exists(): return {"error": "not a git repo", "path": str(cwd)}
    rc_b, branch = _run(["rev-parse","--abbrev-ref","HEAD"], cwd)
    rc_s, status = _run(["status","--short"], cwd)
    rc_h, head   = _run(["log","--oneline","-3"], cwd)
    return {"branch": branch if rc_b == 0 else "unknown", "status": status if rc_s == 0 else "", "clean": rc_s == 0 and not status.strip(), "log": head if rc_h == 0 else "", "path": str(cwd)}

def git_commit(pid: str, message: str, paths: list = None) -> tuple[bool, str]:
    p = get_project(pid)
    if not p: return False, "project not found"
    cwd = _repo_path(p)
    add_args = ["add"] + (paths if paths else ["-A"])
    _run(add_args, cwd)
    rc, out = _run(["commit","-m", message or f"checkpoint {datetime.now().strftime('%Y-%m-%d %H:%M')}"], cwd)
    return rc == 0 or "nothing to commit" in out, out

def git_push(pid: str, branch: str = None, force: bool = False) -> tuple[bool, str]:
    p = get_project(pid)
    if not p: return False, "project not found"
    conn = get_connection(p.get("connection_id","")) if p.get("connection_id") else None
    cwd  = _repo_path(p)
    if conn: _run(["remote","set-url","origin",_remote_url(conn, p["remote_repo"])], cwd)
    target = branch or p.get("main_branch","main")
    args = ["push","origin",target] + (["--force-with-lease"] if force else [])
    rc, out = _run(args, cwd, timeout=60)
    return rc == 0, out

def git_pull(pid: str, branch: str = None) -> tuple[bool, str]:
    p = get_project(pid)
    if not p: return False, "project not found"
    conn = get_connection(p.get("connection_id","")) if p.get("connection_id") else None
    cwd  = _repo_path(p)
    if conn: _run(["remote","set-url","origin",_remote_url(conn, p["remote_repo"])], cwd)
    target = branch or p.get("main_branch","main")
    rc, out = _run(["pull","origin",target], cwd, timeout=60)
    return rc == 0, out

# -- Workspace (AI branch) operations --

def create_workspace(pid: str, label: str, preloaded: list = None, allow_pull: bool = True) -> tuple[Optional[dict], str]:
    p = get_project(pid)
    if not p: return None, "project not found"
    cwd = _repo_path(p)
    wid = uuid.uuid4().hex[:10]
    branch = f"ai/{wid}"
    # Ensure we are on main before branching
    main = p.get("main_branch","main")
    _run(["checkout",main], cwd)
    rc, out = _run(["checkout","-b",branch], cwd)
    if rc != 0: return None, f"branch creation failed: {out}"
    ws = {"id": wid, "label": label, "project_id": pid, "branch": branch, "created": datetime.utcnow().isoformat(), "allow_pull": allow_pull, "preloaded": preloaded or [], "pushed": False}
    _save(WS_DIR / f"{wid}.json", ws)
    return ws, "ok"

def workspace_status(wid: str) -> dict:
    ws = get_workspace(wid)
    if not ws: return {"error": "workspace not found"}
    p = get_project(ws["project_id"])
    if not p: return {"error": "parent project not found"}
    cwd = _repo_path(p)
    main = p.get("main_branch","main")
    _run(["checkout",ws["branch"]], cwd)
    rc_s, status = _run(["status","--short"], cwd)
    rc_d, diff_s = _run(["diff","--stat",f"{main}...{ws['branch']}"], cwd)
    rc_h, commits = _run(["log","--oneline",f"{main}..{ws['branch']}"], cwd)
    return {"branch": ws["branch"], "status": status if rc_s == 0 else "", "diff_stat": diff_s if rc_d == 0 else "", "commits_ahead": commits if rc_h == 0 else "", "label": ws["label"], "allow_pull": ws["allow_pull"]}

def workspace_diff(wid: str) -> str:
    ws = get_workspace(wid)
    if not ws: return "workspace not found"
    p = get_project(ws["project_id"])
    if not p: return "parent project not found"
    cwd = _repo_path(p)
    main = p.get("main_branch","main")
    _run(["checkout",ws["branch"]], cwd)
    rc, diff = _run(["diff",f"{main}...{ws['branch']}"], cwd, timeout=15)
    return diff if rc == 0 else f"diff failed: {diff}"

def workspace_commit(wid: str, message: str) -> tuple[bool, str]:
    ws = get_workspace(wid)
    if not ws: return False, "workspace not found"
    p  = get_project(ws["project_id"])
    if not p: return False, "parent project not found"
    cwd = _repo_path(p)
    # Safety: refuse if on main branch
    rc_b, current = _run(["rev-parse","--abbrev-ref","HEAD"], cwd)
    main = p.get("main_branch","main")
    if current.strip() in (main, "master"): return False, "refusing to commit directly to main branch"
    if current.strip() != ws["branch"].strip():
        _rc, _out = _run(["checkout",ws["branch"]], cwd)
        if _rc != 0: return False, f"could not switch to workspace branch: {_out}"
    return git_commit(ws["project_id"], message)

def workspace_sync(wid: str) -> tuple[bool, str]:
    ws = get_workspace(wid)
    if not ws: return False, "workspace not found"
    if not ws.get("allow_pull"): return False, "pull not permitted for this workspace"
    p  = get_project(ws["project_id"])
    if not p: return False, "parent project not found"
    cwd  = _repo_path(p)
    main = p.get("main_branch","main")
    _run(["checkout",ws["branch"]], cwd)
    rc, out = _run(["merge",main,"--no-edit"], cwd)
    return rc == 0, out

async def workspace_push_pr(wid: str, title: str = "", body: str = "") -> tuple[bool, str]:
    ws = get_workspace(wid)
    if not ws: return False, "workspace not found"
    p  = get_project(ws["project_id"])
    if not p: return False, "parent project not found"
    cwd  = _repo_path(p)
    conn = get_connection(p.get("connection_id","")) if p.get("connection_id") else None
    if conn: _run(["remote","set-url","origin",_remote_url(conn, p["remote_repo"])], cwd)
    _run(["checkout",ws["branch"]], cwd)
    rc, out = _run(["push","--set-upstream","origin",ws["branch"]], cwd, timeout=60)
    if rc != 0: return False, f"push failed: {out}"
    ws["pushed"] = True
    _save(WS_DIR / f"{wid}.json", ws)
    if not conn: return True, f"branch pushed: {ws['branch']} - create PR manually (no remote configured)"
    # Create PR via gitea API
    try:
        main = p.get("main_branch","main")
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{conn['url'].rstrip('/')}/api/v1/repos/{conn['user']}/{p['remote_repo']}/pulls", headers={"Authorization": f"token {conn['token']}", "Content-Type": "application/json"}, json={"head": ws["branch"], "base": main, "title": title or f"AI workspace: {ws['label']}", "body": body or ""})
            if r.status_code in (200, 201):
                pr = r.json()
                return True, f"PR created: {pr.get('html_url', 'see gitea')}"
            return True, f"pushed but PR creation returned HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return True, f"pushed but PR creation failed: {e}"

def destroy_workspace(wid: str, delete_remote: bool = False) -> tuple[bool, str]:
    ws = get_workspace(wid)
    if not ws: return False, "workspace not found"
    p = get_project(ws["project_id"])
    steps = []
    if p:
        cwd  = _repo_path(p)
        main = p.get("main_branch","main")
        _run(["checkout",main], cwd)
        rc, out = _run(["branch","-D",ws["branch"]], cwd)
        steps.append(f"local branch: {'deleted' if rc==0 else out[:80]}")
        if delete_remote and ws.get("pushed"):
            conn = get_connection(p.get("connection_id","")) if p.get("connection_id") else None
            if conn: _run(["remote","set-url","origin",_remote_url(conn, p["remote_repo"])], cwd)
            rc2, out2 = _run(["push","origin","--delete",ws["branch"]], cwd, timeout=30)
            steps.append(f"remote branch: {'deleted' if rc2==0 else out2[:80]}")
    (WS_DIR / f"{wid}.json").unlink(missing_ok=True)
    return True, "; ".join(steps) if steps else "workspace record removed"

# -- Public API for other tools/modules to use --

def render_panel_for_module(request: Request, module_root: Path) -> str:
    """Render the git panel for a module that doesn't have its own git UI.
    Finds the project matching module_root, or shows setup instructions."""
    root_str = str(module_root.resolve())
    proj = next((p for p in list_projects() if str(Path(p["local_path"]).resolve()) == root_str), None)
    if not proj: return f"""<div style="padding:0.7rem;font-size:0.8rem;">
                                <div style="color:var(--text_muted);margin-bottom:0.5rem;">No git project linked for this path:</div>
                                <code style="font-size:0.7rem;">{root_str}</code>
                                <div style="margin-top:0.6rem;"><a href="{_P}/" style="color:var(--accent);">Set up in Git Manager &#x2192;</a></div>
                            </div>"""
    pid  = proj["_id"]
    info = git_status(pid)
    dot  = '<span style="color:#ff9944;">&#x25CF;</span>' if not info.get("clean") else '<span style="color:var(--accent);">&#x25CF;</span>'
    return f"""<div style="padding:0.5rem 0.7rem;font-size:0.8rem;">
                   <div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.4rem;">{dot}<span style="font-family:var(--font-mono);font-size:0.75rem;">{info.get("branch","unknown")}</span></div>
                   <div style="display:flex;gap:0.3rem;flex-wrap:wrap;margin-bottom:0.4rem;">
                       <button class="rpanel-btn" hx-post="{_P}/projects/{pid}/commit" hx-include="#git-msg" hx-target="#git-op-out">&#x2191; Commit+Push</button>
                       <button class="rpanel-btn" hx-post="{_P}/projects/{pid}/pull" hx-target="#git-op-out">&#x2193; Pull</button>
                   </div>
                   <textarea id="git-msg" name="message" placeholder="Commit message..." style="width:100%;background:var(--bg);border:var(--border-thick) solid var(--border);color:var(--text);padding:0.3rem;border-radius:var(--radius);font-size:0.75rem;min-height:3.5rem;box-sizing:border-box;resize:vertical;"></textarea>
                   <pre style="font-size:0.7rem;white-space:pre-wrap;max-height:6rem;overflow-y:auto;margin:0.3rem 0;color:var(--text_muted);">{info.get("status","") or "clean"}</pre>
                   <div id="git-op-out" style="font-size:0.72rem;min-height:1.2rem;"></div>
                   <div style="border-top:var(--border-thick) solid var(--border);margin-top:0.5rem;padding-top:0.4rem;">
                       <a href="{_P}/" style="font-size:0.72rem;color:var(--accent);">Git Manager &#x2192;</a>
                   </div>
                </div>"""

# -- HTML helpers --

def _esc(s): return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"','&quot;')

def _field(label: str, name: str, value: str = "", ftype: str = "text", hint: str = "") -> str:
    return f"""<label style="font-size:0.75rem;color:var(--text_muted);">{label}{f'<span style="opacity:0.6;"> - {hint}</span>' if hint else ""}<input type="{ftype}" name="{name}" value="{_esc(value)}" autocomplete="off" style="width:100%;background:var(--bg);border:var(--border-thick) solid var(--border);color:var(--text);padding:0.35rem;border-radius:var(--radius);font-family:var(--font-mono);font-size:0.8rem;box-sizing:border-box;margin-top:0.2rem;"></label>"""

def _render_projects() -> str:
    projects = list_projects()
    conns = {c["_id"]: c for c in list_connections()}
    if not projects: return '<div style="color:var(--text_muted);font-size:0.85rem;padding:0.5rem 0;">No projects. Add one below.</div>'
    cards = ""
    for p in projects:
        pid = p["_id"]
        info = git_status(pid)
        conn = conns.get(p.get("connection_id",""), {})
        dot = '<span style="color:#ff9944;">&#x25CF;</span>' if not info.get("clean") else '<span style="color:var(--accent);">&#x25CF;</span>'
        if info.get("error"): dot = '<span style="color:#ff5f5f;">&#x25CF;</span>'
        cards += (f"""<div class="glass" style="padding:0.6rem;margin-bottom:0.4rem;">
                          <div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.3rem;">
                              {dot}<span style="font-weight:600;font-size:0.85rem;flex:1;">{_esc(p["label"])}</span>
                              <span style="font-size:0.7rem;color:var(--text_muted);font-family:var(--font-mono);">{_esc(info.get("branch","?"))}</span>
                              <button class="btn-icon" style="color:#ff5f5f;" hx-delete="{_P}/projects/{pid}" hx-target="#gm-projects" hx-swap="outerHTML" hx-confirm="Remove project?">&#x2715;</button>
                          </div>
                          <div style="font-size:0.7rem;color:var(--text_muted);font-family:var(--font-mono);margin-bottom:0.4rem;">{_esc(p["local_path"])}</div>
                          <div style="display:flex;gap:0.3rem;flex-wrap:wrap;">
                              <button class="rpanel-btn" hx-get="{_P}/projects/{pid}/status" hx-target="#gm-op-out">Status</button>
                              <button class="rpanel-btn" hx-post="{_P}/projects/{pid}/pull" hx-target="#gm-op-out">&#x2193; Pull</button>
                          </div>
                      </div>""")
    return cards

def _render_workspaces() -> str:
    workspaces = list_workspaces()
    projects   = {p["_id"]: p for p in list_projects()}
    if not workspaces: return '<div style="color:var(--text_muted);font-size:0.85rem;padding:0.5rem 0;">No AI workspaces active.</div>'
    cards = ""
    for ws in workspaces:
        wid  = ws["_id"]
        proj = projects.get(ws["project_id"], {})
        pull_badge = '<span style="font-size:0.65rem;color:var(--accent);">sync allowed</span>' if ws.get("allow_pull") else '<span style="font-size:0.65rem;color:var(--text_muted);">sync locked</span>'
        pushed_badge = '<span style="font-size:0.65rem;color:var(--accent);">pushed</span>' if ws.get("pushed") else ''
        cards += (f"""<div class="glass" style="padding:0.6rem;margin-bottom:0.4rem;">
                          <div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.3rem;">
                              <span style="font-weight:600;flex:1;font-size:0.85rem;">{_esc(ws["label"])}</span>
                              {pull_badge} {pushed_badge}
                              <button class="btn-icon" style="color:#ff5f5f;" hx-delete="{_P}/workspaces/{wid}" hx-target="#gm-workspaces" hx-swap="outerHTML" hx-confirm="Destroy workspace?">&#x2715;</button>
                          </div>
                          <div style="font-size:0.7rem;color:var(--text_muted);font-family:var(--font-mono);">{_esc(ws["branch"])} | {_esc(proj.get("label","?"))}</div>
                          <div style="display:flex;gap:0.3rem;flex-wrap:wrap;margin-top:0.35rem;">
                              <button class="rpanel-btn" hx-get="{_P}/workspaces/{wid}/status" hx-target="#gm-op-out">Status</button>
                              <button class="rpanel-btn" hx-get="{_P}/workspaces/{wid}/diff" hx-target="#gm-op-out">Diff</button>
                              {f'<button class="rpanel-btn" hx-post="{_P}/workspaces/{wid}/sync" hx-target="#gm-op-out">&#x21BA; Sync</button>' if ws.get("allow_pull") else ''}
                              <button class="rpanel-btn" hx-post="{_P}/workspaces/{wid}/push-pr" hx-target="#gm-op-out">&#x2191; Push PR</button>
                          </div>
                        </div>""")
    return cards

# -- Routes --

@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conns = list_connections()
    conn_opts = "".join(f'<option value="{c["_id"]}">{_esc(c["label"])}</option>' for c in conns)
    conn_html = ""
    for c in conns:
        conn_html += (f"""<div style="display:flex;align-items:center;gap:0.4rem;padding:0.3rem 0;border-bottom:1px solid var(--border);font-size:0.82rem;">
                              <span style="flex:1;">{_esc(c["label"])}</span>
                              <span style="color:var(--text_muted);font-size:0.72rem;font-family:var(--font-mono);">{_esc(c.get("url","")[:40])}</span>
                              <button class="btn-icon" style="color:#ff5f5f;" hx-delete="{_P}/connections/{c["_id"]}" hx-target="#gm-connections" hx-swap="outerHTML" hx-confirm="Delete?">&#x2715;</button>
                          </div>""")
    if not conn_html: conn_html = '<div style="color:var(--text_muted);font-size:0.85rem;">No connections.</div>'
    proj_opts = "".join(f'<option value="{p["_id"]}">{_esc(p["label"])}</option>' for p in list_projects())
    return HTMLResponse(f"""<div style="max-width:64rem;margin:0 auto;padding:1.5rem;display:grid;grid-template-columns:1fr 1fr;gap:1.5rem;align-items:start;">
                                <div>
                                  <h3 style="margin:0 0 0.8rem;font-size:0.9rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:0.08em;">Connections</h3>
                                  <div id="gm-connections">{conn_html}</div>
                                  <details class="glass" style="padding:0.7rem;margin-top:0.6rem;">
                                    <summary style="cursor:pointer;font-size:0.82rem;color:var(--text_muted);">+ Add Connection</summary>
                                    <form hx-post="{_P}/connections/add" hx-target="#gm-connections" hx-swap="outerHTML" style="display:flex;flex-direction:column;gap:0.45rem;margin-top:0.6rem;">
                                      {_field("Label","label","","text","display name")}
                                      {_field("URL","url","","text","http://gitea:3000")}
                                      {_field("Username","user")}
                                      {_field("Token","token","","password","personal access token")}
                                      <label style="font-size:0.75rem;color:var(--text_muted);">Type
                                        <select name="type" style="width:100%;background:var(--bg);border:var(--border-thick) solid var(--border);color:var(--text);padding:0.35rem;border-radius:var(--radius);font-size:0.8rem;">
                                          <option value="gitea">Gitea</option><option value="github">GitHub</option><option value="generic">Generic</option>
                                        </select></label>
                                      <button type="submit" class="ui-btn">Save</button>
                                    </form>
                                  </details>
                                  <h3 style="margin:1.2rem 0 0.8rem;font-size:0.9rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:0.08em;">Projects</h3>
                                  <div id="gm-projects">{_render_projects()}</div>
                                  <details class="glass" style="padding:0.7rem;margin-top:0.6rem;">
                                    <summary style="cursor:pointer;font-size:0.82rem;color:var(--text_muted);">+ Add Project</summary>
                                    <form hx-post="{_P}/projects/add" hx-target="#gm-projects" hx-swap="outerHTML" style="display:flex;flex-direction:column;gap:0.45rem;margin-top:0.6rem;">
                                      {_field("Label","label","","text","display name")}
                                      {_field("Local Path","local_path","","text","/path/to/repo or relative")}
                                      {_field("Remote Repo Name","remote_repo","","text","repo-name (for push/PR URL)")}
                                      {_field("Main Branch","main_branch","main")}
                                      <label style="font-size:0.75rem;color:var(--text_muted);">Connection (optional)
                                        <select name="connection_id" style="width:100%;background:var(--bg);border:var(--border-thick) solid var(--border);color:var(--text);padding:0.35rem;border-radius:var(--radius);font-size:0.8rem;">
                                          <option value="">None (local only)</option>{conn_opts}
                                        </select></label>
                                      <button type="submit" class="ui-btn">Add Project</button>
                                    </form>
                                  </details>
                                </div>
                                <div>
                                  <h3 style="margin:0 0 0.8rem;font-size:0.9rem;color:var(--text_muted);text-transform:uppercase;letter-spacing:0.08em;">AI Workspaces</h3>
                                  <div id="gm-workspaces">{_render_workspaces()}</div>
                                  <details class="glass" style="padding:0.7rem;margin-top:0.6rem;">
                                    <summary style="cursor:pointer;font-size:0.82rem;color:var(--text_muted);">+ Create Workspace</summary>
                                    <form hx-post="{_P}/workspaces/create" hx-target="#gm-workspaces" hx-swap="outerHTML" style="display:flex;flex-direction:column;gap:0.45rem;margin-top:0.6rem;">
                                      {_field("Label","label","","text","e.g. Claude-session-1")}
                                      <label style="font-size:0.75rem;color:var(--text_muted);">Project
                                        <select name="project_id" style="width:100%;background:var(--bg);border:var(--border-thick) solid var(--border);color:var(--text);padding:0.35rem;border-radius:var(--radius);font-size:0.8rem;">
                                          {proj_opts}
                                        </select></label>
                                      {_field("Preload Paths (comma-sep)","preloaded","","text","backend/, frontend/style.py")}
                                      <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.78rem;">
                                        <input type="checkbox" name="allow_pull" value="1" checked> Allow sync from main</label>
                                      <button type="submit" class="ui-btn">Create Branch</button>
                                    </form>
                                  </details>
                                  <div class="glass" style="padding:0.7rem;margin-top:0.8rem;">
                                    <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.5rem;">Operation Output</div>
                                    <div id="gm-op-out" style="font-family:var(--font-mono);font-size:0.72rem;white-space:pre-wrap;max-height:14rem;overflow-y:auto;min-height:2rem;color:var(--text_muted);"></div>
                                  </div>
                                </div>
                            </div>""")

@router.post("/connections/add", response_class=HTMLResponse)
async def connections_add(request: Request):
    f = await request.form()
    cid = f"conn_{uuid.uuid4().hex[:8]}"
    _save(CONN_DIR / f"{cid}.json", {"label": f.get("label",""), "type": f.get("type","gitea"), "url": f.get("url","").rstrip("/"), "user": f.get("user",""), "token": encrypt_token(f.get("token","")), "created": datetime.utcnow().isoformat()})
    conn_html = "".join(f"""<div style="display:flex;align-items:center;gap:0.4rem;padding:0.3rem 0;border-bottom:(--border-thick) solid var(--border); font-size:0.82rem;">
                                <span style="flex:1;">{_esc(c["label"])}</span>
                                <span style="color:var(--text_muted); font-size:0.72rem; font-family:var(--font-mono);">{_esc(c.get("url","")[:40])}</span>
                                <button class="btn-icon" style="color:#ff5f5f;" hx-delete="{_P}/connections/{c["_id"]}" hx-target="#gm-connections" hx-swap="outerHTML">&#x2715;</button>
                            </div>"""for c in list_connections())
    return HTMLResponse(f'<div id="gm-connections">{conn_html or "<div style=color:var(--text_muted);font-size:0.85rem;>No connections.</div>"}</div>')

@router.delete("/connections/{cid}", response_class=HTMLResponse)
async def connections_delete(cid: str):
    (CONN_DIR / f"{cid}.json").unlink(missing_ok=True)
    conns = list_connections()
    conn_html = "".join(f'<div style="display:flex;align-items:center;gap:0.4rem;padding:0.3rem 0;border-bottom:1px solid var(--border);font-size:0.82rem;"><span style="flex:1;">{_esc(c["label"])}</span><span style="color:var(--text_muted);font-size:0.7rem;">{_esc(c.get("url","")[:40])}</span><button class="btn-icon" style="color:#ff5f5f;" hx-delete="{_P}/connections/{c["_id"]}" hx-target="#gm-connections" hx-swap="outerHTML">&#x2715;</button></div>' for c in conns)
    return HTMLResponse(f'<div id="gm-connections">{conn_html or "<div style=color:var(--text_muted);>No connections.</div>"}</div>')

@router.post("/projects/add", response_class=HTMLResponse)
async def projects_add(request: Request):
    f = await request.form()
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    _save(PROJ_DIR / f"{pid}.json", {"label": f.get("label",""), "local_path": f.get("local_path",""), "remote_repo": f.get("remote_repo",""), "main_branch": f.get("main_branch","main"), "connection_id": f.get("connection_id",""), "created": datetime.utcnow().isoformat()})
    return HTMLResponse(f'<div id="gm-projects">{_render_projects()}</div>')

@router.delete("/projects/{pid}", response_class=HTMLResponse)
async def projects_delete(pid: str):
    (PROJ_DIR / f"{pid}.json").unlink(missing_ok=True)
    return HTMLResponse(f'<div id="gm-projects">{_render_projects()}</div>')

@router.get("/projects/{pid}/status", response_class=HTMLResponse)
async def projects_status(pid: str):
    info = git_status(pid)
    if info.get("error"): return HTMLResponse(f'<pre style="color:#ff5f5f;">{_esc(info["error"])}</pre>')
    return HTMLResponse(f"""<pre style="font-size:0.75rem;white-space:pre-wrap;">
                                branch: {_esc(info["branch"])}
                                {"clean" if info["clean"] else _esc(info["status"])}
                                {_esc(info["log"])}
                            </pre>""")

@router.post("/projects/{pid}/commit", response_class=HTMLResponse)
async def projects_commit(pid: str, message: str = Form("")):
    ok, out = git_commit(pid, message)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')

@router.post("/projects/{pid}/push", response_class=HTMLResponse)
async def projects_push(pid: str):
    ok, out = git_push(pid)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')

@router.post("/projects/{pid}/pull", response_class=HTMLResponse)
async def projects_pull(pid: str):
    ok, out = git_pull(pid)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')

@router.post("/workspaces/create", response_class=HTMLResponse)
async def workspaces_create(request: Request):
    f = await request.form()
    preloaded = [x.strip() for x in f.get("preloaded","").split(",") if x.strip()]
    ws, msg = create_workspace(f.get("project_id",""), f.get("label","workspace"), preloaded, bool(f.get("allow_pull")))
    if not ws: return HTMLResponse(f'<div id="gm-workspaces"><div style="color:#ff5f5f;font-size:0.82rem;">{_esc(msg)}</div>{_render_workspaces()}</div>')
    return HTMLResponse(f'<div id="gm-workspaces">{_render_workspaces()}</div>')

@router.delete("/workspaces/{wid}", response_class=HTMLResponse)
async def workspaces_delete(wid: str, delete_remote: bool = False):
    destroy_workspace(wid, delete_remote)
    return HTMLResponse(f'<div id="gm-workspaces">{_render_workspaces()}</div>')

@router.get("/workspaces/{wid}/status", response_class=HTMLResponse)
async def workspaces_status(wid: str):
    info = workspace_status(wid)
    if info.get("error"): return HTMLResponse(f'<pre style="color:#ff5f5f;">{_esc(info["error"])}</pre>')
    return HTMLResponse(f"""<pre style="font-size:0.75rem;white-space:pre-wrap;">
                                branch: {_esc(info["branch"])}
                                commits ahead: {_esc(info["commits_ahead"]) or "(none)"}
                                diff stat: {_esc(info["diff_stat"]) or "(no changes)"}
                                working tree: {_esc(info["status"]) or "(clean)"}
                            </pre>""")

@router.get("/workspaces/{wid}/diff", response_class=HTMLResponse)
async def workspaces_diff(wid: str):
    diff = workspace_diff(wid)
    return HTMLResponse(f'<pre style="font-size:0.7rem;white-space:pre-wrap;max-height:20rem;overflow-y:auto;">{_esc(diff[:8000])}{"..." if len(diff)>8000 else ""}</pre>')

@router.post("/workspaces/{wid}/sync", response_class=HTMLResponse)
async def workspaces_sync(wid: str):
    ok, out = workspace_sync(wid)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')

@router.post("/workspaces/{wid}/push-pr", response_class=HTMLResponse)
async def workspaces_push_pr(wid: str, title: str = Form(""), body: str = Form("")):
    ok, out = await workspace_push_pr(wid, title, body, {"Content-Type": "application/json"})
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')

@router.post("/workspaces/{wid}/commit", response_class=HTMLResponse)
async def workspaces_commit(wid: str, message: str = Form("")):
    ok, out = workspace_commit(wid, message)
    return HTMLResponse(f'<pre style="color:{"var(--accent)" if ok else "#ff5f5f"};font-size:0.75rem;">{_esc(out)}</pre>')