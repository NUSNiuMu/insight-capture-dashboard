"""Standalone phone-preview contract; no paths or services from other projects."""

FPS = 30
GOP = 10
PREVIEW_BITRATE = 2_500_000
MAX_PREVIEW_SIDE = 1280
STALE_NS = 500_000_000
OVERVIEW_HOLD_NS = 2_000_000_000


def preview_description(cameras, poses, *, paused=False):
    roles = {p.name: p.teleop_role for p in poses}
    order = {"head": 0, "left_hand": 1, "right_hand": 2}
    labels = {"head": "中间相机", "left_hand": "左腕左目", "right_hand": "右腕左目"}
    head_count = sum(roles.get(camera.device_name) == "head" for camera in cameras)
    sources = []
    for camera in cameras:
        role = roles.get(camera.device_name, "")
        uq2168 = getattr(camera, "uq2168", False)
        width = getattr(camera, "width", 0) or (1088 if role == "head" else 544)
        height = getattr(camera, "height", 0) or (1920 if role == "head" else 640)
        longest = max(width, height)
        if not uq2168 and longest > MAX_PREVIEW_SIDE:
            width = max(4, width * MAX_PREVIEW_SIDE // (longest * 4) * 4)
            height = max(4, height * MAX_PREVIEW_SIDE // (longest * 4) * 4)
        label = camera.label if uq2168 else labels.get(role, camera.label)
        if role == "head" and head_count > 1:
            label = camera.label.removeprefix("EgoHalo ")
        sources.append({"id": camera.name, "topic": camera.topic, "width": width, "height": height,
                        "label": label, "role": role})
    sources.sort(key=lambda s: order.get(s["role"], 9))
    overview = None
    if head_count == 4:
        head_sources = [source for source in sources if source["role"] == "head"]
        overview = {
            "id": "__overview__", "label": "四路总览", "width": 960, "height": 1280, "direct_video": False,
            "regions": [dict(id=source["id"], x=index % 2 * 480, y=index // 2 * 640, width=480, height=640)
                        for index, source in enumerate(head_sources)],
        }
    elif len(sources) == 3 and {s["role"] for s in sources} == set(order):
        overview = {"id": "__overview__", "label": "三路总览", "width": 960, "height": 960,
                    "direct_video": False,
                    "regions": [dict(id=s["id"], x=x, y=y, width=w, height=h)
                                for s, (x, y, w, h) in zip(sources,
                                    [(0, 0, 552, 960), (552, 0, 408, 480), (552, 480, 408, 480)])]}
    return {"transport": "webrtc", "codec": "h264", "paused": paused,
            "max_fps": FPS, "sources": sources, "overview": overview,
            "default_source": "__overview__" if overview else (sources[0]["id"] if sources else "")}
