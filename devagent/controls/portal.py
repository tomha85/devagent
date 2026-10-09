from __future__ import annotations

import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from devagent.controls.catalog import STANDARDS
from devagent.controls.build import (
    ControlsBuildError,
    build_controls_spec,
    verify_controls_build,
)
from devagent.controls.diff import diff_build_against_spec
from devagent.controls.ir import build_controls_ir, controls_ir_payload, controls_ir_sha256
from devagent.controls.manifest import build_authoring_manifest
from devagent.controls.normalize import normalized_spec_payload
from devagent.controls.review import create_review_request
from devagent.controls.rules import evaluate_controls_rules
from devagent.controls.schema import ControlSpecError, parse_control_system_payload
from devagent.controls.symbols import portable_name

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class PortalService:
    """Small self-service facade over the same deterministic Controls library."""

    def __init__(self, workspace: Path):
        self.workspace = workspace.expanduser().resolve(strict=False)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _build_target(self, payload: Any) -> tuple[Any, Any, Path]:
        spec = parse_control_system_payload(payload)
        ir = build_controls_ir(spec)
        target = self.workspace / (
            f"{portable_name(spec.project_id)}-{controls_ir_sha256(ir)[:12]}"
        )
        return spec, ir, target

    def catalog_payload(self) -> dict[str, Any]:
        return {
            "schema": "devagent-controls-catalog-v2",
            "standards": [
                {
                    "id": item.id,
                    "equipment_type": item.equipment_type,
                    "required_commands": list(item.required_commands),
                    "required_status": list(item.required_status),
                    "generated_outputs": list(item.generated_outputs),
                    "default_faceplate": item.default_faceplate,
                    "min_permissives": item.min_permissives,
                    "min_interlocks": item.min_interlocks,
                    "min_alarms": item.min_alarms,
                    "fault_status_member": item.fault_status_member,
                    "alarm_source_policy": item.alarm_source_policy,
                    "historian_policy": item.historian_policy,
                }
                for item in sorted(STANDARDS.values(), key=lambda value: value.id)
            ],
        }

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
        spec, ir, target = self._build_target(payload)
        expected_ir_sha256 = controls_ir_sha256(ir)
        validation = self.validate_payload(payload)
        if validation["status"] != "PASS":
            raise ControlsBuildError("company standards must pass before portal build")
        if target.exists():
            verified = verify_controls_build(target)
            if verified["status"] != "PASS":
                raise ControlsBuildError(
                    f"existing portal build is invalid: {target}: "
                    + " | ".join(verified["errors"])
                )
            if verified.get("controls_ir_sha256") != expected_ir_sha256:
                raise ControlsBuildError(
                    "portal build path hash-prefix collision detected; refusing "
                    "to reuse a different Controls IR"
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

    def _find_baseline_build(
        self,
        *,
        project_id: str,
        controls_ir_sha256_value: str,
    ) -> Path:
        digest = controls_ir_sha256_value.strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ControlsBuildError(
                "baseline_controls_ir_sha256 must be a 64-character SHA-256 hex digest"
            )

        matches: list[Path] = []
        for candidate in sorted(self.workspace.iterdir()):
            if not candidate.is_dir():
                continue
            manifest_path = candidate / "generation-manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                manifest.get("project_id") == project_id
                and manifest.get("controls_ir_sha256") == digest
            ):
                matches.append(candidate)
        if not matches:
            raise ControlsBuildError(
                "no verified portal build matches the requested project/hash baseline"
            )
        baseline = matches[0]
        verified = verify_controls_build(baseline)
        if verified["status"] != "PASS":
            raise ControlsBuildError(
                "requested baseline build failed verification: "
                + " | ".join(verified["errors"])
            )
        return baseline

    def diff_payload(
        self,
        payload: Any,
        *,
        baseline_controls_ir_sha256: str,
    ) -> dict[str, Any]:
        candidate = parse_control_system_payload(payload)
        baseline = self._find_baseline_build(
            project_id=candidate.project_id,
            controls_ir_sha256_value=baseline_controls_ir_sha256,
        )
        return diff_build_against_spec(baseline, candidate)

    def request_review_payload(
        self,
        payload: Any,
        *,
        requested_by: str,
    ) -> dict[str, Any]:
        _spec, ir, target = self._build_target(payload)
        # Always verify or build the deterministic target before reusing/creating
        # any review request. A stale build from an older generator must not be
        # silently accepted merely because the directory already exists.
        self.build_payload(payload)

        identity = requested_by.strip()
        if not identity:
            raise ControlsBuildError("requested_by must not be empty")
        requester_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        review_path = (
            self.workspace
            / "review-requests"
            / (
                f"{portable_name(ir.project_id)}-{controls_ir_sha256(ir)[:12]}-"
                f"{requester_hash}.json"
            )
        )
        if review_path.exists():
            request = json.loads(review_path.read_text(encoding="utf-8"))
            return {
                "status": "PASS",
                "reused": True,
                "request_path": str(review_path),
                "request": request,
            }

        request = create_review_request(
            target,
            requested_by=identity,
            output_path=review_path,
        )
        return {
            "status": "PASS",
            "reused": False,
            "request_path": str(review_path),
            "request": request,
        }


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DevAgent Controls</title>
<style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:1180px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.75rem 1rem}
.full{grid-column:1/-1}
label{font-weight:600;display:block;margin-bottom:.25rem}
input,select,textarea{box-sizing:border-box;width:100%;padding:.55rem}
textarea{min-height:90px;font-family:ui-monospace,monospace}
#spec{min-height:330px}
button{margin:.5rem .5rem .5rem 0;padding:.6rem 1rem}
pre{background:#f4f4f4;padding:1rem;white-space:pre-wrap;overflow:auto}
small{color:#555}
@media(max-width:760px){.grid{grid-template-columns:1fr}.full{grid-column:auto}}
</style>
</head>
<body>
<h1>DevAgent Controls Platform</h1>
<p>Configure standardized equipment, validate company rules, generate deterministic
Rockwell/Ignition/FAT staging artifacts, and request engineering review. This
portal never performs PLC writes/downloads or Ignition Gateway deployment.</p>

<div class="grid">
<div><label>Project ID</label><input id="projectId" value="PACKAGING_LINE_04"></div>
<div><label>Controller ID</label><input id="controllerId" value="PLC_PACK_01"></div>
<div><label>Rockwell platform</label><select id="platform"><option>CONTROLLOGIX</option></select></div>
<div><label>Controller network</label><input id="network" placeholder="Packaging VLAN 20"></div>
<div><label>Equipment type</label><select id="equipmentType"></select></div>
<div><label>Equipment ID</label><input id="equipmentId" value="CONV_101"></div>
<div><label>Area</label><input id="area" value="Packaging"></div>
<div><label>Safety zone</label><input id="safetyZone" value="SZ03"></div>
<div class="full"><label>Signals (comma separated)</label><input id="signals" value="SAFETY_OK,DOWNSTREAM_READY,GUARD_OPEN,DRIVE_FAULT"></div>
<div class="full"><label>Permissives</label><input id="permissives" value="SAFETY_OK,DOWNSTREAM_READY"></div>
<div class="full"><label>Interlocks / fault sources</label><input id="interlocks" value="GUARD_OPEN,DRIVE_FAULT"></div>
<div><label>Alarm ID</label><input id="alarmId" value="ALM_CONV101_DRIVE_FAULT"></div>
<div><label>Alarm source signal</label><input id="alarmSource" value="DRIVE_FAULT"></div>
<div><label>Alarm priority</label><select id="alarmPriority"><option>HIGH</option><option>CRITICAL</option><option>MEDIUM</option><option>LOW</option><option>INFO</option></select></div>
<div><label>Historian</label><select id="historian"><option value="true">Enabled</option><option value="false">Disabled</option></select></div>
<div class="full"><label>Operator response</label><input id="operatorResponse" value="Inspect drive fault and correct the cause before reset."></div>
<div class="full"><label>I/O mappings</label><textarea id="ioMappings" placeholder="One per line: INPUT SIGNAL.GUARD_OPEN Local:1:I.Data.0&#10;OUTPUT OUTPUT.RUN Local:2:O.Data.0"></textarea><small>Mappings are staged metadata only. Generated standard logic never writes a physical I/O address directly.</small></div>
<div><label>Requirement ID</label><input id="requirementId" value="REQ_CONV101_GUARD"></div>
<div><label>Criticality</label><select id="criticality"><option>HIGH</option><option>CRITICAL</option><option>MEDIUM</option><option>LOW</option></select></div>
<div class="full"><label>Requirement text</label><input id="requirementText" value="CONV_101 must not run while GUARD_OPEN is active."></div>
<div class="full"><label>Assertion conditions</label><input id="assertConditions" value="COMMAND.START=true,SIGNAL.GUARD_OPEN=true"><small>Structured Boolean references. Safe baseline values for other declared permissives/interlocks are made explicit in the generated FAT case.</small></div>
<div><label>Expected reference</label><input id="expectedRef" value="OUTPUT.RUN"></div>
<div><label>Expected value</label><select id="expectedValue"><option value="false">false</option><option value="true">true</option></select></div>
</div>

<button onclick="generateSpec()">Generate deterministic spec</button>
<button onclick="callApi('validate')">Validate</button>
<button onclick="callApi('build')">Build staging artifacts</button>
<br>
<div class="grid">
<div><label>Baseline Controls IR SHA-256</label><input id="baselineHash" placeholder="Paste a prior verified build Controls IR hash"></div>
<div><label>Engineer</label><input id="requestedBy" placeholder="Engineer name for review request"></div>
</div>
<button onclick="callApi('diff')">Review diff against baseline</button>
<button onclick="callApi('review')">Request engineering review</button>

<h2>Generated / advanced specification</h2>
<textarea id="spec"></textarea>
<h2>Result</h2>
<pre id="result"></pre>

<script>
let catalog={standards:[]};
function csv(id){return document.getElementById(id).value.split(',').map(x=>x.trim()).filter(Boolean);}
function boolValue(id){return document.getElementById(id).value==='true';}
function assertionConditions(){
 const result={};
 for(const pair of csv('assertConditions')){
   const pos=pair.lastIndexOf('=');
   if(pos<1) throw new Error('Assertion condition must be REF=true or REF=false: '+pair);
   const key=pair.slice(0,pos).trim(), value=pair.slice(pos+1).trim().toLowerCase();
   if(value!=='true'&&value!=='false') throw new Error('Assertion Boolean must be true/false: '+pair);
   result[key]=value==='true';
 }
 return result;
}
function ioRows(){
 const lines=document.getElementById('ioMappings').value.split(/\n+/).map(x=>x.trim()).filter(Boolean);
 return lines.map(line=>{
   const parts=line.split(/\\s+/);
   if(parts.length<3) throw new Error('I/O line must be: INPUT|OUTPUT MEMBER ADDRESS');
   return {direction:parts[0].toUpperCase(),member:parts[1],address:parts.slice(2).join(' ')};
 });
}
function selectedStandard(){
 const type=document.getElementById('equipmentType').value;
 const found=catalog.standards.find(x=>x.equipment_type===type);
 if(!found) throw new Error('No qualified standard for '+type);
 return found;
}
function generateSpec(){
 const s=selectedStandard();
 const spec={
   schema:'devagent-controls-spec-v2',
   project_id:document.getElementById('projectId').value.trim(),
   controllers:[{
     id:document.getElementById('controllerId').value.trim(),
     vendor:'ROCKWELL',
     platform:document.getElementById('platform').value,
     network:document.getElementById('network').value.trim()||null
   }],
   equipment:[{
     id:document.getElementById('equipmentId').value.trim(),
     type:s.equipment_type,
     standard:s.id,
     controller:document.getElementById('controllerId').value.trim(),
     area:document.getElementById('area').value.trim()||null,
     safety_zone:document.getElementById('safetyZone').value.trim()||null,
     signals:csv('signals'),
     commands:Object.fromEntries(s.required_commands.map(x=>[x,true])),
     status:s.required_status,
     permissives:csv('permissives'),
     interlocks:csv('interlocks'),
     alarms:[{
       id:document.getElementById('alarmId').value.trim(),
       priority:document.getElementById('alarmPriority').value,
       operator_response:document.getElementById('operatorResponse').value.trim(),
       source_signal:document.getElementById('alarmSource').value.trim()
     }],
     hmi:{faceplate:s.default_faceplate,historian:boolValue('historian')},
     io:ioRows(),
     requirements:[{
       id:document.getElementById('requirementId').value.trim(),
       text:document.getElementById('requirementText').value.trim(),
       criticality:document.getElementById('criticality').value,
       assertion:{
         conditions:assertionConditions(),
         expect:{[document.getElementById('expectedRef').value.trim()]:boolValue('expectedValue')}
       }
     }]
   }]
 };
 document.getElementById('spec').value=JSON.stringify(spec,null,2);
 return spec;
}
async function callApi(action){
 const out=document.getElementById('result');
 try{
   const text=document.getElementById('spec').value.trim();
   const spec=text?JSON.parse(text):generateSpec();
   const payload=action==='review'
     ? {spec:spec,requested_by:document.getElementById('requestedBy').value}
     : action==='diff'
     ? {spec:spec,baseline_controls_ir_sha256:document.getElementById('baselineHash').value}
     : spec;
   const response=await fetch('/api/'+action,{
     method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)
   });
   const data=await response.json();
   out.textContent=JSON.stringify(data,null,2);
 }catch(error){out.textContent=String(error);}
}
async function init(){
 const response=await fetch('/api/catalog');
 catalog=await response.json();
 const select=document.getElementById('equipmentType');
 for(const type of [...new Set(catalog.standards.map(x=>x.equipment_type))].sort()){
   const option=document.createElement('option'); option.value=type; option.textContent=type; select.appendChild(option);
 }
 select.value='CONVEYOR';
 generateSpec();
}
init().catch(error=>document.getElementById('result').textContent=String(error));
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
            if self.path == "/api/catalog":
                self._send_json(HTTPStatus.OK, service.catalog_payload())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in {"/api/validate", "/api/build", "/api/diff", "/api/review"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > _MAX_REQUEST_BYTES:
                    raise ValueError("request body size is invalid")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/api/validate":
                    result = service.validate_payload(payload)
                elif self.path == "/api/build":
                    result = service.build_payload(payload)
                elif self.path == "/api/diff":
                    if not isinstance(payload, dict):
                        raise ValueError("diff request body must be a JSON object")
                    result = service.diff_payload(
                        payload.get("spec"),
                        baseline_controls_ir_sha256=str(
                            payload.get("baseline_controls_ir_sha256", "")
                        ),
                    )
                else:
                    if not isinstance(payload, dict):
                        raise ValueError("review request body must be a JSON object")
                    result = service.request_review_payload(
                        payload.get("spec"),
                        requested_by=str(payload.get("requested_by", "")),
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
