#!/usr/bin/env python3
r"""
RelayForge controller — multi-link SRT failover controller with live telemetry.

Heart of this file is a deterministic, threshold-based health state machine
(GOOD / DEGRADED / DEAD with hysteresis + cooldown) that polls a MediaMTX
instance, scores each publish path, and picks the single ACTIVE link, optionally
driving OBS scene-item visibility so the broadcast follows the healthy link.

That state machine is the original car-stream controller (see upstream repo
akagifreeez/mediamtx-failover-controller). RelayForge adds, on top of it:
  - configuration via env / CLI (no hardcoded machine paths),
  - a JSONL telemetry stream (one snapshot per poll) — the schema contract,
  - a stdlib Server-Sent-Events HTTP server so a browser dashboard
    (web/index.html) can render link health + failover live.

No AI/ML: compute_state() is pure threshold logic.

Run:
    python controller.py --no-supervise            # watch an existing MediaMTX
    python controller.py --headless --no-supervise # no TUI (for the demo / CI)
Config (all overridable by env RELAYFORGE_* or CLI; see --help):
    --api http://127.0.0.1:9997/v3   --http-port 8080   --jsonl run.jsonl
"""
import os
import sys
import json
import time
import queue
import argparse
import threading
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- config (defaults; overridden by env/CLI in configure()) ----------------
MEDIAMTX_EXE = os.environ.get("RELAYFORGE_MEDIAMTX_EXE", "mediamtx")
MEDIAMTX_CFG = os.environ.get("RELAYFORGE_MEDIAMTX_CFG", "")
API = os.environ.get("RELAYFORGE_API", "http://127.0.0.1:9997/v3")
LOGFILE = os.environ.get("RELAYFORGE_LOGFILE", "relayforge.log")
JSONL_FILE = os.environ.get("RELAYFORGE_JSONL", "")          # "" = no JSONL file
HTTP_ENABLE = True
HTTP_HOST = os.environ.get("RELAYFORGE_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("RELAYFORGE_HTTP_PORT", "8080"))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
POLL = float(os.environ.get("RELAYFORGE_POLL", "1.0"))

# Watched publish-path priority (earlier = preferred). Unknown paths append at the tail.
PRIORITY = ["linkA", "linkB", "car", "phone"]

# health thresholds
FREEZE_POLLS = 3          # bytesReceived Δ==0 for N consecutive polls => DEAD (freeze)
OFFLINE_POLLS = 2         # ready==false for N consecutive polls => DEAD
DEGRADE_BITRATE_KBPS = 300
DEGRADE_RTT_MS = 2500
DEGRADE_LOSS_PCT = 5.0
DEGRADE_POLLS = 3         # degrade conditions for N consecutive polls => DEGRADED
COOLDOWN_S = 8.0          # suppress reverse/other switches for N s after a switch (DEAD is exempt)

# OBS (obs-websocket). If unavailable it is skipped automatically (decision still runs).
OBS_ENABLE = os.environ.get("RELAYFORGE_OBS", "1") not in ("0", "false", "False", "")
OBS_HOST = os.environ.get("RELAYFORGE_OBS_HOST", "localhost")
OBS_PORT = int(os.environ.get("RELAYFORGE_OBS_PORT", "4455"))
OBS_PASSWORD = os.environ.get("RELAYFORGE_OBS_PASSWORD", "")
OBS_SCENE = os.environ.get("RELAYFORGE_OBS_SCENE", "RelayForge")


# path name -> OBS source name (default: same)
def obs_source_name(path):
    return path


# ---------------- state ----------------
_lock = threading.RLock()
_stop = threading.Event()
_proc = None

paths = {}        # name -> PathHealth
active = None      # current active path name
last_switch = 0.0
g = {"mtx": "DOWN", "spawns": 0, "api_ok": False, "last_log": "", "obs": "off"}


def logline(msg):
    line = time.strftime("%H:%M:%S") + "  " + msg
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


class PathHealth:
    def __init__(self, name):
        self.name = name
        self.ready = False
        self.bitrate = 0.0
        self.bytes = None
        self.prev_bytes = None
        self.prev_t = None
        self.rtt = None
        self.loss = None
        self.tracks = "-"
        self.readers = 0
        self.online_since = None
        self.freeze = 0
        self.offline = 0
        self.degrade = 0
        self.state = "DEAD"
        self.last_seen = 0.0

    def update_from_path(self, item, now):
        self.last_seen = now
        ready = bool(item.get("ready"))
        if ready:
            self.offline = 0
            if self.online_since is None:
                self.online_since = now
            self.tracks = ", ".join(item.get("tracks", [])) or "-"
            self.readers = len(item.get("readers", []))
            br = item.get("bytesReceived", 0)
            if self.prev_bytes is not None and self.prev_t is not None and now > self.prev_t:
                if br >= self.prev_bytes:
                    delta = br - self.prev_bytes
                    self.bitrate = max(0.0, delta * 8 / (now - self.prev_t) / 1000.0)
                    self.freeze = self.freeze + 1 if delta == 0 else 0
            self.prev_bytes, self.prev_t = br, now
            self.ready = True
        else:
            self.offline += 1
            self.ready = False
            self.bitrate = 0.0
            self.online_since = None
            self.prev_bytes = None
            self.freeze = 0
            self.readers = 0

    def update_srt(self, conn):
        # Pull SRT stats from MediaMTX /v3/srtconns items if present (else stay None).
        for k in ("msRTT", "rtt", "roundTripTime"):
            if k in conn:
                try:
                    self.rtt = float(conn[k]); break
                except Exception:
                    pass
        for k in ("packetsReceivedLossRate", "packetsLossRate"):
            if k in conn:
                try:
                    self.loss = float(conn[k]) * (100.0 if conn[k] <= 1 else 1.0); break
                except Exception:
                    pass

    def compute_state(self):
        # DEAD conditions
        if self.offline >= OFFLINE_POLLS:
            self.state = "DEAD"; self.degrade = 0; return
        if self.freeze >= FREEZE_POLLS:
            self.state = "DEAD"; self.degrade = 0; return
        if not self.ready:
            self.state = "DEAD"; self.degrade = 0; return
        # DEGRADED conditions (consecutive)
        bad = (self.bitrate < DEGRADE_BITRATE_KBPS)
        if self.rtt is not None and self.rtt > DEGRADE_RTT_MS:
            bad = True
        if self.loss is not None and self.loss > DEGRADE_LOSS_PCT:
            bad = True
        if bad:
            self.degrade += 1
        else:
            self.degrade = 0
        self.state = "DEGRADED" if self.degrade >= DEGRADE_POLLS else "GOOD"


def prio(name):
    return PRIORITY.index(name) if name in PRIORITY else len(PRIORITY) + hash(name) % 1000


# ---------------- OBS ----------------
_obs = None


def obs_connect():
    global _obs
    if not OBS_ENABLE:
        g["obs"] = "disabled"; return
    try:
        import obsws_python as o
        _obs = o.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=3)
        _obs.get_version()
        g["obs"] = "connected"
        logline("OBS connected")
    except Exception as e:
        _obs = None
        g["obs"] = "off(%s)" % type(e).__name__


