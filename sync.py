"""sync.py - Git Manager's sync engine: this portal's code folders against one or more git servers (targets), by the rules in data/_tools/git_manager/sync.json.
Git Manager owns how git is done here; the pages people use (Dev Studio's Sync) call it through the "git_sync" service: config, check_config, targets, run, code_saved.

Rules (all from the config - another workflow is another config, not other code):
  - Each target (a git server and owner on a Git Manager connection) has its own folder -> repository list: its own repos, or like another target's list. An inherited list is a black list with skip (everything but these; folders added to the other list later come along) or a white list with only (nothing but these; later folders do not come along). extra adds folders of its own.
  - Push and pull each have their own narrowing of that list (push_only, push_skip, pull_only, pull_skip): a folder may be pulled from a target and never sent to it, or the other way. A folder asked for that a direction does not cover is refused by name.
  - push "none": this portal only pulls from that target (a deployment). push "force": the folder as it is here is the truth - local changes are committed (author = the target's identity when it has one, else the acting portal user's) and pushed over whatever the target has. What the push replaced is kept here first as branch portal-sync-replaced/<target>/<time>. push "safe": refused when the target has commits the folder lacks.
  - pull "replace": the target is the truth - the folder becomes exactly the target's version; local commits and changes are kept first as branch portal-sync-backup/<time> (unless backup is false). pull "ff": fast-forward only, refused otherwise.
  - history "snapshot": each push is one new commit with no parents (author = the target's identity, excluded paths left out), so the target holds the current files and nothing of their history. history "keep": local commits are pushed as they are.
  - A repository missing on a target is created when push is asked to (Create & push, or create_missing "yes"; "no" never creates) - private unless the target's create.private is false.
  - A missing folder is cloned on pull; a folder with files but no .git becomes a checkout in place (its files kept as the backup commit). Nothing here changes file permissions: git runs with core.fileMode=false, so executable bits are neither changed in a checkout's records nor pushed.
  - The token travels only in an environment-only http header per git call - never on a command line, in a URL or in .git/config.
  - Whose account: each person may choose, per target, one of their own Git Manager connections and their commit identity (sync_people.json, set by themselves on the Sync page); without a choice the target's connection_id and the config's users entry are used.
"""
import os, sys, json, time, shutil, asyncio, base64, fnmatch, subprocess, httpx
from pathlib import Path
from datetime import datetime

CONFIG = Path("./data/_tools/git_manager/sync.json")
STATE = Path("./data/_tools/git_manager/sync_state.json")   # per target and folder: the target commit this portal last pushed or pulled - for "changed there since" notes
LAST = Path("./data/_tools/git_manager/sync_last.json")     # the latest results, shown again after the restart a pull may cause
PEOPLE = Path("./data/_tools/git_manager/sync_people.json")  # {user: {"identity": {"name", "email"}, "connections": {target: connection id}}}: what each person chose for themselves
PROGRESS: dict = {}   # the run under way: {"op", "target", "user", "total", "done", "current", "started"}
GM = None   # the git_manager module: connections, token decryption, git binary
SERVICES = {}
_LOCK = asyncio.Lock()
_RESTART = {"at": 0.0}

def init(gm, services: dict):
    global GM, SERVICES
    GM, SERVICES = gm, services

# --- Config ---

def config() -> dict: return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}

def save_config(text: str) -> list:
    """Saves the config when it parses and checks clean; returns the problems otherwise (nothing saved)."""
    try: c = json.loads(text)
    except json.JSONDecodeError as e: return [f"not valid JSON: {e.msg} at line {e.lineno} column {e.colno}"]
    problems = check_config(c)
    if not problems: CONFIG.parent.mkdir(parents=True, exist_ok=True); CONFIG.write_text(json.dumps(c, indent=2))
    return problems

