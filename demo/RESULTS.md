# Demo results — local loopback (2026-06-29)

All self-contained on a Windows 11 dev PC: MediaMTX + ffmpeg SRT publishers +
the controller (headless, no OBS, no cellular, no second physical line).
Telemetry observed live over HTTP (`/snapshot`, `/events` SSE) and the browser
dashboard, and recorded to `demo-run.jsonl`.

## 1. Failover (`--scenario failover`)

```
python run_demo.py --mediamtx <mediamtx.exe> --auto --scenario failover --kill-after 12 --duration 26
```
```
t+000.0s  active=linkA   linkA: - -> GOOD   linkB: - -> GOOD
t+015.1s  active=linkB   linkA: GOOD -> DEAD          # linkA's ffmpeg killed
```
Kill -> ACTIVE=linkB switch: **~1.0 s** (within one poll). This is the *clean
disconnect* path (SRT socket closes -> `ready=false` -> DEAD next poll). A real
link death where the socket stays open but bytes stop is the freeze path
(`FREEZE_POLLS=3`) ≈ 3 s. Browser before/after: `dashboard-1-prekill.png`,
`dashboard-2-postkill.png`.

## 2. Degraded, no flap (`--scenario degraded`)

```
python run_demo.py --mediamtx <mediamtx.exe> --auto --scenario degraded --duration 16
```
```
t+00.0s  linkA=GOOD       0 kbps   active=linkA
t+02.0s  linkA=DEGRADED   263 kbps active=linkA
```
linkA encoded under the 300 kbps threshold -> `DEGRADED`, but it **stays ACTIVE**:
a merely-degraded primary is not abandoned for a lower-priority backup (stability
over flapping).

## 3. Recovery to primary (`--scenario recover`)

```
python run_demo.py --mediamtx <mediamtx.exe> --auto --scenario recover --kill-after 12 --restart-gap 10 --duration 36
```
```
t+00.0s  active=linkA   linkA=GOOD
t+15.2s  active=linkB   linkA=DEAD      # linkA killed -> failover
t+23.2s  active=linkA   linkA=GOOD      # linkA restarted -> switch back
```
The switch back happened **8.0 s** after the failover = `COOLDOWN_S`: the
controller waited out the cooldown before returning to the higher-priority link.

## 4. Freeze (~3 s) via synthetic source (`controller.py --source synthetic`)

No MediaMTX/ffmpeg — a scripted timeline drives the same model. linkA stays
`ready=true` but its `bytesReceived` stops (Δ==0), so the freeze rule fires:
```
t+00.0s  linkA=GOOD              active=linkA
t+07.0s  linkA=DEAD  freeze=3    active=linkB    # bytes stalled, socket open
```
This is the real-link-death path (`FREEZE_POLLS=3` ≈ 3 polls), which a clean
process-kill (scenario 1) can't stage — there the socket closes and `ready`
flips immediately (~1 s). Recorded to `demo/recordings/freeze.jsonl`.

## Offline tests

`python -m unittest discover -s tests -t .` — 16 deterministic tests over
`compute_state()` and `decide()` (GOOD/DEGRADED/DEAD, hysteresis, failover,
failover-to-degraded, no-flap, cooldown recovery, stale-link exclusion). No
network/MediaMTX/OBS required.

## Notes

- RTT/loss were populated by this MediaMTX build on `/v3/srtconns` (loopback RTT
  ≈ 0), confirming the SRT-stats path works on this version.
- Not yet exercised in RelayForge: OBS output switching (code present),
  real senders / multiple physical lines (hardware).
```
