# RelayForge

A multi-link **SRT failover controller** with live telemetry. It polls a
[MediaMTX](https://github.com/bluenviron/mediamtx) instance, scores each publish
link's health with a deterministic state machine, picks the single **ACTIVE**
link, and (optionally) drives OBS so the broadcast follows the healthy link —
then streams that health to a browser **Mission Control** dashboard.

> Status: failover core + telemetry + dashboard + a self-contained local demo.
> This is the v0/tracer slice of a larger plan (live recording + richer UI to come).

## What this is / what it isn't

- **Deterministic, threshold-based.** No AI/ML — `compute_state()` is pure
  threshold logic: GOOD / DEGRADED / DEAD with hysteresis (`FREEZE_POLLS`,
  `OFFLINE_POLLS`, `DEGRADE_POLLS`) and a switch `COOLDOWN`.
- **Failover (proven locally).** Killing one of several SRT publishers into the
  real MediaMTX + controller stack triggers an automatic switch to the next
  healthy link. In the loopback demo below this is measured at **~1 poll (~1–2 s)**
  for a *clean* disconnect (the SRT socket closes). A *real* link death where
  packets stop but the socket stays open is caught by the freeze rule
  (`FREEZE_POLLS=3`) in **~3 s**. Both paths are deterministic. The broadcast
  (OBS output) itself does not drop — only the dead link's video is interrupted
  during the switch; this is **not** hitless.
- **Telemetry is new work.** The original controller emitted a terminal TUI +
  logfile only. The HTTP/SSE telemetry server, the JSONL snapshot stream, and the
  dashboard are added here. `state` / `bitrate` / `freeze` come from
  `/v3/paths/list` `bytesReceived` deltas; `rtt` / `loss` are shown when MediaMTX
  reports them on `/v3/srtconns`.
- **Scope.** Full-mesh / single-host — individual to small scale, not an SFU.

## Heritage / contribution boundary

The health state machine is the original car-stream controller
([akagifreeez/mediamtx-failover-controller](https://github.com/akagifreeez/mediamtx-failover-controller)).
The transmission pieces of the wider stack are upstream OSS — the SRT sender app
([CarStream](https://github.com/akagifreeez/CarStream), built on
[StreamPack](https://github.com/ThibaultBee/StreamPack)) and the IPv6 ingest
recipe ([srtla-ipv6-bonding](https://github.com/akagifreeez/srtla-ipv6-bonding),
whose core is the belabox SRT fork / irl-srt-server / srtla_send). This repo's own
work = the integration, the telemetry layer, the state machine, and the dashboard.

## Run the demo (no cellular, no real lines, no OBS needed)

Requires `python` 3.9+, `ffmpeg`, and a `mediamtx` binary.

```bash
cd demo
python run_demo.py --mediamtx /path/to/mediamtx --auto    # scripted: kills linkA, observe, exit
# or, hands-on:
python run_demo.py --mediamtx /path/to/mediamtx           # then kill linkA's ffmpeg yourself
```

Open <http://127.0.0.1:8080> to watch link cards flip GOOD → DEAD and the ACTIVE
marker move when a link dies. Every poll is also appended to `demo/demo-run.jsonl`.

## Controller (standalone)

```bash
python controller.py --no-supervise --headless \
  --api http://127.0.0.1:9997/v3 --http-port 8080 --jsonl run.jsonl
```

- `GET /`         — the Mission Control dashboard
- `GET /events`   — Server-Sent Events; one full snapshot per poll
- `GET /snapshot` — the current snapshot as JSON

All paths/ports/thresholds are configurable via CLI or `RELAYFORGE_*` env vars
(`controller.py --help`). The demo MediaMTX config (`demo/mediamtx-loopback.yml`)
is environment-specific by design.

### Snapshot schema (the contract)

```json
{"ts": 0.0, "active": "linkA", "api_ok": true,
 "links": [{"name":"linkA","state":"GOOD","bitrate_kbps":1550.0,
            "freeze":0,"rtt_ms":null,"loss_pct":null,"readers":0,"uptime_s":7}]}
```

## License

MIT — see [LICENSE](LICENSE).
