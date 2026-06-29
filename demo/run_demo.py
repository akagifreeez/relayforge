#!/usr/bin/env python3
r"""
RelayForge local failover demo — no cellular, no real lines, no OBS required.

Launches:
  - MediaMTX (loopback, SRT + API)        via mediamtx-loopback.yml
  - two ffmpeg SRT publishers: linkA (priority) and linkB (backup)
  - the RelayForge controller (--no-supervise --headless) with telemetry on :8080

Then open http://127.0.0.1:8080 and KILL linkA's ffmpeg (or use --auto) to watch
the controller fail over to linkB in ~3s, live, in the dashboard + JSONL.

Examples:
  python run_demo.py --mediamtx ../../carstream/mediamtx/mediamtx.exe
  python run_demo.py --mediamtx <path> --auto         # scripted: kill linkA, observe, exit
"""
import os
import sys
import time
import signal
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = os.path.join(HERE, "mediamtx-loopback.yml")
CONTROLLER = os.path.join(ROOT, "controller.py")
SRT = "srt://127.0.0.1:8890?streamid=publish:%s"

procs = {}


def spawn(name, cmd, **kw):
    p = subprocess.Popen(cmd, **kw)
    procs[name] = p
    print("  [started] %-10s pid=%d" % (name, p.pid))
    return p


def ffmpeg_cmd(ffmpeg, link, kbps):
    return [
        ffmpeg, "-loglevel", "error", "-re",
        "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30",
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
    print("\nstopping demo...")
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--mediamtx", required=True, help="path to mediamtx(.exe)")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg binary (default: PATH)")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--jsonl", default=os.path.join(HERE, "demo-run.jsonl"))
    ap.add_argument("--bitrate", type=int, default=1500)
    ap.add_argument("--auto", action="store_true", help="run a scripted scenario, then exit")
    ap.add_argument("--scenario", choices=["failover", "recover", "degraded"], default="failover",
                    help="failover=kill linkA; recover=kill then restart linkA; degraded=linkA at low bitrate")
    ap.add_argument("--kill-after", type=float, default=12.0)
    ap.add_argument("--restart-gap", type=float, default=10.0, help="recover: seconds after kill before linkA restarts")
    ap.add_argument("--duration", type=float, default=22.0)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    signal.signal(signal.SIGINT, cleanup)
    print("RelayForge demo - MediaMTX + 2 SRT publishers + controller\n")

    spawn("mediamtx", [args.mediamtx, CFG],
          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)  # let the API/SRT listener come up

    a_kbps = 150 if args.scenario == "degraded" else args.bitrate   # 150+audio < DEGRADE_BITRATE_KBPS(300)
    spawn("linkA", ffmpeg_cmd(args.ffmpeg, "linkA", a_kbps))
    spawn("linkB", ffmpeg_cmd(args.ffmpeg, "linkB", args.bitrate))
    time.sleep(1.5)

    spawn("controller", [
        sys.executable, CONTROLLER, "--no-supervise", "--headless", "--no-obs",
        "--api", "http://127.0.0.1:9997/v3",
        "--http-port", str(args.http_port),
        "--jsonl", args.jsonl,
        "--logfile", os.path.join(HERE, "demo-controller.log"),
    ])

    print("\n  dashboard : http://127.0.0.1:%d" % args.http_port)
    print("  jsonl     : %s" % args.jsonl)
    print("  -> kill linkA's ffmpeg to trigger failover (or run with --auto)\n")

    if not args.auto:
        print("  Ctrl+C to stop.")
        while True:
            time.sleep(1)

    # scripted scenarios
    if args.scenario == "degraded":
        print("  [auto] degraded: linkA at %dkbps (< 300), running %ss" % (a_kbps, args.duration))
        time.sleep(args.duration)
    elif args.scenario == "recover":
        print("  [auto] recover: kill linkA @%ss, restart @%ss" % (
            args.kill_after, args.kill_after + args.restart_gap))
        time.sleep(args.kill_after)
        kill("linkA")
        time.sleep(args.restart_gap)
        spawn("linkA", ffmpeg_cmd(args.ffmpeg, "linkA", args.bitrate))   # bring the primary back
        time.sleep(max(1.0, args.duration - args.kill_after - args.restart_gap))
    else:  # failover
        print("  [auto] failover: kill linkA @%ss, run %ss" % (args.kill_after, args.duration))
        time.sleep(args.kill_after)
        kill("linkA")
        time.sleep(max(1.0, args.duration - args.kill_after))
    cleanup()


if __name__ == "__main__":
    main()