def check_config(c: dict) -> list:
    out = []
    for name, t in c.get("targets", {}).items():
        if name.startswith("_"): continue
        if not GM.get_connection(t.get("connection_id", "")): out.append(f"""target {name}: connection_id '{t.get("connection_id", "")}' is not a Git Manager connection""")
        if t.get("push", "force") not in ("force", "safe", "none"): out.append(f"target {name}: push must be force, safe or none")
        if t.get("pull", "replace") not in ("replace", "ff"): out.append(f"target {name}: pull must be replace or ff")
        if t.get("history", "keep") not in ("keep", "snapshot"): out.append(f"target {name}: history must be keep or snapshot")
        if t.get("history") == "snapshot" and not (t.get("identity") or {}).get("email"): out.append(f"target {name}: history snapshot needs identity {{name, email}} - the author of each snapshot")
        if t.get("like") and t["like"] not in c.get("targets", {}): out.append(f"""target {name}: like '{t["like"]}' is not a target""")
        if t.get("create_missing", "ask") not in ("ask", "yes", "no"): out.append(f"target {name}: create_missing must be ask, yes or no")
        out += [f"target {name}: {k} must be a list of folders or globs" for k in ("only", "skip", "push_only", "push_skip", "pull_only", "pull_skip") if k in t and not isinstance(t[k], list)]
    for u, i in c.get("users", {}).items():
        if u.startswith("_"): continue
        if not (i.get("name") and i.get("email")): out.append(f"user {u}: needs name and email (the commit identity)")
    return out

def targets(c: dict = None) -> list: return [n for n in (c or config()).get("targets", {}) if not n.startswith("_")]

def _hit(path: str, patterns: list) -> bool: return any(path == x or fnmatch.fnmatch(path, x) for x in patterns)

def repos(c: dict, name: str, op: str = "", _seen=()) -> dict:
    """{folder: repository} of a target. Its list: like (another target's list) narrowed by only (when given, nothing else is inherited: a white list) and by skip (a black list), plus its own repos and extra. op "push" or "pull" narrows the list for that direction alone (<op>_only, <op>_skip), so what a target is sent and what is taken from it can differ. Entries of only and skip are folders or globs."""
    t = c["targets"][name]
    if name in _seen: raise ValueError(f"target {name}: like loops back to itself")
    base = repos(c, t["like"], "", (*_seen, name)) if t.get("like") else {}
    whole = {**{p: r for p, r in base.items() if ("only" not in t or _hit(p, t["only"])) and not _hit(p, t.get("skip", []))}, **t.get("repos", {}), **t.get("extra", {})}
    return {p: r for p, r in whole.items() if not op or ((f"{op}_only" not in t or _hit(p, t[f"{op}_only"])) and not _hit(p, t.get(f"{op}_skip", [])))}

def explain(c: dict, name: str) -> list:
    """A target's list in words, for whoever is about to use it: how many folders each direction covers, and whether folders added to the list it is like will come along by themselves."""
    t, whole = c["targets"][name], repos(c, name)
    out = [f"""{len(whole)} folder(s): push covers {len(repos(c, name, "push"))}, pull covers {len(repos(c, name, "pull"))}."""]
    if t.get("like") and "only" not in t: out.append(f"""Black list: every folder of '{t["like"]}' except its skip list. A folder added to '{t["like"]}' later is on this target too, by itself. To send only named folders, give this target "only": [folders]; "repos" adds to the inherited list and does not replace it.""")
    if t.get("like") and "only" in t: out.append(f"""White list: of '{t["like"]}' only the {len(t["only"])} entr{"y" if len(t["only"]) == 1 else "ies"} under "only". A folder added to '{t["like"]}' later is not on this target until named there.""")
    if not t.get("like"): out.append("White list: only the folders written under this target's repos.")
    return out

# --- Lists as a grid (the Sync page's folders-by-target editor) ---

def _inherited(c: dict, name: str) -> dict: return repos(c, c["targets"][name]["like"]) if c["targets"][name].get("like") else {}
def _own(c: dict, name: str) -> dict: return {**c["targets"][name].get("repos", {}), **c["targets"][name].get("extra", {})}

