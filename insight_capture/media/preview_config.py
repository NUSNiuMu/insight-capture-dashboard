"""Standalone phone-preview contract; no paths or services from other projects."""

FPS = 30
GOP = 10
OVERVIEW_QP = 23
SINGLE_BITRATE = 2_500_000
SYNC_TOLERANCE_NS = 25_000_000
SYNC_WAIT_NS = 50_000_000
STALE_NS = 500_000_000


def preview_description(cameras, poses, *, paused=False):
    roles = {p.name: p.teleop_role for p in poses}
    order = {"head": 0, "left_hand": 1, "right_hand": 2}
    labels = {"head": "中间相机", "left_hand": "左腕左目", "right_hand": "右腕左目"}
    sources = [{"id": c.name, "topic": c.topic, "width": getattr(c, "width", 0) or (1088 if roles.get(c.device_name) == "head" else 544),
                "height": getattr(c, "height", 0) or (1920 if roles.get(c.device_name) == "head" else 640),
                "label": labels.get(roles.get(c.device_name), c.label),
                "role": roles.get(c.device_name, "")}
               for c in cameras]
    sources.sort(key=lambda s: order.get(s["role"], 9))
    overview = None
    if len(sources) == 3 and {s["role"] for s in sources} == set(order):
        overview = {"id": "__overview__", "label": "三路总览", "width": 1280, "height": 1280,
                    "direct_video": False,
                    "regions": [dict(id=s["id"], x=x, y=y, width=w, height=h)
                                for s, (x, y, w, h) in zip(sources,
                                    [(0, 0, 736, 1280), (736, 0, 544, 640), (736, 640, 544, 640)])]}
    return {"transport": "webrtc", "codec": "h264", "paused": paused,
            "max_fps": FPS, "sources": sources, "overview": overview,
            "default_source": "__overview__" if overview else (sources[0]["id"] if sources else "")}
