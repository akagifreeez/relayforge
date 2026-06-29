# Demo result — local loopback failover (2026-06-29)

Self-contained run on a Windows 11 dev PC (MediaMTX + 2 ffmpeg SRT publishers +
the controller, headless, no OBS, no cellular, no second physical line):

```
python run_demo.py --mediamtx <mediamtx.exe> --auto --kill-after 12 --duration 26
```

Telemetry observed live over HTTP (`/snapshot`, `/events` SSE) and recorded to
`demo-run.jsonl` (26 snapshots). State/active transitions:

```
t+000.0s  active=linkA   linkA: - -> GOOD   linkB: - -> GOOD
t+015.1s  active=linkB   linkA: GOOD -> DEAD          # linkA's ffmpeg killed
```

Measured kill -> ACTIVE=linkB switch: **~1.0 s** (within one poll).

Notes:
- This is the *clean disconnect* path (ffmpeg terminates, SRT socket closes →
  `ready=false` → DEAD on the next poll). A real link death where the socket
  stays open but bytes stop is the freeze path (`FREEZE_POLLS=3`) ≈ 3 s.
- RTT/loss were populated by this MediaMTX build on `/v3/srtconns` (loopback RTT
  ≈ 0), confirming the SRT-stats path works on this version.
```
