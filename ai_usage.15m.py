#!/usr/bin/env python3
# <xbar.title>Claude + Codex Token Usage</xbar.title>
# <xbar.version>v13.0</xbar.version>
# <xbar.author>Stepan Suvorov</xbar.author>
# <xbar.author.github>stevermeister</xbar.author.github>
# <xbar.dependencies>python3,codex</xbar.dependencies>
# <xbar.abouturl>https://github.com/stevermeister/xbar-claude-codex-usage</xbar.abouturl>
# <xbar.desc>Claude Code usage (Anthropic OAuth API) + Codex usage (codex app-server), each shown with its binding rate-limit window</xbar.desc>

import json, urllib.request, urllib.error, os, time, subprocess, shutil
from datetime import datetime

CACHE_FILE       = os.path.expanduser("~/.claude_usage_cache.json")
CODEX_CACHE_FILE = os.path.expanduser("~/.codex_usage_cache.json")
CACHE_TTL        = 800

def get_token():
    # Credentials file (Linux-style / older installs)
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json")) as f:
            return json.load(f).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        pass
    # macOS Keychain
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True
        )
        if r.returncode == 0:
            return json.loads(r.stdout.strip()).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        pass
    return None

def read_cache(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None

def fresh_cache(path):
    c = read_cache(path)
    if c and time.time() - c.get("_ts", 0) < CACHE_TTL:
        return c
    return None

def write_cache(path, data):
    data["_ts"] = time.time()
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    return data

def get_usage_cached(token):
    cached = fresh_cache(CACHE_FILE)
    if cached:
        return cached
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Accept": "application/json",
                "User-Agent": "xbar-claude-codex-usage/1.0",
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return write_cache(CACHE_FILE, json.loads(resp.read()))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            stale = read_cache(CACHE_FILE)
            if stale:
                return stale
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"error": str(e)}

# ---------------------------------------------------------------- Codex

def codex_bin():
    p = shutil.which("codex")
    if p:
        return p
    for c in ("~/.npm-global/bin/codex", "/opt/homebrew/bin/codex",
              "/usr/local/bin/codex", "~/.local/bin/codex"):
        c = os.path.expanduser(c)
        if os.path.exists(c):
            return c
    return None

def codex_rpc_rate_limits(binary):
    """Ask `codex app-server` for live rate limits over JSON-RPC/stdio."""
    # xbar runs with a bare PATH; the npm `codex` shim needs `node` on it.
    env = dict(os.environ)
    env["PATH"] = ":".join([os.path.dirname(binary), "/usr/local/bin",
                            "/opt/homebrew/bin", env.get("PATH", "/usr/bin:/bin")])
    p = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1, env=env,
    )
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"clientInfo": {"name": "xbar", "title": "xbar", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 1, "method": "account/rateLimits/read", "params": {}},
        ):
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()

        deadline = time.time() + 15
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except Exception:
                continue
            if m.get("id") == 1:
                if "error" in m:
                    raise RuntimeError(m["error"].get("message", "rpc error"))
                return m.get("result") or {}
        raise RuntimeError("timeout")
    finally:
        p.kill()

def get_codex_cached():
    cached = fresh_cache(CODEX_CACHE_FILE)
    if cached:
        return cached
    binary = codex_bin()
    if not binary:
        return {"error": "codex CLI not found"}
    try:
        return write_cache(CODEX_CACHE_FILE, codex_rpc_rate_limits(binary))
    except Exception as e:
        stale = read_cache(CODEX_CACHE_FILE)
        if stale:
            stale["_stale_err"] = str(e)
            return stale
        return {"error": str(e)}

# ---------------------------------------------------------------- format

def status_dot(pct):
    if pct < 60: return "🟢"
    if pct < 85: return "🟡"
    return "🔴"