def obs_apply(active_name):
    """Enable only the active source, disable others. No-op if OBS not connected."""
    if _obs is None:
        return
    try:
        scene = OBS_SCENE
        items = _obs.get_scene_item_list(scene).scene_items
        for it in items:
            src = it["sourceName"]
            iid = it["sceneItemId"]
            want = (src == obs_source_name(active_name))
            if it.get("sceneItemEnabled") != want:
                _obs.set_scene_item_enabled(scene, iid, want)
    except Exception as e:
        g["obs"] = "err(%s)" % type(e).__name__


# ---------------- decision ----------------
def decide():
    global active, last_switch
    now = time.time()
    alive = {n: p for n, p in paths.items() if (now - p.last_seen) < 5}
    goods = sorted([n for n, p in alive.items() if p.state == "GOOD"], key=prio)
    cur = paths.get(active)

    new_active = active
    reason = None
    if active is None or active not in alive or (cur and cur.state == "DEAD"):
        # emergency/initial: take highest-priority GOOD now (else highest DEGRADED, else last alive)
        if goods:
            new_active = goods[0]; reason = "failover->GOOD"
        else:
            degs = sorted([n for n, p in alive.items() if p.state == "DEGRADED"], key=prio)
            if degs:
                new_active = degs[0]; reason = "failover->DEGRADED(no GOOD)"
            elif alive:
                new_active = sorted(alive.keys(), key=prio)[0]; reason = "failover->last-alive"
            else:
                new_active = None; reason = "no-source"
    elif cur and cur.state == "DEGRADED":
        # degraded: after cooldown, move/return to a higher-priority GOOD
        if goods and (now - last_switch) > COOLDOWN_S:
            if prio(goods[0]) <= prio(active):
                new_active = goods[0]; reason = "degraded->better GOOD"
    else:
        # GOOD & stable: after cooldown, return to a higher-priority GOOD (prefer primary)
        if goods and (now - last_switch) > COOLDOWN_S and prio(goods[0]) < prio(active):
            new_active = goods[0]; reason = "prefer higher-priority GOOD"

    if new_active != active:
        logline("ACTIVE %s -> %s  (%s)" % (active, new_active, reason))
        active = new_active
        last_switch = now
        obs_apply(active)


