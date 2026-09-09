"""Host-side live verification and evidence exporter; Python standard library only."""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_CODE = """import json,os,sys,urllib.request,urllib.error
x=json.load(sys.stdin)
req=urllib.request.Request('http://localhost:8000'+x['path'],method=x['method'],
 data=json.dumps(x['body']).encode() if x.get('body') is not None else None,
 headers={'Authorization':'Bearer '+os.environ['API_TOKEN'],'Content-Type':'application/json'})
try:
 with urllib.request.urlopen(req,timeout=650) as resp:
  result={'status':resp.status,'response':json.loads(resp.read())}
except urllib.error.HTTPError as exc:
 result={'status':exc.code,'response':json.loads(exc.read())}
print(json.dumps(result))
"""
COLLECT_CODE = """import json,sys,os
from pathlib import Path
x=json.load(sys.stdin);root=Path('/sessions')/x['id']/'data'
files={};missing=[]
for name in ['state/session.jsonl','state/python-packages.json','extension-audit.jsonl']+x['artifacts']:
 p=root/name
 if not p.resolve().is_relative_to(root.resolve()):raise ValueError('Artifact escapes session')
 if p.is_file():files[name]=p.read_text()
 else:missing.append(name)
print(json.dumps({'files':files,'missing':missing,'model':os.getenv('PI_MODEL')}))
"""
HISTORY_CODE = """import json,sys,sqlite3,os
from pathlib import Path
x=json.load(sys.stdin);rows=[]
if Path('/state/conversations.sqlite').exists():
 with sqlite3.connect('file:/state/conversations.sqlite?mode=ro',uri=True) as db:
  if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_messages'").fetchone():
   rows=db.execute('SELECT message_data FROM agent_messages WHERE session_id=? ORDER BY id',(x['agent_id'],)).fetchall()
print(json.dumps({'model':os.getenv('OPENAI_MODEL'),'messages':[json.loads(r[0]) for r in rows]}))
"""
DEFAULT_ARTIFACTS = ["verification.py", "analysis.py", "analysis.json", "package-stats.json"]


def now():
    return dt.datetime.now(dt.UTC).isoformat()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def docker_python(service, code, payload, python="python"):
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", service, python, "-c", code],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=700,
    )
    if result.returncode:
        raise RuntimeError(f"Docker execution failed for {service} (exit {result.returncode})")
    return json.loads(result.stdout)


def call(report, target, method, path, body=None):
    record = {"started_at": now(), "method": method, "path": path, "body": body}
    report.setdefault("requests", []).append(record)
    save(target, report)  # Keep an intent even if the connection is lost.
    result = docker_python("api", API_CODE, record)
    record.update(result, finished_at=now())
    save(target, report)  # Preserve usage before raising or validating evidence.
    if result["status"] >= 400:
        raise RuntimeError(f"HTTP {result['status']} for {path}; response saved in report")
    return result["response"]


def session_path(s):
    return f"/agents/{s['agent_id']}/sessions/{s['id']}"


def validate_session(s):
    for key in ("id", "agent_id"):
        uuid.UUID(s[key])
    # Session identity is supplied by API/manifest, never interpolated into shell code.
    if not s["sandbox_id"] or s["sandbox_id"].startswith("-"):
        raise ValueError("Invalid Compose service")


def usage_summary(report):
    outer = dict.fromkeys(["requests", "input_tokens", "output_tokens", "total_tokens"], 0)
    pi = dict.fromkeys(["input", "output", "cacheRead", "cacheWrite", "totalTokens"], 0)
    gaps = []
    for q in report.get("requests", []):
        if not q["path"].endswith("/run"):
            if "/pi/prompt" in q["path"] and ("status" not in q or q["status"] >= 400):
                gaps.append("Pi prompt failed or interrupted; provider usage may be missing")
            continue
        response = q.get("response", {})
        detail = response.get("detail", {})
        u = response.get("usage") or (detail.get("usage") if isinstance(detail, dict) else None)
        if u is None:
            gaps.append("Missing outer run usage: " + q["path"])
        else:
            for k in outer:
                outer[k] += u[k]
    for e in report.get("evidence", []):
        for k in pi:
            pi[k] += e["usage"][k]
        gaps.extend(e.get("usage_gaps", []))
    if len(report.get("evidence", [])) != len(report["sessions"]):
        gaps.append("One or more session transcripts were not collected")
    if not report.get("fresh_sessions"):
        gaps.append(
            "Existing sessions: Pi is lifetime usage; outer totals include only captured requests"
        )
    total = outer["total_tokens"] + pi["totalTokens"]
    return {
        "agents_sdk": outer,
        "pi": pi,
        "recorded_total_tokens": total,
        "complete_for_this_run": not gaps,
        "limitations": gaps,
        "cached_input_included": True,
    }


