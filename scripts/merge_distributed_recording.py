#!/usr/bin/env python3
"""Start manual postprocessing through the shared recording coordinator."""

import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    session = json.loads(args.session.read_text())
    request = Request(args.base_url.rstrip("/") + "/api/postprocessing/merge",
                      data=json.dumps({"session_id": session["session_id"]}).encode(),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        print(response.read().decode())


if __name__ == "__main__":
    main()
