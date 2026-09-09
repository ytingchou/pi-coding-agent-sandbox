# Kubernetes 多 sandbox Helm 部署

Chart 位於 [`charts/pi-sandbox`](../charts/pi-sandbox/README.md)，適用於 Kubernetes **1.33+**、Helm 3。需要先建置並推送本專案 API／sandbox images 至叢集能存取的 registry。Chart 不安裝 Python、npm 或 Pi packages；skills、extensions、Pi packages 與 Python 應用套件沿用 image build 的預裝內容。

## 架構與必要條件

- 一個 API StatefulSet 固定 **1 replica**，獨立 state PVC 保存對話 SQLite；Agent／session registry 保存在外部 MongoDB。
- 一個 worker StatefulSet，預設 **2 replicas**。每個 ordinal 有獨立 `state` 和 `sessions` PVC；API 透過 headless Service 的 Pod DNS 直接呼叫指定 worker，不把 session request 丟到共用負載平衡 Service。
- 例如 release `demo`、namespace `agents`：`demo-pi-workers-0.demo-pi-workers.agents.svc:8080`；sandbox ID 是 `demo-pi-workers-0`。Chart 依 replicas 產生 `SANDBOX_ENDPOINTS`，不需 Kubernetes API discovery 權限。
- 兩種 workload 都使用 `OnDelete` 更新策略，避免 Helm upgrade 自動中斷執行中的 sessions。必須依下方程序安排 Pod 重建；`helm upgrade --wait` 本身不代表 Pod 已採用新設定。
- PVC 在縮容／解除安裝後 **Retain**；session TTL 刪檔不刪 PVC。請監控空間及實際備份，不能將 PV snapshot 當成跨 MongoDB 與各 PVC 的自動一致性備份。

Worker 必須能使用巢狀 user/mount/PID namespaces 和 `/proc`。Chart 設定 `hostUsers: false`、`procMount: Unmasked`，需要 Linux kernel、container runtime、kubelet、CSI volume 的 Pod user namespace／idmapped mount 支援。Kubernetes 1.33/1.34 叢集也需確認相關 feature gates；1.36 起 Pod user namespaces 為 GA。Pod Security／公司 admission policy 必須接受指定安全設定。

Supervisor 在 **Pod user namespace 內以 root** 執行，保留 SETUID／SETGID／CHOWN／DAC_OVERRIDE／KILL；生成程式仍以獨立非 root UID 在 Bubblewrap 內執行。沒有 privileged、SYS_ADMIN、hostPath、host network 或 Docker socket。seccomp 和 AppArmor 預設 Unconfined 是為現有 Bubblewrap 所需，並非符合 restricted policy 的宣稱；可經平台團隊驗證後提供 Localhost profiles。若叢集禁止這些條件，需調整 worker runtime／隔離方案，不能移除隔離繼續執行。

