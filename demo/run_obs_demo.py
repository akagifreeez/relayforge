#!/usr/bin/env python3
r"""
RelayForge OBS demo — show the BROADCAST output (OBS) following the healthy link.

Pipeline: two color-coded SRT publishers (linkA = blue, linkB = green) -> MediaMTX
(RTSP enabled) -> OBS media sources -> the controller (OBS enabled) enables only
the ACTIVE link's source. Kill linkA and OBS's program output switches to linkB.

Proof is twofold: OBS program screenshots (before/after) AND the obs-websocket
scene-item enabled states read back from OBS.

Prereqs: OBS running with obs-websocket enabled (Tools -> WebSocket Server Settings),
plus python, ffmpeg, a mediamtx binary, and `pip install obsws-python`.

    python run_obs_demo.py --mediamtx <mediamtx(.exe)> --auto
"""
import os
import sys
import time
import signal
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
CFG = os.path.join(HERE, "mediamtx-obs.yml")
CONTROLLER = os.path.join(ROOT, "controller.py")
SRT = "srt://127.0.0.1:8890?streamid=publish:%s"
COLORS = {"linkA": "0x1f6feb", "linkB": "0x2ea043"}   # blue / green

procs = {}


def spawn(name, cmd, **kw):
    p = subprocess.Popen(cmd, **kw)
    procs[name] = p
    print("  [started] %-11s pid=%d" % (name, p.pid))
    return p


def ffmpeg_cmd(ffmpeg, link, kbps):
    return [
        ffmpeg, "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "color=c=%s:s=640x480:r=30" % COLORS.get(link, "gray"),
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-b:v", "%dk" % kbps, "-pix_fmt", "yuv420p", "-g", "30",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "mpegts", SRT % link,
    ]


def kill(name):
    p = procs.get(name)
    if p and p.poll() is None:
        print("  [KILL]    %s (pid=%d)" % (name, p.pid))
        try:
            p.terminate()
        except Exception:
            pass


def cleanup(*_):
    print("\nstopping OBS demo...")
    for name in list(procs):
        try:
            if procs[name].poll() is None:
                procs[name].terminate()
        except Exception:
            pass
    time.sleep(0.8)
    for name in list(procs):
        try:
            if procs[name].poll() is None:
                procs[name].kill()
        except Exception:
            pass
    sys.exit(0)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mediamtx", required=True, help="path to mediamtx(.exe)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--bitrate", type=int, default=1500)
    ap.add_argument("--auto", action="store_true", help="scripted: kill linkA, screenshot before/after, exit")
    ap.add_argument("--kill-after", type=float, default=12.0)
    ap.add_argument("--duration", type=float, default=24.0)
    args = ap.parse_args()

    import obs_setup  # uses obsws-python; same env config as the controller

    # preflight: OBS reachable?
    try:
        obs_setup.client().get_version()
    except Exception as e:
        print("ERROR: cannot reach OBS websocket on %s:%d (%s)." % (obs_setup.HOST, obs_setup.PORT, type(e).__name__))
        print("Start OBS and enable Tools -> WebSocket Server Settings, then retry.")
        sys.exit(1)

    signal.signal(signal.SIGINT, cleanup)
    print("RelayForge OBS demo - linkA(blue)/linkB(green) -> MediaMTX(RTSP) -> OBS\n")

    spawn("mediamtx", [args.mediamtx, CFG], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    spawn("linkA", ffmpeg_cmd(args.ffmpeg, "linkA", args.bitrate))
    spawn("linkB", ffmpeg_cmd(args.ffmpeg, "linkB", args.bitrate))
    time.sleep(2.0)

    print("\n  creating OBS scene + sources...")
    obs_setup.setup()
    time.sleep(3.0)  # let OBS media sources connect to RTSP and decode

    spawn("controller", [
        sys.executable, CONTROLLER, "--no-supervise", "--headless",
        "--api", "http://127.0.0.1:9997/v3",
        "--http-port", str(args.http_port),
        "--jsonl", os.path.join(HERE, "obs-run.jsonl"),
        "--logfile", os.path.join(HERE, "obs-controller.log"),
    ])
    print("\n  dashboard: http://127.0.0.1:%d   (OBS program follows the active link)" % args.http_port)

    if not args.auto:
        print("  -> kill linkA's ffmpeg to see OBS switch to green. Ctrl+C to stop.")
        while True:
            time.sleep(1)

    # scripted: before -> kill -> after, with screenshots + obs-websocket state read
    time.sleep(args.kill_after)
    obs_setup.shot(os.path.join(HERE, "obs-before.png"))
    b_state = dict((n, en) for n, en, _ in obs_setup.items())
    print("  [before] OBS enabled:", b_state)

    kill("linkA")
    time.sleep(max(4.0, args.duration - args.kill_after))
    obs_setup.shot(os.path.join(HERE, "obs-after.png"))
    a_state = dict((n, en) for n, en, _ in obs_setup.items())
    print("  [after]  OBS enabled:", a_state)

    ok = b_state.get("linkA") and a_state.get("linkB") and not a_state.get("linkA")
    print("\n  RESULT:", "PASS - OBS program followed the failover (linkA -> linkB)" if ok
          else "CHECK - states: before=%s after=%s" % (b_state, a_state))
    cleanup()


if __name__ == "__main__":
    main()
