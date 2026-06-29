#!/usr/bin/env python3
r"""
RelayForge OBS helper — create the scene + one media source per link, inspect
scene-item visibility, and screenshot the program output. The controller's
obs_apply() then enables only the ACTIVE link's source, so the OBS program
output follows the healthy link on failover.

Reads each link from MediaMTX over RTSP (rtsp://HOST:8554/<name>), so run
MediaMTX with RTSP enabled (see demo/mediamtx-obs.yml).

Usage:
    python obs_setup.py setup            # create scene "RelayForge" + linkA/linkB sources
    python obs_setup.py status           # list scene items + enabled flags
    python obs_setup.py status --json     # same, machine-readable
    python obs_setup.py enable linkB     # manually enable only linkB
    python obs_setup.py shot out.png     # screenshot the program output

Config via env: RELAYFORGE_OBS_HOST/PORT/PASSWORD, RELAYFORGE_OBS_SCENE,
RELAYFORGE_LINKS (comma-separated), RELAYFORGE_RTSP_HOST.
"""
import os
import sys
import json
import base64
import obsws_python as obs

SCENE = os.environ.get("RELAYFORGE_OBS_SCENE", "RelayForge")
LINKS = [s.strip() for s in os.environ.get("RELAYFORGE_LINKS", "linkA,linkB").split(",") if s.strip()]
HOST = os.environ.get("RELAYFORGE_OBS_HOST", "localhost")
PORT = int(os.environ.get("RELAYFORGE_OBS_PORT", "4455"))
PW = os.environ.get("RELAYFORGE_OBS_PASSWORD", "")        # empty when websocket auth is off
RTSP_HOST = os.environ.get("RELAYFORGE_RTSP_HOST", "127.0.0.1")


def client():
    return obs.ReqClient(host=HOST, port=PORT, password=PW, timeout=5)


def src_settings(name):
    return {
        "is_local_file": False,
        "input": "rtsp://%s:8554/%s" % (RTSP_HOST, name),
        "input_format": "",
        "close_when_inactive": False,   # keep decoding while hidden -> instant, gap-free switch
        "restart_on_activate": False,
        "reconnect_delay_sec": 1,
    }


def ensure_scene(cl):
    if SCENE not in [s["sceneName"] for s in cl.get_scene_list().scenes]:
        cl.create_scene(SCENE)
        print("created scene", SCENE)


def fill_canvas(cl, name):
    v = cl.get_video_settings()
    iid = cl.get_scene_item_id(SCENE, name).scene_item_id
    try:
        cl.set_scene_item_transform(SCENE, iid, {
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsWidth": float(v.base_width), "boundsHeight": float(v.base_height),
            "positionX": 0.0, "positionY": 0.0, "alignment": 5,
        })
    except Exception as e:
        print("transform skip(%s):" % name, str(e)[:80])
    return iid


def ensure_source(cl, name):
    if name in [i["inputName"] for i in cl.get_input_list().inputs]:
        cl.set_input_settings(name, src_settings(name), True)
        if name not in [it["sourceName"] for it in cl.get_scene_item_list(SCENE).scene_items]:
            cl.create_scene_item(SCENE, name, True)
        print("updated input", name)
    else:
        cl.create_input(SCENE, name, "ffmpeg_source", src_settings(name), True)
        print("created input", name)
    fill_canvas(cl, name)


def setup():
    cl = client()
    ensure_scene(cl)
    for n in LINKS:
        ensure_source(cl, n)
    cl.set_current_program_scene(SCENE)
    status(cl)


def items(cl=None):
    cl = cl or client()
    return [(it["sourceName"], it["sceneItemEnabled"], it["sceneItemId"])
            for it in cl.get_scene_item_list(SCENE).scene_items]


def status(cl=None, as_json=False):
    rows = items(cl)
    if as_json:
        print(json.dumps({n: en for n, en, _ in rows}))
        return
    print("scene '%s' items:" % SCENE)
    for n, en, iid in rows:
        print("  %-10s enabled=%s id=%s" % (n, en, iid))


def enable(name):
    cl = client()
    for n, en, iid in items(cl):
        cl.set_scene_item_enabled(SCENE, iid, n == name)
    print("enabled only", name)
    status(cl)


def shot(out):
    cl = client()
    r = cl.get_source_screenshot(SCENE, "png", 640, 360, -1)
    with open(out, "wb") as f:
        f.write(base64.b64decode(r.image_data.split(",", 1)[1]))
    print("wrote", out)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "setup":
        setup()
    elif cmd == "status":
        status(as_json=("--json" in sys.argv))
    elif cmd == "enable":
        enable(sys.argv[2])
    elif cmd == "shot":
        shot(sys.argv[2])
    else:
        print("unknown:", cmd); sys.exit(2)
