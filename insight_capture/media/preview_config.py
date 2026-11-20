"""Standalone phone-preview contract; no paths or services from other projects."""

FPS = 30
GOP = 10
PREVIEW_BITRATE = 2_500_000
MAX_PREVIEW_SIDE = 1280
STALE_NS = 500_000_000


def preview_description(cameras, poses, *, paused=False):
    roles = {p.name: p.teleop_role for p in poses}
    order = {"head": 0, "left_hand": 1, "right_hand": 2}
    labels = {"head": "中间相机", "left_hand": "左腕左目", "right_hand": "右腕左目"}
    sources = []
    for camera in cameras:
        role = roles.get(camera.device_name, "")
        width = getattr(camera, "width", 0) or (1088 if role == "head" else 544)
        height = getattr(camera, "height", 0) or (1920 if role == "head" else 640)
        longest = max(width, height)
        if longest > MAX_PREVIEW_SIDE:
            width = max(4, width * MAX_PREVIEW_SIDE // (longest * 4) * 4)
            height = max(4, height * MAX_PREVIEW_SIDE // (longest * 4) * 4)
        sources.append({"id": camera.name, "topic": camera.topic, "width": width, "height": height,
                        "label": labels.get(role, camera.label), "role": role})
    sources.sort(key=lambda s: order.get(s["role"], 9))
    overview = None
    if len(sources) == 3 and {s["role"] for s in sources} == set(order):
        overview = {"id": "__overview__", "label": "三路总览", "width": 960, "height": 960,
                    "direct_video": False,
                    "regions": [dict(id=s["id"], x=x, y=y, width=w, height=h)
                                for s, (x, y, w, h) in zip(sources,
                                    [(0, 0, 552, 960), (552, 0, 408, 480), (552, 480, 408, 480)])]}
    return {"transport": "webrtc", "codec": "h264", "paused": paused,
            "max_fps": FPS, "sources": sources, "overview": overview,
            "default_source": "__overview__" if overview else (sources[0]["id"] if sources else "")}