官方參考：[StatefulSet](https://kubernetes.io/docs/concepts/workloads/controllers/statefulset/)、[Pod user namespaces](https://kubernetes.io/docs/concepts/workloads/pods/user-namespaces/)、[security context](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)。

## 建置與安裝

```bash
# 修改 registry 與不可變版本 tag，不要將 secret 放進 build args。
docker build -f orchestrator/Dockerfile -t registry.example.internal/pi-api:uv-lifecycle-v1 .
docker build -f sandbox/Dockerfile -t registry.example.internal/pi-worker:uv-lifecycle-v1 .
docker push registry.example.internal/pi-api:uv-lifecycle-v1
docker push registry.example.internal/pi-worker:uv-lifecycle-v1

kubectl create namespace agents
```

另外必須預先備妥外部 MongoDB 與 Vault 同步的 DB Secret（預設 `pi-mongodb`）；URI／username／password env、CA、egress與輪替見 [MongoDB 手冊](mongodb.md)。Chart 不建立 MongoDB。

在該 namespace 以公司 Secret 管理流程建立 `pi-sandbox-secrets`，含四個必填 keys：`API_TOKEN`、`SANDBOX_TOKEN`、`OPENAI_API_KEY`、`PI_API_KEY`。兩個控制 token 應不同；外層模型與 Pi 模型 key 分別設定，Chart 不將外層 key 傳入 worker。Secret 可由 External Secrets／公司平台建立；Chart 不代建明文 Secret，避免放入 Helm release values。

例如已有受保護、Git 外的四行 `KEY=value` 檔，可使用：

```bash
kubectl -n agents create secret generic pi-sandbox-secrets --from-env-file=/secure/path/pi-secrets.env
```

複製 [internal.yaml](../charts/pi-sandbox/examples/internal.yaml) 到自己的設定檔，填入 images、兩組 base URL／model name、內部 gateway egress CIDRs、StorageClass。其 IP 是文件專用示意值，不能直接用於正式連線。需拉取私人 image 時設定 `imagePullSecrets: [{name: registry-credentials}]`。

```bash
helm lint charts/pi-sandbox --strict
helm template demo charts/pi-sandbox --namespace agents --kube-version 1.33.0 \
  -f /secure/path/internal-values.yaml > /tmp/pi-manifests.yaml
# 連得到目標叢集時先驗證 admission；server dry-run 不保存 workload。
kubectl -n agents apply --dry-run=server -f /tmp/pi-manifests.yaml
helm upgrade --install demo charts/pi-sandbox -n agents \
  -f /secure/path/internal-values.yaml --wait --timeout 10m
kubectl -n agents get pods,pvc
kubectl -n agents port-forward service/demo-pi-api 8000:8000
```

設定摘要與 optional/default 規則見 [configuration.md](configuration.md)。本機 `.env` 由 Compose 使用，Helm **不會自動載入 `.env`**。

## 模型與網路

`api.baseUrl/api.model/api.apiMode` 和 `sandbox.baseUrl/sandbox.model/sandbox.apiMode` 分別設定；內部 OpenAI 相容端點應兩邊都明確設定 URL 和協定，通常 `chat_completions`。`sandbox.contextWindow/maxTokens/modelCompat` 對應 Pi 自訂 provider。CA 信任鏈需預先打入 images（Python 與 Node 都要驗證）；不要用停用 TLS 檢查的方式處理公司 CA。

NetworkPolicy 預設開啟：允許同 namespace client 到 API、API 到本 release worker、到 kube-system CoreDNS 的 DNS；其餘 egress 不允許。必須透過 `networkPolicy.apiEgress`／`sandboxEgress` 加入兩組模型 gateway 與必要 MinIO／業務端點。NetworkPolicy 需 CNI 支援才會生效，CIDR 規則不是 FQDN allowlist，DNS／NAT／NodeLocal DNS 的路徑需由平台驗證。若使用不同 DNS labels 或公司統一政策，調整模板或在已有等效政策下關閉 chart policy。Worker 中的任意 session 共用 Pod network 權限，不是每個 session 獨立的 NetworkPolicy。

## 資源、清理與擴縮容

`replicas * maxSessions` 是理論 session 名額；managed／暫停／尚未完成刪除的 session 仍占名額。Pod 預設 worker limits 為 2 CPU／2Gi，requests 為 250m／512Mi；應從每 Pod 2 sessions 的設定量測。PID 總量限制需平台 kubelet 的 Pod PID policy，K8s 沒有這裡 Compose `pids_limit` 的直接 Pod 欄位。

Session 使用 `managed`（預設）或 `ephemeral`；閒置 Pi 預設 300 秒暫停，ephemeral 預設 3600 秒刪除，每 60 秒 sweep。建立／切換、dry-run、清理證據與例外情況見 [Session 生命週期](session-lifecycle.md)。啟用清理不會把現有 managed sessions 改成 ephemeral。

擴容步驟：

1. 將 `sandbox.replicas` 增加後 Helm upgrade，等待新增 workers Ready。
2. 暫停外層新請求並等待既有工作完成，刪除 `demo-pi-api-0` Pod 讓 StatefulSet 以原 PVC、新 endpoints 重建。Chart 使用 OnDelete，不會自動完成這一步。
3. 透過帶 API token 的 `GET /sandboxes` 確認所有 worker，再恢復流量。

縮容前：停止新增工作／配置，依 `GET /agents/{agent_id}/sessions` 盤點將被移除的最高 ordinal 上所有 binding。先匯出證據、明確刪除或等待 ephemeral TTL，確認無 pending deletion，再縮 replicas 並依上述方式重建 API。沒有 session migration、自動 drain 或 HPA；若忽略這些步驟，原 binding 會保留但 worker 不可用，必須恢復原 ordinal 與 PVC。

更新 image／Secret／模型設定時，也應先停止新工作、等工作完成，再逐一刪除受影響 Pod 讓它套用新 StatefulSet template。避免更改 release 名稱、namespace、worker 命名；這些值決定 routing 身分。API 不可同時運行兩份，也不要調高 Uvicorn workers。關閉服務不等於刪資料；只有確認備份與資料保留要求後才人工刪除 retained PVC。

## 驗證範圍與故障處理

```bash
make lint-helm      # Helm lint、1/3 worker 渲染、DNS/PVC/Secret/Policy 不變條件與錯誤 values
make test-container # 無外網的真正 Pi、隔離、gateway、生命週期整合測試
# 可選：對渲染結果執行 Kubernetes schema 驗證；需要取得工具與公開 schema。
docker run --rm -v /tmp/pi-manifests.yaml:/manifest.yaml:ro \
  ghcr.io/yannh/kubeconform:v0.7.0 -strict -summary -kubernetes-version 1.33.0 /manifest.yaml
```

MongoDB 版本完成主機 36 passed／5 skipped、無外網 Linux 容器 41 passed；真實本機 MongoDB 17 passed／1 skipped。Helm 會檢查 1／3 worker 渲染與不合法 values。

目前已驗證本機 Helm 渲染與 Kubernetes 1.33 schema；本機 Kubernetes API 未啟動，**尚未完成真實叢集安裝驗證**。Compose Linux 容器通過不代表公司 CSI、CNI、admission 或巢狀 userns 必然相容。

實際安裝後，先檢查 `GET /sandboxes`（只證明 worker HTTP 健康），再在各 sandbox 明確建立一個 session 並呼叫 `/resources`，確認 Pi／Bubblewrap 啟動。透過 `/pi/prompt` 送 `/session-info` 可驗證無模型 extension command；最後 DELETE 清理。這些操作不需付費模型。若 startup 回傳 503，查看 worker logs 與 Pod events，優先檢查 userns、procMount、AppArmor、seccomp、PVC uidmap／權限與 image 版本。只有需要驗證公司模型端點時，才依 [session-verification.md](session-verification.md) 執行 live 驗證並保存 usage。