# ---------------- telemetry: snapshot + JSONL + SSE  (RelayForge addition) ----------------
_sse_clients = set()
_sse_lock = threading.Lock()
_jsonl_fh = None


def build_snapshot(now):
    """Thread-safe snapshot of all links + the active one. The schema CONTRACT."""
    with _lock:
        snap_active = active
        links = []
        for n in sorted(paths, key=prio):
            p = paths[n]
            links.append({
                "name": n,
                "state": p.state,
                "bitrate_kbps": round(p.bitrate, 1),
                "freeze": p.freeze,
                "rtt_ms": round(p.rtt, 2) if p.rtt is not None else None,
                "loss_pct": round(p.loss, 2) if p.loss is not None else None,
                "readers": p.readers,
                "uptime_s": int(now - p.online_since) if p.online_since else None,
            })
    return {"ts": round(now, 3), "active": snap_active, "api_ok": g.get("api_ok", False), "links": links}


def telemetry_emit(now):
    """Write one JSONL line and push to all SSE clients. Never blocks the poll loop."""
    snap = build_snapshot(now)
    line = json.dumps(snap, ensure_ascii=False)
    if _jsonl_fh is not None:
        try:
            _jsonl_fh.write(line + "\n")
            _jsonl_fh.flush()
        except Exception:
            pass
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(line)
            except queue.Full:
                dead.append(q)  # slow client: drop it rather than stall the loop
        for q in dead:
            _sse_clients.discard(q)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._serve_sse()
        elif path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/snapshot":
            body = json.dumps(build_snapshot(time.time()), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self):
        q = queue.Queue(maxsize=64)
        with _sse_lock:
            _sse_clients.add(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            # prime with the current snapshot so a fresh client renders immediately
            self.wfile.write(("data: %s\n\n" % json.dumps(build_snapshot(time.time()), ensure_ascii=False)).encode("utf-8"))
            self.wfile.flush()
            while not _stop.is_set():
                try:
                    line = q.get(timeout=5)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # comment frame keeps the connection warm
                    self.wfile.flush()
                    continue
                self.wfile.write(("data: %s\n\n" % line).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                _sse_clients.discard(q)


_httpd = None


def serve_http():
    global _httpd
    try:
        _httpd = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), _Handler)
    except OSError as e:
        logline("HTTP server bind failed on %s:%d (%s)" % (HTTP_HOST, HTTP_PORT, e))
        return
    logline("telemetry http://%s:%d  (/ dashboard, /events SSE, /snapshot)" % (HTTP_HOST, HTTP_PORT))
    _httpd.serve_forever(poll_interval=0.5)


# ---------------- poll ----------------
def api_get(path, opener):
    with opener.open(API + path, timeout=2) as r:
        return json.load(r)


