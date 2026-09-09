"""Render and validate chart invariants without a cluster or credentials."""

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

CHART = "charts/pi-sandbox"


def main():
    subprocess.run(["helm", "lint", "--strict", CHART], check=True)
    for replicas in (1, 3):
        rendered = subprocess.check_output(
            [
                "helm",
                "template",
                "demo",
                CHART,
                "--kube-version",
                "1.33.0",
                "--namespace",
                "agents",
                "-f",
                f"{CHART}/examples/internal.yaml",
                "--set",
                f"sandbox.replicas={replicas}",
            ],
            text=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rendered.yaml"
            path.write_text(rendered)
            subprocess.run(
                ["yamllint", "--strict", "-c", ".yamllint-rendered.yaml", str(path)], check=True
            )
        docs = list(yaml.safe_load_all(rendered))
        sets = {d["metadata"]["name"]: d for d in docs if d and d["kind"] == "StatefulSet"}
        api = sets["demo-pi-api"]["spec"]
        worker = sets["demo-pi-workers"]["spec"]
        assert api["replicas"] == 1 and worker["replicas"] == replicas
        pod = worker["template"]["spec"]
        assert pod["hostUsers"] is False and pod["automountServiceAccountToken"] is False
        assert pod["containers"][0]["securityContext"]["procMount"] == "Unmasked"
        assert {v["metadata"]["name"] for v in worker["volumeClaimTemplates"]} == {
            "state",
            "sessions",
        }
        assert worker["persistentVolumeClaimRetentionPolicy"] == {
            "whenDeleted": "Retain",
            "whenScaled": "Retain",
        }
        api_env = {e["name"]: e for e in api["template"]["spec"]["containers"][0]["env"]}
        endpoints = json.loads(api_env["SANDBOX_ENDPOINTS"]["value"])
        assert endpoints == {
            f"demo-pi-workers-{i}": f"http://demo-pi-workers-{i}.demo-pi-workers.agents.svc:8080"
            for i in range(replicas)
        }
        for spec in (api, worker):
            env = spec["template"]["spec"]["containers"][0]["env"]
            for item in env:
                if item["name"].endswith(("_TOKEN", "_API_KEY")):
                    assert "secretKeyRef" in item["valueFrom"]
        assert len([d for d in docs if d and d["kind"] == "NetworkPolicy"]) == 2
    for invalid in ("sandbox.replicas=0", "lifecycle.ephemeralIdleTtlSeconds=0"):
        result = subprocess.run(
            ["helm", "template", "demo", CHART, "--kube-version", "1.33.0", "--set", invalid],
            capture_output=True,
        )
        assert result.returncode, f"Chart accepted {invalid}"
    print("Helm checks passed: 1/3 workers, routing, PVCs, secrets, policies and invalid values")


if __name__ == "__main__":
    main()
