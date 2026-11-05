"""Read-only support snapshot shared by desktop and phone clients."""

from datetime import datetime
import json
import os
from aiohttp import web


class DiagnosticRoutes:
    def __init__(self, context):
        self.context = context

    async def snapshot(self, _request):
        node = self.context.node
        config = json.loads(node.config_path.read_text())
        marker = node.config_path.parent / ".device"
        return web.json_response({
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "configured_release": os.environ.get("INSIGHT_VERSION"),
            "profile": marker.read_text().strip() if marker.exists() else None,
            "ros_domain_id": node.context.get_domain_id(),
            "rmw": os.environ.get("RMW_IMPLEMENTATION"),
            "ros_transport": config.get("ros_transport", {}),
            "image_qos": node.image_qos_reliability,
            "input_generation": node.camera_inputs.generation,
            "camera_links": [vars(link) for link in sorted(node.camera_inputs.links, key=lambda link: link.name)],
            "cameras": node.build_camera_payload(),
            "mapping": node.build_mapping_payload(),
            "events": node.events.snapshot(),
        })