def fmt_reset(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M")
    except Exception:
        return "?"

def fmt_reset_epoch(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%a %H:%M")
    except Exception:
        return "?"

def window_label(mins):
    if not mins:
        return "window"
    if mins % 1440 == 0: return f"{mins // 1440}d"
    if mins % 60 == 0:   return f"{mins // 60}h"
    return f"{mins}m"

def age_of(data):
    return int((time.time() - data.get("_ts", time.time())) / 60)

# ---------------------------------------------------------------- windows

def claude_windows(data):
    """[(label, pct, reset_str)] for every window Anthropic reports."""
    out = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d"),
                       ("seven_day_sonnet", "7d·S"), ("seven_day_opus", "7d·O")):
        w = data.get(key) or {}
        pct = w.get("utilization")
        if pct is None:
            continue
        out.append((label, pct, fmt_reset(w.get("resets_at", ""))))
    return out

def codex_windows(rl):
    """[(label, pct, reset_str)] for every window Codex reports.

    Codex Pro currently exposes only a weekly `primary`; `secondary` (the old
    5h window, dropped mid-2026) is None. Both are handled so a returning 5h
    window shows up with no code change.
    """
    out = []
    for key in ("secondary", "primary"):
        w = rl.get(key) or {}
        pct = w.get("usedPercent")
        if pct is None:
            continue
        out.append((window_label(w.get("windowDurationMins")), pct,
                    fmt_reset_epoch(w.get("resetsAt"))))
    return out

def binding(windows):
    """The window closest to throttling — the one worth putting in the menu bar."""
    return max(windows, key=lambda w: w[1]) if windows else None

def slot(tag, windows):
    b = binding(windows)
    if not b:
        return f"{tag} ⚠️"
    label, pct, _ = b
    return f"{tag} {status_dot(pct)} {label} {pct:.0f}%"

def matrix_rows(claude_ws, codex_ws):
    """Aligned table so the windows line up and missing ones are explicit."""
    cols = []
    for label, _, _ in claude_ws + codex_ws:
        if label not in cols:
            cols.append(label)
    if not cols:
        return []

    # Every cell is 6 display columns. The status emoji takes two of them
    # while counting as one character, so the dash cell pads to 6 chars.
    def cell(ws, label):
        for l, pct, _ in ws:
            if l == label:
                return f"{status_dot(pct)}{pct:>3.0f}%"
        return "     —"

    rows = ["        " + "  ".join(f"{c:>6}" for c in cols)]
    for name, ws in (("Claude", claude_ws), ("Codex ", codex_ws)):
        rows.append(name + "  " + "  ".join(cell(ws, c) for c in cols))
    return rows

# ---------------------------------------------------------------- render

claude_ws, claude_detail = [], []
token = get_token()
if not token:
    claude_detail.append("⚠️ No Claude token")
else:
    data = get_usage_cached(token)
    if "error" in data:
        claude_detail.append(f"⚠️ {data['error']}")
    else:
        claude_ws = claude_windows(data)
        for label, pct, reset in claude_ws:
            claude_detail.append(f"{status_dot(pct)} {label:<5} {pct:>3.0f}%  ↺ {reset}")
        extra = data.get("extra_usage") or {}
        claude_detail.append(f"Extra usage: {'✅ enabled' if extra.get('is_enabled') else '❌ disabled'}")
        claude_detail.append(f"Updated: {age_of(data)}m ago")

codex_ws, codex_detail = [], []
cx = get_codex_cached()
if "error" in cx:
    codex_detail.append(f"⚠️ {cx['error']}")
else:
    rl      = cx.get("rateLimits") or {}
    credits = rl.get("credits") or {}
    resets  = cx.get("rateLimitResetCredits") or {}

    codex_ws = codex_windows(rl)
    for label, pct, reset in codex_ws:
        codex_detail.append(f"{status_dot(pct)} {label:<5} {pct:>3.0f}%  ↺ {reset}")
    codex_detail.append(f"Plan: {rl.get('planType') or '?'}")
    if credits.get("unlimited"):
        codex_detail.append("Credits: ♾️ unlimited")
    elif credits.get("hasCredits"):
        codex_detail.append(f"Credits: {credits.get('balance', '?')}")
    if resets.get("availableCount"):
        codex_detail.append(f"Limit resets available: {resets['availableCount']}")
    if rl.get("spendControlReached"):
        codex_detail.append("🔴 Spend control reached")
    if rl.get("rateLimitReachedType"):
        codex_detail.append(f"🔴 {rl['rateLimitReachedType']}")
    if cx.get("_stale_err"):
        codex_detail.append(f"⚠️ stale ({cx['_stale_err']})")
    codex_detail.append(f"Updated: {age_of(cx)}m ago")

print(f"{slot('CC', claude_ws)}  ⬩  {slot('CX', codex_ws)}")
print("---")
for r in matrix_rows(claude_ws, codex_ws):
    print(f"{r} | font=Menlo")
if claude_ws or codex_ws:
    print("---")
print("Claude Code")
for l in claude_detail:
    print(f"{l} | font=Menlo")
print("---")
print("Codex")
for l in codex_detail:
    print(f"{l} | font=Menlo")