def grid(c: dict) -> dict:
    """{"folders": {folder: repository}, "targets": {name: {"can": [folders it may take], "push": [...], "pull": [...], "white": bool}}}: every folder any list names, and for each target which of them it could take (what it inherits and its own) and which each direction covers now."""
    names = targets(c)
    folders = {p: r for n in [*[k for k in c.get("targets", {}) if k.startswith("_")], *names] for p, r in {**_inherited(c, n), **_own(c, n)}.items()} if names else {}
    return {"folders": dict(sorted(folders.items())), "targets": {n: {"can": sorted({**_inherited(c, n), **_own(c, n)}), "push": sorted(repos(c, n, "push")), "pull": sorted(repos(c, n, "pull")), "white": "only" in c["targets"][n] or not c["targets"][n].get("like")} for n in names}}

def set_lists(c: dict, name: str, push: list, pull: list, white: bool) -> dict:
    """The config with one target's lists written from ticks: push and pull are the folders each direction covers. white: a folder added later to the list it is like stays off until ticked ("only"); otherwise it comes along by itself ("skip"). Folder patterns written by hand in this target's lists are replaced by the names ticked."""
    t, inherited, own = dict(c["targets"][name]), set(_inherited(c, name)), set(_own(c, name))
    for k in ("only", "skip", "push_only", "push_skip", "pull_only", "pull_skip"): t.pop(k, None)
    push, pull = set(push) & (inherited | own), set(pull) & (inherited | own)
    if white:
        if t.get("like"): t["only"] = sorted((push | pull) & inherited)
        kept = (push | pull) & inherited | own
        t.update({f"{op}_only": sorted(got) for op, got in (("push", push), ("pull", pull)) if got != kept})
    else:
        t.update({"skip": sorted(gone)} if (gone := inherited - push - pull) else {})
        t.update({f"{op}_skip": sorted(off) for op, got in (("push", push), ("pull", pull)) if (off := (inherited | own) - set(t.get("skip", [])) - got)})
    return {**c, "targets": {**c["targets"], name: t}}

def instance(c: dict = None) -> dict: return {"name": "portal", "role": "deployed", "restart_after_pull": True, "restart_on_code_change": False, **(c or config()).get("instance", {})}

def mine(user: str) -> dict: return (json.loads(PEOPLE.read_text()) if PEOPLE.exists() else {}).get(user, {})

def set_mine(user: str, admin: bool, target: str = "", connection_id: str = "", name: str = "", email: str = "") -> str:
    """What a person chooses for themselves: their commit identity, and per target which of their connections sync uses when they act (empty: the target's own). Only a connection they may use."""
    if connection_id and not any(c["_id"] == connection_id for c in GM.connections_of(user, admin)): raise ValueError(f"connection {connection_id} is not one of yours")
    allp = json.loads(PEOPLE.read_text()) if PEOPLE.exists() else {}
    me = allp.setdefault(user, {})
    if name or email: me["identity"] = {"name": name.strip(), "email": email.strip()}
    if target: me.setdefault("connections", {})[target] = connection_id
    if target and not connection_id: me["connections"].pop(target)
    PEOPLE.write_text(json.dumps(allp, indent=2))
    return "saved"

def progress() -> dict: return dict(PROGRESS)

def may_use(c: dict, name: str, user) -> bool: return getattr(user, "role", "") == "admin" or getattr(user, "username", "") in c["targets"][name].get("users", [])   # admin always; anyone else only when the target lists them

# --- Git ---

def _env(header: str = "", ident: dict = None) -> dict:
    cfg = [("safe.directory", "*"), ("core.fileMode", "false"), ("commit.gpgsign", "false"), *([("http.extraHeader", header)] if header else [])]
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(len(cfg)), **{f"GIT_CONFIG_{k}_{i}": v for i, kv in enumerate(cfg) for k, v in zip(("KEY", "VALUE"), kv)}}
    return {**env, **({"GIT_AUTHOR_NAME": ident["name"], "GIT_AUTHOR_EMAIL": ident["email"], "GIT_COMMITTER_NAME": ident["name"], "GIT_COMMITTER_EMAIL": ident["email"]} if ident else {})}