def collect(report, target, artifacts):
    evidence = []
    for s in report["sessions"]:
        validate_session(s)
        data = docker_python(
            s["sandbox_id"], COLLECT_CODE, {**s, "artifacts": artifacts}, "python3"
        )
        folder = target.parent / "sessions" / s["id"]
        for name, content in data["files"].items():
            dest = folder / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
            os.chmod(dest, 0o600)
        rows = [
            json.loads(line)
            for line in data["files"].get("state/session.jsonl", "").splitlines()
            if line.strip()
        ]
        messages = [v["message"] for v in rows if v.get("type") == "message"]
        assistants = [m for m in messages if m.get("role") == "assistant"]
        usage = {
            k: sum(m.get("usage", {}).get(k, 0) for m in assistants)
            for k in ["input", "output", "cacheRead", "cacheWrite", "totalTokens"]
        }
        gaps = (
            []
            if "state/session.jsonl" in data["files"]
            else ["Session transcript not persisted or missing: " + s["id"]]
        )
        gaps += ["Assistant missing usage" for m in assistants if not m.get("usage")]
        gaps += [
            "Assistant ended with error/aborted; provider may have unreported usage"
            for m in assistants
            if m.get("stopReason") in ("error", "aborted")
        ]
        trace = []
        for row in rows:
            m = row.get("message", {})
            if m:
                trace.append(
                    {
                        "entry_id": row.get("id"),
                        "parent_id": row.get("parentId"),
                        "timestamp": row.get("timestamp"),
                        "message": m,
                    }
                )
        save(folder / "trace.json", trace)
        history = docker_python("api", HISTORY_CODE, s)
        save(folder / "outer-history.json", history)
        evidence.append(
            {
                **s,
                "model": data["model"],
                "usage": usage,
                "usage_gaps": gaps,
                "assistant_messages": len(assistants),
                "missing_files": data["missing"],
                "files": data["files"],
                "trace": trace,
            }
        )
        report["evidence"] = evidence
        save(target, report)
    report["token_summary"] = usage_summary(report)
    report["collected_at"] = now()
    save(target, report)


def verify_evidence(report):
    a, b, c = report["evidence"]

    def calls(e):
        return {
            v["name"]
            for row in e["trace"]
            for v in row["message"].get("content", [])
            if isinstance(v, dict) and v.get("type") == "toolCall"
        }

    def successful(e, name):
        return any(
            row["message"].get("role") == "toolResult"
            and row["message"].get("toolName") == name
            and not row["message"].get("isError", False)
            for row in e["trace"]
        )

    for e in (a, b):
        assert {"write", "bash"} <= calls(e), "Missing actual write/bash calls"
        assert successful(e, "write") and successful(e, "bash"), (
            "Missing successful write/bash results"
        )
    assert "sum" in a["files"]["verification.py"]
    assert any(
        "55" in str(row["message"].get("content"))
        for row in a["trace"]
        if row["message"].get("role") == "toolResult" and row["message"].get("toolName") == "bash"
    )
    stats = json.loads(b["files"]["analysis.json"])
    assert stats == {"count": 3, "sum": 18, "mean": 6, "min": 3, "max": 9}
    assert any(
        '<skill name="python-stats"' in str(row["message"].get("content")) for row in b["trace"]
    )
    assert any(
        '<skill name="package-stats"' in str(row["message"].get("content")) for row in c["trace"]
    )
    assert {"python_stats", "session_info"} <= calls(c)
    assert successful(c, "python_stats") and successful(c, "session_info")
    package = json.loads(c["files"]["package-stats.json"])
    assert package["mean"] == 6 and package["python"] == "/workspace/venv/bin/python"
    runs = [q["response"] for q in report["requests"] if q["path"].endswith("/run")]
    assert "55" in runs[0]["output"] and runs[0]["sandbox_results"][0]["session_id"] == a["id"]
    assert "6" in runs[1]["output"] and runs[1]["sandbox_results"][0]["session_id"] == c["id"]
    report["checks"] += [
        "Python write → bash execution → outer Agent result",
        "Native python-stats skill → generated Python + statistics JSON",
        "Native package skill → python_stats tool → session venv → outer Agent",
        "Model invoked session_info extension successfully",
    ]