def poll():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    prev_states = {}
    while not _stop.is_set():
        now = time.time()
        try:
            data = api_get("/paths/list", opener)
            with _lock:
                g["api_ok"] = True
                seen = set()
                for item in data.get("items", []):
                    name = item.get("name")
                    if not name:
                        continue
                    seen.add(name)
                    p = paths.get(name) or PathHealth(name)
                    paths[name] = p
                    p.update_from_path(item, now)
                # SRT stats
                try:
                    conns = api_get("/srtconns/list", opener)
                    for c in conns.get("items", []):
                        if c.get("state") == "publish" and c.get("path") in paths:
                            paths[c["path"]].update_srt(c)
                except Exception:
                    pass
                # compute state + log changes
                for name, p in paths.items():
                    if name not in seen:
                        p.offline += 1
                        p.ready = False
                    p.compute_state()
                    if prev_states.get(name) != p.state:
                        logline("PATH %s: %s -> %s (br=%.0fkb/s freeze=%d off=%d)" % (
                            name, prev_states.get(name, "-"), p.state, p.bitrate, p.freeze, p.offline))
                        prev_states[name] = p.state
                decide()
        except Exception as e:
            with _lock:
                g["api_ok"] = False
                g["last_log"] = "API err: %s" % (str(e)[:80])
        telemetry_emit(now)  # RelayForge: push JSONL + SSE every poll (outside is fine; takes its own lock)
        time.sleep(POLL)


# ---------------- supervise (optional) ----------------
def mtx_reader(proc):
    for raw in iter(proc.stdout.readline, b""):
        if _stop.is_set():
            break
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            with _lock:
                g["last_log"] = line[:140]


def supervise():
    global _proc
    while not _stop.is_set():
        with _lock:
            g["spawns"] += 1; g["mtx"] = "UP"
        logline("MediaMTX starting")
        try:
            cmd = [MEDIAMTX_EXE] + ([MEDIAMTX_CFG] if MEDIAMTX_CFG else [])
            _proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except FileNotFoundError:
            with _lock:
                g["mtx"] = "DOWN"
            logline("mediamtx not found: %s" % MEDIAMTX_EXE)
            time.sleep(3); continue
        threading.Thread(target=mtx_reader, args=(_proc,), daemon=True).start()
        _proc.wait()
        if _stop.is_set():
            break
        with _lock:
            g["mtx"] = "DOWN"
        logline("MediaMTX exited -> restart")
        time.sleep(1.5)


# ---------------- dashboard (TUI) ----------------
def _up(t0):
    if not t0:
        return "-"
    s = int(time.time() - t0)
    return "%d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


COL = {"GOOD": "\033[32m", "DEGRADED": "\033[33m", "DEAD": "\033[31m"}


def render():
    while not _stop.is_set():
        with _lock:
            snap = [(n, paths[n]) for n in sorted(paths, key=prio)]
            a = active
            gg = dict(g)
        out = ["\033[2J\033[H",
               "=== RelayForge controller (multi-link SRT failover) ===  " + time.strftime("%H:%M:%S"),
               "  MediaMTX:%s spawns:%d   API:%s   OBS:%s   HTTP::%d" % (
                   gg["mtx"], gg["spawns"], "ok" if gg["api_ok"] else "NG", gg["obs"], HTTP_PORT),
               "  active : \033[36m%s\033[0m" % (a or "-(none)"),
               "",
               "  %-10s %-9s %9s %6s %7s %6s %5s %s" % (
                   "PATH", "STATE", "bitrate", "frz", "rtt", "loss", "rdr", "uptime")]
        for n, p in snap:
            c = COL.get(p.state, "")
            mark = "►" if n == a else " "
            out.append("  %s%-9s %s%-9s\033[0m %7.0fkb %5d %6s %5s %5d %s" % (
                mark, n, c, p.state, p.bitrate, p.freeze,
                ("%.0f" % p.rtt) if p.rtt is not None else "-",
                ("%.1f" % p.loss) if p.loss is not None else "-",
                p.readers, _up(p.online_since)))
        if not snap:
            out.append("  (waiting for a publisher... send streamid=publish:<name>)")
        out += ["", "  log: %s" % LOGFILE, "  mediamtx: %s" % gg["last_log"],
                "  (Ctrl+C to stop)"]
        sys.stdout.write("\n".join(out) + "\n")
        sys.stdout.flush()
        time.sleep(1)


