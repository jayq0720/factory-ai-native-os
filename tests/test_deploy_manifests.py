"""生产部署资产（Dockerfile / Docker Compose / Kubernetes Kustomize 清单）安全与完整性校验。"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def test_dockerfile_non_root_and_healthcheck() -> None:
    """验证 Dockerfile 以非 root 用户运行、预建 blobs/backups 目录并配置 HEALTHCHECK。"""
    txt = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "USER fos" in txt
    assert "/data/blobs" in txt
    assert "/data/backups" in txt
    assert "HEALTHCHECK" in txt
    assert "/healthz" in txt


def test_k8s_manifests_security_and_structure() -> None:
    """验证 deploy/k8s 全部 YAML 合法、无明文密钥、强制非 root 与双探针、CronJob 防并发重叠。"""
    k8s_dir = REPO / "deploy" / "k8s"
    kust = yaml.safe_load((k8s_dir / "kustomization.yaml").read_text(encoding="utf-8"))
    assert kust["kind"] == "Kustomization"

    docs: list[dict] = []
    for rel in kust["resources"]:
        p = k8s_dir / rel
        assert p.exists(), f"kustomization 引用的文件不存在：{rel}"
        raw = p.read_text(encoding="utf-8")
        for forbidden in ("change-me", "sk-", "fos_"):
            assert forbidden not in raw, f"{rel} 疑似包含明文凭据标记 {forbidden}"
        docs.extend(d for d in yaml.safe_load_all(raw) if d)

    by_kind: dict[str, list[dict]] = {}
    for d in docs:
        by_kind.setdefault(d["kind"], []).append(d)

    assert {"Namespace", "ConfigMap", "Deployment", "Service", "CronJob"}.issubset(by_kind)

    dep = by_kind["Deployment"][0]
    pod_spec = dep["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    c = pod_spec["containers"][0]
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert "ALL" in c["securityContext"]["capabilities"]["drop"]
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"

    cron_names = {cj["metadata"]["name"] for cj in by_kind["CronJob"]}
    assert {"fos-watchdog", "fos-backup-ledger", "fos-verify-ledger"}.issubset(cron_names)
    for cj in by_kind["CronJob"]:
        assert cj["spec"]["concurrencyPolicy"] == "Forbid"
        t_spec = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert t_spec["securityContext"]["runAsNonRoot"] is True
        cmd = t_spec["containers"][0]["command"]
        assert cmd[0] == "fos"