def verify(report, target, args):
    for worker in (args.worker1, args.worker1, args.worker2):
        aid = call(report, target, "POST", "/agents")["agent_id"]
        s = call(report, target, "POST", f"/agents/{aid}/sessions", {"sandbox_id": worker})
        report["sessions"].append({**s, "agent_id": aid})
        save(target, report)
    a, b, c = report["sessions"]
    print("Created three fresh sessions.", flush=True)
    call(
        report,
        target,
        "POST",
        f"/agents/{a['agent_id']}/run",
        {
            "session_ids": [a["id"]],
            "prompt": "Ask pi to write /workspace/verification.py that computes sum of squares of integers 1 through 5, then execute it with python. Return the actual numeric result concisely.",
        },
    )
    print("Python delegation finished.", flush=True)
    call(
        report,
        target,
        "POST",
        session_path(b) + "/pi/prompt",
        {"prompt": "/skill:python-stats 3,6,9"},
    )
    print("Standalone skill finished.", flush=True)
    call(
        report,
        target,
        "POST",
        session_path(c) + "/packages",
        {"action": "install", "source": "/workspace/packages/stats-kit"},
    )
    call(
        report,
        target,
        "POST",
        f"/agents/{c['agent_id']}/run",
        {
            "session_ids": [c["id"]],
            "prompt": "Delegate to pi with the exact prompt '/skill:package-stats 3,6,9'. Ask pi to also call session_info. Return its actual tool results concisely.",
        },
    )
    for s in (a, b):
        resources = call(report, target, "GET", session_path(s) + "/resources")
        assert "package-stats" not in {v["name"] for v in resources["commands"]}
    report["checks"].append("Package absent from the two other sessions")
    collect(report, target, args.artifact)
    verify_evidence(report)
    report["status"] = "passed"
    save(target, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    v = sub.add_parser(
        "verify", help="Run three live model scenarios; sessions retained for inspection"
    )
    v.add_argument(
        "--output", default="artifacts/verification-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    v.add_argument("--worker1", default="sandbox-1")
    v.add_argument("--worker2", default="sandbox-2")
    for name in ("collect", "cleanup"):
        p = sub.add_parser(name)
        p.add_argument("--report", required=True, help="Existing report or manifest with sessions")
    for p in (v, sub.choices["collect"]):
        p.add_argument(
            "--artifact",
            action="append",
            default=DEFAULT_ARTIFACTS.copy(),
            help="Additional UTF-8 workspace-relative file",
        )
    args = parser.parse_args()
    for name in getattr(args, "artifact", []):
        p = Path(name)
        if (
            p.is_absolute()
            or ".." in p.parts
            or not p.parts
            or p.parts[0] in ("home", "state", "venv")
        ):
            parser.error("Artifacts must be workspace-relative and outside home/state/venv")
    if args.command == "verify":
        folder = Path(args.output).resolve()
        folder.mkdir(parents=True, exist_ok=False)
        os.chmod(folder, 0o700)
        target = folder / "report.json"
        report = {
            "started_at": now(),
            "fresh_sessions": True,
            "status": "running",
            "sessions": [],
            "requests": [],
            "checks": [],
        }
        save(target, report)
        try:
            verify(report, target, args)
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = str(exc)
            save(target, report)
            try:
                collect(report, target, args.artifact)
            except Exception as collection_error:
                report["collection_error"] = str(collection_error)
                report["token_summary"] = usage_summary(report)
                save(target, report)
            print(
                f"FAILED: {exc}. No automatic retry or deletion. Report: {target}", file=sys.stderr
            )
            return 1
    else:
        target = Path(args.report).resolve()
        report = json.loads(target.read_text())
        if args.command == "collect":
            collect(report, target, args.artifact)
        else:
            if not report.get("collected_at"):
                parser.error("Collect evidence before cleanup")
            for s in report["sessions"]:
                validate_session(s)
                if not s.get("deleted_at"):
                    call(report, target, "DELETE", session_path(s))
                    s["deleted_at"] = now()
                    save(target, report)
            print("Verification sessions deleted; local evidence retained.")
    print("Report:", target)
    print(json.dumps(report.get("token_summary", {}), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