# ---------------- config ----------------
def configure(argv):
    """Populate the module globals from env defaults + CLI. Keeps the threading
    model untouched (we only reassign module-level values at startup)."""
    global MEDIAMTX_EXE, MEDIAMTX_CFG, API, LOGFILE, JSONL_FILE, POLL
    global HTTP_ENABLE, HTTP_HOST, HTTP_PORT, PRIORITY
    global FREEZE_POLLS, OFFLINE_POLLS, DEGRADE_BITRATE_KBPS, DEGRADE_RTT_MS
    global DEGRADE_LOSS_PCT, DEGRADE_POLLS, COOLDOWN_S
    global OBS_ENABLE, OBS_HOST, OBS_PORT, OBS_PASSWORD, OBS_SCENE

    p = argparse.ArgumentParser(description="RelayForge multi-link SRT failover controller")
    p.add_argument("--no-supervise", action="store_true", help="watch an existing MediaMTX (do not spawn it)")
    p.add_argument("--headless", action="store_true", help="no TUI (telemetry still served)")
    p.add_argument("--api", default=API, help="MediaMTX API base (default %(default)s)")
    p.add_argument("--mediamtx-exe", default=MEDIAMTX_EXE)
    p.add_argument("--mediamtx-cfg", default=MEDIAMTX_CFG)
    p.add_argument("--logfile", default=LOGFILE)
    p.add_argument("--jsonl", default=JSONL_FILE, help="write one snapshot per poll to this file")
    p.add_argument("--http-host", default=HTTP_HOST)
    p.add_argument("--http-port", type=int, default=HTTP_PORT)
    p.add_argument("--no-http", action="store_true", help="disable the telemetry HTTP/SSE server")
    p.add_argument("--poll", type=float, default=POLL)
    p.add_argument("--priority", default=",".join(PRIORITY), help="comma-separated path priority")
    p.add_argument("--no-obs", action="store_true", help="disable OBS websocket control")
    args = p.parse_args(argv)

    API = args.api
    MEDIAMTX_EXE = args.mediamtx_exe
    MEDIAMTX_CFG = args.mediamtx_cfg
    LOGFILE = args.logfile
    JSONL_FILE = args.jsonl
    HTTP_HOST = args.http_host
    HTTP_PORT = args.http_port
    HTTP_ENABLE = not args.no_http
    POLL = args.poll
    PRIORITY = [s.strip() for s in args.priority.split(",") if s.strip()]
    if args.no_obs:
        OBS_ENABLE = False
    return args, HTTP_ENABLE


def main():
    args, http_enable = configure(sys.argv[1:])
    global _jsonl_fh
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logline("=== RelayForge start (supervise=%s headless=%s) ===" % (not args.no_supervise, args.headless))
    if JSONL_FILE:
        try:
            _jsonl_fh = open(JSONL_FILE, "a", encoding="utf-8")
            logline("JSONL -> %s" % JSONL_FILE)
        except OSError as e:
            logline("JSONL open failed: %s" % e)
    obs_connect()
    if http_enable:
        threading.Thread(target=serve_http, daemon=True).start()
    if not args.no_supervise:
        threading.Thread(target=supervise, daemon=True).start()
    threading.Thread(target=poll, daemon=True).start()
    if not args.headless:
        threading.Thread(target=render, daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        _stop.set()
        logline("=== RelayForge stop ===")
        if _httpd is not None:
            try:
                _httpd.shutdown()
            except Exception:
                pass
        if _proc and _proc.poll() is None:
            try:
                _proc.terminate()
            except Exception:
                pass
        if _jsonl_fh is not None:
            try:
                _jsonl_fh.close()
            except Exception:
                pass
        time.sleep(0.6)
        sys.stdout.write("\nstopped.\n")


if __name__ == "__main__":
    main()
