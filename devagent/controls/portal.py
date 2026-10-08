from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from devagent.controls.build import (
    ControlsBuildError,
    build_controls_spec,
    verify_controls_build,
)
from devagent.controls.ir import build_controls_ir, controls_ir_payload, controls_ir_sha256
from devagent.controls.manifest import build_authoring_manifest
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.rules import evaluate_controls_rules
from devagent.controls.schema import ControlSpecError, parse_control_system_payload

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class PortalService:
    """Small self-service façade over the same deterministic Controls library."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def validate_payload(self, payload: Any) -> dict[str, Any]:
        spec = parse_control_system_payload(payload)
        ir = build_controls_ir(spec)
        rules = evaluate_controls_rules(spec)
        return {
            "status": "PASS" if all(item.status != "FAIL" for item in rules) else "FAIL",
            "manifest": build_authoring_manifest(spec, ir),
            "normalized_spec": normalized_spec_payload(spec),
            "controls_ir": controls_ir_payload(ir),
            "company_rules": [
                {
                    "id": item.id,
                    "status": item.status,
                    "subject": item.subject,
                    "summary": item.summary,
                }
                for item in rules
            ],
        }

    def build_payload(self, payload: Any) -> dict[str, Any]:
        spec = parse_control_system_payload(payload)
        ir = build_controls_ir(spec)
        validation = self.validate_payload(payload)
        if validation["status"] != "PASS":
            raise ControlsBuildError("company standards must pass before portal build")
        target = self.workspace / f"{spec.project_id}-{controls_ir_sha256(ir)[:12]}"
        if target.exists():
            verified = verify_controls_build(target)
            if verified["status"] != "PASS":
                raise ControlsBuildError(
                    f"existing portal build is invalid: {target}: "
                    + " | ".join(verified["errors"])
                )
            return {
                "status": "PASS",
                "reused": True,
                "build_dir": str(target),
                "verification": verified,
            }
        result = build_controls_spec(spec, target)
        verified = verify_controls_build(target)
        return {
            "status": "PASS",
            "reused": False,
            "build_dir": str(result.output_dir),
            "readiness": result.readiness,
            "verification": verified,
        }


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DevAgent Controls</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}
textarea{width:100%;min-height:420px;font-family:ui-monospace,monospace}
button{margin:.5rem .5rem .5rem 0;padding:.6rem 1rem}
pre{background:#f4f4f4;padding:1rem;white-space:pre-wrap}
</style>
</head>
<body>
<h1>DevAgent Controls Platform</h1>
<p>Deterministic specification validation and staging build. No PLC or Ignition
production deployment is performed by this portal.</p>
<textarea id="spec">{"schema":"devagent-controls-spec-v1"}</textarea><br>
<button onclick="callApi('validate')">Validate</button>
<button onclick="callApi('build')">Build staging artifacts</button>
<pre id="result"></pre>
<script>
async function callApi(action){
 const out=document.getElementById('result');
 try{
   const payload=JSON.parse(document.getElementById('spec').value);
   const response=await fetch('/api/'+action,{
     method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)
   });
   const data=await response.json();
   out.textContent=JSON.stringify(data,null,2);
 }catch(error){out.textContent=String(error);}
}
</script>
</body>
</html>
"""


def serve_portal(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote: bool = False,
) -> None:
    if host not in _LOOPBACK_HOSTS and not allow_remote:
        raise ValueError(
            "remote Controls portal binding is disabled by default; "
            "use --allow-remote only behind an operator-managed secure boundary"
        )
    service = PortalService(workspace)

    class Handler(BaseHTTPRequestHandler):
        server_version = "DevAgentControls/1.0"

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                body = _INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/health":
                self._send_json(HTTPStatus.OK, {"status": "PASS"})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {"/api/validate", "/api/build"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > _MAX_REQUEST_BYTES:
                    raise ValueError("request body size is invalid")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = (
                    service.validate_payload(payload)
                    if self.path == "/api/validate"
                    else service.build_payload(payload)
                )
                self._send_json(HTTPStatus.OK, result)
            except (ControlSpecError, ControlsBuildError, ValueError, OSError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "FAIL", "error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        print(f"DevAgent Controls portal: http://{host}:{port}")
        print(f"Workspace: {service.workspace}")
        server.serve_forever()
    finally:
        server.server_close()
