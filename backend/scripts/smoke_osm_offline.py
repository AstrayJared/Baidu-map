"""Offline real-cache HTTP smoke + performance evidence, without a running server."""
import argparse
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from app.config import load_settings
from app.main import create_app
from app.osm_api import router as internal_osm_router
from tools.test_origin import TEST_ORIGIN


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lng", type=float, default=TEST_ORIGIN[0], help="BD09 longitude")
    parser.add_argument("--lat", type=float, default=TEST_ORIGIN[1], help="BD09 latitude")
    parser.add_argument("--output", type=Path, default=Path("../.tmp/osm-smoke.json"))
    args = parser.parse_args()
    config = load_settings()
    started = time.perf_counter()
    app = create_app(config)
    # Explicit offline harness only; production does not expose this route.
    app.include_router(internal_osm_router)
    with TestClient(app) as client:
        startup_ms = (time.perf_counter()-started)*1000
        body = {"origin": {"lng": args.lng, "lat": args.lat}, "coordinate_system": "bd09ll", "algorithm": "osm_offline"}
        def forbidden(*args, **kwargs):
            raise AssertionError("Offline runtime attempted a network connection")
        with patch.object(socket.socket, "connect", forbidden), patch.object(socket, "create_connection", forbidden):
            responses = [client.post("/api/v1/analysis/osm_offline", json=body) for _ in range(5)]
        payloads = [response.json() for response in responses]
        assert all(r.status_code == 200 for r in responses)
        assert all(p["algorithm"]["quality"] in ("usable", "partial") and p["data"]["geometry"] is not None for p in payloads), payloads[0]["algorithm"]["stopReason"]
        assert all(p["data"]["geometry"] == payloads[0]["data"]["geometry"] for p in payloads)
        assert all(p["algorithm"]["diagnostics"]["network_requests"] == 0 for p in payloads)
        evidence = {"startup_ms": startup_ms, "identical_geometry_5_runs": True,
                    "network_connections_blocked": True, "samples": [p["algorithm"]["diagnostics"] for p in payloads],
                    "response": payloads[0]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"startup_ms": startup_ms, "total_ms": [s["total_ms"] for s in evidence["samples"]],
                      "quality": payloads[0]["algorithm"]["quality"], "evidence": str(args.output)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