def _git(args: list, cwd: Path, env: dict, timeout: int = 120) -> tuple:
    r = subprocess.run([GM._GIT, *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env={**os.environ, **env})
    return r.returncode, (r.stdout + r.stderr).strip()

def _ok(args: list, cwd: Path, env: dict, timeout: int = 120) -> str:
    rc, out = _git(args, cwd, env, timeout)
    if rc: raise RuntimeError(f"""git {args[0]} failed: {out[-400:]}""")
    return out

def _base(conn: dict, url: str = "") -> str:
    """scheme://host[/path] of a connection's server (or of url): a URL saved without a scheme gets https when the connection has tls, else http."""
    url = (url or conn["url"]).strip().rstrip("/")
    return url if "://" in url else f"""{"https" if conn.get("tls") else "http"}://{url}"""

def _auth(t: dict, cid: str = "") -> tuple:
    """(owner base URL, http header) from a Git Manager connection: the one given (the acting person's choice), else the target's."""
    conn = GM.get_connection(cid or t["connection_id"])
    cred = f"""{conn["user"]}:{GM.decrypt_token(conn.get("token", ""))}"""
    return f"""{_base(conn)}/{t["owner"]}""", f"Authorization: Basic {base64.b64encode(cred.encode()).decode()}"

class Missing(Exception): """The repository does not exist on the target."""

def _fetch(cwd: Path, url: str, branch: str, env: dict) -> str:
    """The target's commit for branch ("" when the repository or branch is empty); Missing when the repository does not exist; raises on any other failure."""
    rc, out = _git(["fetch", "-q", "--no-tags", url, branch], cwd, env, timeout=300)
    if rc and "couldn't find remote ref" in out: return ""   # the repository exists but has no such branch yet (empty)
    if rc and any(x in out.lower() for x in ("not found", "does not exist", "does not appear to be a git repository")): raise Missing(out[-200:])
    if rc and "permission denied" in out.lower(): raise RuntimeError(f"fetch failed: {out[-200:]} - {_owners(cwd)}")
    if rc: raise RuntimeError(f"fetch failed: {out[-400:]}")
    return _ok(["rev-parse", "FETCH_HEAD"], cwd, env)

def _owners(cwd: Path) -> str:
    """Why git may not write in a checkout: who owns its .git (user id) against who the portal runs as, and how to give it to the portal. Git refuses to change a checkout it cannot write; nothing is changed here."""
    g, me = cwd / ".git", os.getuid() if hasattr(os, "getuid") else -1
    if not g.exists(): return f"{cwd} has no .git"
    bad = sorted({f.stat().st_uid for f in [g, *g.iterdir()] if f.stat().st_uid != me})
    return f"""the portal runs as user id {me}; {g} (or files in it) belong to user id(s) {", ".join(map(str, bad))}. On the machine holding this folder: sudo chown -R {me}:{os.getgid()} <the folder>, or run the portal's container as the user that owns it""" if bad else f"the portal runs as user id {me} and owns {g}; a file inside it may still be read-only (ls -ln {g})"

def _create_repo(t: dict, repo: str) -> str:
    """Creates repo under the target's owner through the server's API (Gitea / Forgejo, GitHub): as an organisation repository, or as the connection user's own when the owner is that user. "" when created or already there, else the error."""
    conn, private = GM.get_connection(t["connection_id"]), (t.get("create") or {}).get("private", True)
    if not GM.decrypt_token(conn.get("token", "")): return f"""creating {repo}: the connection {conn.get("label", "")} has no token - add one on the Git Manager page"""
    host = conn["url"].split("://", 1)[-1].rstrip("/")
    gh = conn.get("type") == "github" or host.split("/")[0] in ("github.com", "www.github.com")
    api = _base(conn, t["api_url"]) if t.get("api_url") else "https://api.github.com" if gh else f"""{_base(conn)}/api/v1"""   # api_url: when the server's API is not at <url>/api/v1
    headers = {"Authorization": f"""{"Bearer" if gh else "token"} {GM.decrypt_token(conn.get("token", ""))}""", "Accept": "application/vnd.github+json" if gh else "application/json"}
    body = {"name": repo, "private": private, "description": (t.get("create") or {}).get("description", "")}
    with httpx.Client(timeout=30) as c:
        for path in ([f"""/orgs/{t["owner"]}/repos"""] + (["/user/repos"] if t["owner"].lower() == conn["user"].lower() else [])):
            r = c.post(api + path, json=body, headers=headers)
            if r.status_code in (200, 201) or r.status_code in (409, 422) and "exist" in r.text.lower(): return ""
            if r.status_code != 404: return f"""creating {repo} failed: HTTP {r.status_code} {r.text[:200]}"""
    return f"""creating {repo} failed: {t["owner"]} is neither an organisation the connection's user can create in nor that user"""

def _head(cwd: Path, env: dict) -> str: rc, out = _git(["rev-parse", "-q", "--verify", "HEAD"], cwd, env); return out if rc == 0 else ""
def _tree(cwd: Path, rev: str, env: dict) -> str: return _ok(["rev-parse", f"{rev}^{{tree}}"], cwd, env) if rev else ""
def _dirty(cwd: Path, env: dict) -> int: return len([l for l in _ok(["status", "--porcelain"], cwd, env).splitlines() if l.strip()])
def _differs(cwd: Path, a: str, b: str, env: dict) -> int: return len(_ok(["diff", "--name-only", a, b], cwd, env).splitlines()) if a and b else -1
def _stamp() -> str: return datetime.now().strftime("%Y%m%d-%H%M%S")

def _commit_local(cwd: Path, ident: dict, message: str, env: dict) -> bool:
    """Commits every change in the folder (ignored files stay out) with the acting user's identity. True when there was something to commit."""
    if not _dirty(cwd, env): return False
    _ok(["add", "-A"], cwd, env)
    _ok(["commit", "-q", "--no-verify", "-m", message], cwd, {**env, **_env(ident=ident)})
    return True

def _snapshot(cwd: Path, t: dict, message: str, env: dict) -> str: return _ok(["commit-tree", _snapshot_tree(cwd, t, env), "-m", message], cwd, {**env, **_env(ident=t["identity"])})   # one parentless commit, authored by the target's identity

def _sent_tree(cwd: Path, t: dict, env: dict) -> str: return _snapshot_tree(cwd, t, env) if t.get("history") == "snapshot" else _tree(cwd, "HEAD", env)   # the files a push to t would send

def _snapshot_tree(cwd: Path, t: dict, env: dict) -> str:
    """HEAD's files minus the target's excludes (a path, or a pattern matched against the path and each of its parts), as a tree."""
    idx, ex = cwd / ".git" / "portal-sync-index", t.get("excludes", [])
    ienv = {**env, "GIT_INDEX_FILE": str(idx)}
    try:
        _ok(["read-tree", "HEAD"], cwd, ienv)
        drop = [p for p in _ok(["ls-files"], cwd, ienv).splitlines() if any(fnmatch.fnmatch(p, x) or any(fnmatch.fnmatch(part, x) for part in Path(p).parts) for x in ex)]
        for i in range(0, len(drop), 200): _ok(["rm", "--cached", "-q", "--", *drop[i:i + 200]], cwd, ienv)
        return _ok(["write-tree"], cwd, ienv)
    finally: idx.unlink(missing_ok=True)

# --- Per folder (run in a worker thread, one folder at a time per call) ---

def _state() -> dict: return json.loads(STATE.read_text()) if STATE.exists() else {}
def _remember(name: str, path: str, sha: str): s = _state(); s.setdefault(name, {})[path] = {"remote": sha, "at": datetime.now().isoformat(timespec="seconds")}; STATE.write_text(json.dumps(s, indent=2))
def _known(name: str, path: str) -> str: return _state().get(name, {}).get(path, {}).get("remote", "")

def _changed_there(name: str, path: str, remote: str) -> str:
    known = _known(name, path)
    return f" - changed on {name} since this portal last synced it" if known and remote and known != remote else ""

def status_one(name: str, t: dict, path: str, repo: str, url: str, env: dict) -> str:
    cwd = Path(path).resolve()
    if not cwd.is_dir(): return "missing here - Pull clones it"
    if not (cwd / ".git").exists(): return "files here but not a git folder - Pull makes it one (its files kept as a backup commit), Push sends them"
    try: remote, head = _fetch(cwd, f"{url}/{repo}.git", t.get("branch", "main"), env), _head(cwd, env)
    except Missing: return f"""missing on {name}{" - Create & push makes it" if t.get("create_missing", "ask") != "no" else " (create_missing is no)"}"""
    if not remote: return f"empty on {name} - Push fills it"
    dirty, n = _dirty(cwd, env), _differs(cwd, _sent_tree(cwd, t, env) if head else "", _tree(cwd, remote, env), env)
    if not dirty and n == 0: return "same"
    return f"""{f"{dirty} uncommitted change(s); " if dirty else ""}{"same files as" if n == 0 else f"{n} file(s) differ from"} {name}{_changed_there(name, path, remote)}"""

def pull_one(name: str, t: dict, path: str, repo: str, url: str, env: dict, ident: dict) -> str:
    cwd, branch = Path(path).resolve(), t.get("branch", "main")
    if not cwd.is_dir():
        tmp = cwd.parent / f".{cwd.name}.portal-sync"
        cwd.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        rc, out = _git(["clone", "-q", "--no-tags", "-b", branch, f"{url}/{repo}.git", str(tmp)], cwd.parent, env, timeout=900)
        if rc: shutil.rmtree(tmp, ignore_errors=True); return f"nothing on {name} to pull (empty repository)" if "not found in upstream" in out else f"missing on {name} - nothing to pull" if "not found" in out.lower() else f"clone failed: {out[-300:]}"
        tmp.rename(cwd)
        _remember(name, path, _head(cwd, env))
        return "cloned"
    if not (cwd / ".git").exists(): _ok(["init", "-q", "-b", branch], cwd, env)
    try: remote = _fetch(cwd, f"{url}/{repo}.git", branch, env)
    except Missing: return f"missing on {name} - nothing to pull"
    if not remote: return f"nothing on {name} to pull"
    head, dirty = _head(cwd, env), _dirty(cwd, env)
    before = _tree(cwd, head, env)
    if not dirty and before == _tree(cwd, remote, env): _remember(name, path, remote); return "same"
    if not head and not t.get("backup", True): return "not pulled: the files here have no git history and backup is off - turn backup on to keep them as a commit, or move them away"
    if t.get("pull", "replace") == "ff":
        if dirty: return f"not pulled (pull ff): {dirty} uncommitted change(s) here"
        rc, out = _git(["merge", "--ff-only", "-q", remote], cwd, env)
        if rc: return f"not pulled (pull ff): this folder has commits {name} lacks - {out[-200:]}"
    else:
        kept = ""
        if t.get("backup", True) and (dirty or (head and _git(["merge-base", "--is-ancestor", head, remote], cwd, env)[0])):
            _commit_local(cwd, ident, f"backup before pull from {name}", env)
            kept = f"portal-sync-backup/{_stamp()}"
            _ok(["branch", kept, "HEAD"], cwd, env)
        _ok(["checkout", "-q", "-f", "-B", branch, remote], cwd, env)
        if kept: _ok(["clean", "-fdq"], cwd, env)   # untracked files are in the backup commit; ignored files are never touched
        kept = f" - what was here is kept as branch {kept}" if kept else ""
    _remember(name, path, remote)
    return f"""pulled ({_differs(cwd, before, _tree(cwd, "HEAD", env), env) if before else "all"} file(s) changed){kept if t.get("pull", "replace") != "ff" else ""}"""

def push_one(name: str, t: dict, path: str, repo: str, url: str, env: dict, ident: dict, message: str, create: bool = False) -> str:
    cwd, branch = Path(path).resolve(), t.get("branch", "main")
    if not cwd.is_dir(): return "missing here - nothing to push"
    if not (cwd / ".git").exists(): _ok(["init", "-q", "-b", branch], cwd, env)
    committed = _commit_local(cwd, ident, message, env)
    head = _head(cwd, env)
    if not head: return "nothing here to push (empty folder)"
    try: remote = _fetch(cwd, f"{url}/{repo}.git", branch, env)
    except Missing:
        mode = t.get("create_missing", "ask")
        if mode == "no" or not (create or mode == "yes"): return f"""missing on {name} - {"Create & push makes it" if mode != "no" else "create_missing is no: make it on the server"}{" (local changes are committed)" if committed else ""}"""
        err = _create_repo(t, repo)
        if err: return err
        remote = ""
    sending = _snapshot(cwd, t, message, env) if t.get("history") == "snapshot" else head
    if remote and _tree(cwd, sending, env) == _tree(cwd, remote, env): _remember(name, path, remote); return f"""same as {name}{" (local changes committed)" if committed else ""}"""
    force = t.get("push", "force") == "force"
    if remote and not force and _git(["merge-base", "--is-ancestor", remote, head], cwd, env)[0]: return f"not pushed (push safe): {name} has commits this folder lacks - Pull first{' (local changes are committed)' if committed else ''}"
    kept = ""
    if remote and force and remote != _known(name, path) and (t.get("history") == "snapshot" or _git(["merge-base", "--is-ancestor", remote, head], cwd, env)[0]):
        kept = f"portal-sync-replaced/{name}/{_stamp()}"
        _ok(["branch", kept, remote], cwd, env)   # what the push replaces on the target, kept here
    rc, out = _git(["push", "-q", *(["--force"] if force else []), f"{url}/{repo}.git", f"{sending}:refs/heads/{branch}"], cwd, env, timeout=600)
    if rc: return f"push failed: {out[-300:]}"
    _remember(name, path, sending)
    return f"""pushed{" (snapshot)" if t.get("history") == "snapshot" else ""}{" - local changes committed" if committed else ""}{f" - what {name} had is kept here as branch {kept}" if kept else ""}"""

# --- Runs (one at a time per portal) ---

async def run(op: str, name: str, paths: list, user, message: str = "", create: bool = False) -> dict:
    """{"results": {folder: text}, "after": [hook results], "restart": bool} for op status / pull / push on target name over paths (all of its folders when empty)."""
    c = config()
    if name not in targets(c): raise ValueError(f"no target {name} in {CONFIG}")
    if not may_use(c, name, user): raise ValueError(f"{getattr(user, 'username', '?')} may not use target {name} (admin, or listed in the target's users)")
    me = mine(getattr(user, "username", ""))
    ident = me.get("identity") or c.get("users", {}).get(getattr(user, "username", ""))
    if op == "push" and c["targets"][name].get("push") == "none": raise ValueError(f"target {name} is set to push none on this portal (pull only) - change it in the config to push from here")
    if op == "push" and not ident: raise ValueError("""give your commit identity first (name and email, under "You" on the Sync page)""")
    t, rs = c["targets"][name], repos(c, name, op if op in ("push", "pull") else "")
    if op == "push" and t.get("identity", {}).get("email"): ident = t["identity"]   # one portal user, a different identity per server (a work email for the work server): commits made for this push carry the target's identity
    todo = {p: r for p, r in rs.items() if not paths or p in paths}
    refused = {p: f"not on {name}'s {op} list - nothing done" for p in paths if p not in rs}   # a folder asked for that this direction does not cover is said, never acted on
    url, header = _auth(t, (me.get("connections") or {}).get(name, ""))
    ident = ident or {"name": getattr(user, "username", "portal"), "email": f"""{getattr(user, "username", "portal")}@{instance(c)["name"]}.local"""} if op != "push" else ident   # pull's backup commits; push needs a configured identity
    env, msg = _env(header), message or f"""{getattr(user, "username", "")}: update from {instance(c)["name"]} {datetime.now():%Y-%m-%d %H:%M}"""
    fn = {"status": lambda p, r: status_one(name, t, p, r, url, env), "pull": lambda p, r: pull_one(name, t, p, r, url, env, ident), "push": lambda p, r: push_one(name, t, p, r, url, env, ident, msg, create)}[op]
    PROGRESS.clear()
    PROGRESS.update(op=op, target=name, user=getattr(user, "username", ""), total=len(todo), done=0, current=[], started=time.time())
    async def one(p, r):
        PROGRESS["current"].append(p)
        try: return await asyncio.to_thread(fn, p, r)
        except Exception as e: return f"error: {e}"   # one folder's failure is reported in its row; the others go on
        finally: PROGRESS.update(done=PROGRESS["done"] + 1, current=[x for x in PROGRESS["current"] if x != p], last=p)
    async with _LOCK:
        if op == "status":
            sem = asyncio.Semaphore(6)
            async def lim(p, r):
                async with sem: return await one(p, r)
            results = dict(zip(todo, await asyncio.gather(*(lim(p, r) for p, r in todo.items()))))
        else: results = {p: await one(p, r) for p, r in todo.items()}
    results.update(refused)
    moved = [p for p, x in results.items() if x.startswith(("pulled", "cloned", "pushed"))]
    after = [await _hook(h) for h in t.get(f"after_{op}", [])] if moved else []
    restart = op == "pull" and bool(moved) and instance(c)["restart_after_pull"]
    PROGRESS.clear()
    LAST.write_text(json.dumps({"op": op, "target": name, "user": getattr(user, "username", ""), "at": datetime.now().isoformat(timespec="seconds"), "results": results, "after": after, "restart": restart}, indent=2))
    if restart: restart_portal()
    return {"results": results, "after": after, "restart": restart}

async def _hook(h: dict) -> str:
    """{"service", "call", "args"}: a function another tool or module registered (e.g. Kimi's sync of the code knowledge group)."""
    fn = SERVICES.get(h.get("service", ""), {}).get(h.get("call", ""))
    if not fn: return f"""{h.get("service")}.{h.get("call")}: not available on this portal"""
    try:
        r = fn(**h.get("args", {}))
        return f"""{h["service"]}.{h["call"]}: {await r if asyncio.iscoroutine(r) else r}"""
    except Exception as e: return f"""{h["service"]}.{h["call"]} failed: {e}"""

def last() -> dict: return json.loads(LAST.read_text()) if LAST.exists() else {}

# --- Restart ---

def restart_portal(delay_s: float = 2.0):
    """Restarts this portal after delay_s (the reply goes out first). Under uvicorn --reload, touching core_files/main.py makes the reloader start a fresh worker; otherwise the process exits and the container's restart policy starts it again."""
    if time.time() - _RESTART["at"] < delay_s + 1: return   # one restart for a burst of saves
    _RESTART["at"] = time.time()
    if "--reload" in sys.argv: asyncio.get_running_loop().call_later(delay_s, os.utime, "core_files/main.py")
    else: asyncio.get_running_loop().call_later(delay_s, os._exit, 3)

def code_saved(rel_path: str) -> bool:
    """Called by an editor after it saved a file (path from the portal root). On an instance with restart_on_code_change, a file inside one of the configured code folders restarts the portal - except .py files under --reload, which the reloader already picks up. True when a restart was scheduled."""
    c = config()
    if not instance(c)["restart_on_code_change"]: return False
    p = Path(rel_path).as_posix().lstrip("./")
    folders = {f for n in targets(c) for f in repos(c, n)}
    if not any(p == f or p.startswith(f.rstrip("/") + "/") for f in folders) or (p.endswith(".py") and "--reload" in sys.argv): return False
    restart_portal()
    return True
