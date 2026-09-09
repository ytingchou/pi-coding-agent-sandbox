# Pi sandbox Helm chart

在 Kubernetes 部署一個 OpenAI Agents SDK API 與多個長駐 Pi coding sandbox Pods。每個 worker 支援多個隔離 session，並支援閒置程序暫停與 ephemeral session 自動清理。

## 部署需求

- Kubernetes **1.33+**、Helm 3、Linux worker nodes。
- 節點、container runtime、kubelet 與 CSI 支援 Pod user namespaces／idmapped mounts，以及巢狀 Bubblewrap。1.33／1.34 需檢查相關 feature gates。
- Admission policy 允許 `hostUsers: false`、`procMount: Unmasked`、指定 capabilities，以及 Unconfined seccomp／AppArmor。此 Chart 不宣稱符合 restricted Pod Security policy。
- 預先建置本專案 API 與 sandbox images，推送至叢集可存取的 registry。Python、skills、extensions 與 Pi packages 都在 image build 時安裝；Pod runtime 不下載套件。
- 可動態建立 PVC 的相容 StorageClass；使用 NetworkPolicy 時需 CNI 支援。

Worker supervisor 在 Pod user namespace 內以 root 運作，用於配置不同 session UID；生成程式以非 root UID 在 Bubblewrap 內執行。沒有 privileged、SYS_ADMIN、hostPath、host network 或 Docker socket。

## 快速安裝

以下指令從專案根目錄執行。Chart 尚未發布至 Helm repository／OCI registry，直接使用本機目錄。

```bash
# 先建置並推送兩個映像；請替換 registry 與固定版本 tag。
docker build -f orchestrator/Dockerfile -t registry.example.internal/pi-api:uv-lifecycle-v1 .
docker build -f sandbox/Dockerfile -t registry.example.internal/pi-worker:uv-lifecycle-v1 .
docker push registry.example.internal/pi-api:uv-lifecycle-v1
docker push registry.example.internal/pi-worker:uv-lifecycle-v1

kubectl create namespace agents
# 檔案需在 Git 外受保護的位置；四個 keys 見下方說明。
kubectl -n agents create secret generic pi-sandbox-secrets \
  --from-env-file=/secure/path/pi-secrets.env
```

另需預先建立外部 MongoDB 連線 Secret（預設 `pi-mongodb`）；本 chart 不部署 MongoDB。

模型／控制平面的既有 Secret 必須包含 `API_TOKEN`、`SANDBOX_TOKEN`、`OPENAI_API_KEY`、`PI_API_KEY`。兩個控制 token 應不同；外層 Agent 與 Pi 使用各自的模型 key。Chart 只引用 Secret，不把秘密寫入 values 或 Helm release 設定；也不會讀取 Compose 的 `.env`。

以 [examples/internal.yaml](examples/internal.yaml) 為起點建立自己的 values，替換 image、模型 URL／model ID、StorageClass 與 egress IP。範例 `192.0.2.x` 是文件示意位址，不能直接使用。

```bash
helm lint charts/pi-sandbox --strict
helm template demo charts/pi-sandbox -n agents --kube-version 1.33.0 \
  -f /secure/path/internal-values.yaml > /tmp/pi-manifests.yaml
kubectl -n agents apply --dry-run=server -f /tmp/pi-manifests.yaml
helm upgrade --install demo charts/pi-sandbox -n agents \
  -f /secure/path/internal-values.yaml --wait --timeout 10m
kubectl -n agents get pods,pvc
kubectl -n agents port-forward service/demo-pi-api 8000:8000
```

`/health` 與 `GET /sandboxes` 僅驗證 HTTP 健康。部署後應在每個 worker 明確建立 session、讀取 resources，並透過 `/pi/prompt` 執行 `/session-info` 驗證真正 Pi／Bubblewrap 啟動，再 DELETE 清理；此 extension command 不需模型 token。

## 架構與持久化

| 資源 | 行為 |
|---|---|
| API StatefulSet | 固定 1 replica／1 Uvicorn process，state PVC 保存對話 SQLite；registry 在外部 MongoDB |
| Worker StatefulSet | 預設 2 replicas，每個 ordinal 各有 state、sessions PVC |
| Worker headless Service | API 直連 Pod DNS，避免 session 被負載平衡到不同 worker |
| API Service | ClusterIP，port 8000；未內建 Ingress 或公開入口 |
| NetworkPolicy | 預設限制 ingress／egress，需自行允許內部模型與業務端點 |

例如 release `demo`、namespace `agents`，worker ID 為 `demo-pi-workers-0`，路由為 `http://demo-pi-workers-0.demo-pi-workers.agents.svc:8080`。Chart 依 replicas 產生 `SANDBOX_ENDPOINTS`。API 不需 Kubernetes API 權限，Pods 不掛載 ServiceAccount token。

不要增加 API replicas 或 Uvicorn workers；目前協調鎖仍在程序內。不要更換 release／namespace／worker 身分而期待舊 binding 自動遷移。

## 設定參考

完整來源：[values.yaml](values.yaml)。所有設定可覆寫；有預設不代表預設 image、模型認證或網路可直接用於公司部署。

| 設定 | 預設 | 說明／建議 |
|---|---|---|
| `images.api.repository` | `pi-agents-sample-api` | 替換成已推送的 API image |
| `images.sandbox.repository` | `pi-agents-sample-sandbox-1` | 替換成已推送的 worker image |
| `images.*.tag` | `latest` | 公司部署使用不可變版本 tag |
| `images.*.pullPolicy` | `IfNotPresent` | 配合固定 tag；依公司 policy 調整 |
| `imagePullSecrets` | `[]` | 私有 registry 認證，例如 `[{name: registry-credentials}]` |
| `existingSecret` | `pi-sandbox-secrets` | 必須預先建立並包含四個 keys，沒有內建可用認證 |
| `api.baseUrl` / `sandbox.baseUrl` | 空字串 | 內部部署兩邊都必須填入完整 API prefix，例如 `/v1` |
| `api.model` / `sandbox.model` | `gpt-4.1-mini` | 分別替換成支援工具呼叫的實際 model ID |
| `api.apiMode` | `responses` | 內部端點通常明確改成 `chat_completions` |
| `sandbox.apiMode` | `chat_completions` | 依 Pi gateway 協定設定 |
| `sandbox.contextWindow` | `128000` | 依實際模型上限設定 |
| `sandbox.maxTokens` | `4096` | 每次模型回應上限，不是整個 run 的 token budget |
| `sandbox.modelCompat` | `{}` | 僅在 gateway 需要時設定，例如 streaming usage 相容性 |
| `sandbox.replicas` | `2` | 可設定 1–100；擴縮容需遵循下節流程 |
| `sandbox.maxSessions` | `8` | 每 Pod 名額；初次公司部署建議從 2 開始量測 |
| `api.resources` | requests `100m/256Mi`；limits `1 CPU/1Gi` | API Pod 資源 |
| `sandbox.resources` | requests `250m/512Mi`；limits `2 CPU/2Gi` | 整個 worker Pod 共用，非每 session 配額 |
| `sandbox.nodeSelector` | Linux nodes | 可補公司 sandbox node pool labels |
| `sandbox.tolerations` | `[]` | 依專用節點 taints 設定 |
| `sandbox.securityContext` | 見 values.yaml | 不應為通過 admission 而移除必要隔離設定 |
| `persistence.storageClass` | 空字串 | 使用叢集預設；需驗證 CSI userns 支援 |
| `persistence.apiSize` | `1Gi` | API state PVC |
| `persistence.workerStateSize` | `1Gi` | 每 worker metadata PVC |
| `persistence.workerSessionsSize` | `10Gi` | 每 worker session 檔案 PVC |
| `lifecycle.cleanupIntervalSeconds` | `60` | API／worker 背景 sweep；0 關閉 |
| `lifecycle.ephemeralIdleTtlSeconds` | `3600` | 新 ephemeral session 的預設 TTL，必須大於 0 |
| `lifecycle.piIdleDisconnectSeconds` | `300` | 暫停閒置 Pi 程序、保留資料；0 關閉 |
| `promptTimeoutSeconds` | `180` | 每次 Pi prompt timeout |
| `terminationGracePeriodSeconds` | `240` | Pod 終止寬限期，不能取代先等待工作完成 |
| `networkPolicy.enabled` | `true` | 必須有支援 NetworkPolicy 的 CNI |
| `networkPolicy.apiEgress` | `[]` | 額外允許 API 到模型 gateway 的規則 |
| `networkPolicy.sandboxEgress` | `[]` | 額外允許 Pi gateway、MinIO 等業務端點 |

預設 NetworkPolicy 允許同 namespace client 到 API、API 到本 release workers，以及 kube-system CoreDNS；沒有預設模型外連權限。CIDR 規則不是 FQDN allowlist，NodeLocal DNS、NAT、公司 CA 必須由平台確認。不同 session 共用 Pod 的網路政策。

## External MongoDB 與 Vault Secret

公司 Vault 流程先同步 Kubernetes Secret，再由 Chart 的 `secretKeyRef` 注入 API env。Chart 不安裝 Vault controller，也不在 values 中保存 DB 認證。

| 設定 | 預設 | 說明 |
|---|---|---|
| `mongodb.existingSecret` | `pi-mongodb` | 同 namespace 的 Vault 同步 Secret |
| `mongodb.uriKey` | `MONGODB_URI` | 必填 Secret key，完整 URI／SRV URI |
| `mongodb.usernameKey` | `MONGODB_USERNAME` | 選填；若 URI 已含帳密可省略 |
| `mongodb.passwordKey` | `MONGODB_PASSWORD` | 選填；與 username 一起提供或省略 |
| `mongodb.database` | `pi_agents` | 實際 database，與 URI path 獨立 |
| `mongodb.authSource` | `admin` | 使用獨立 username/password 時的認證 database |
| `mongodb.timeoutMS` | `5000` | 正整數，連線／選擇 server／socket timeout |
| `mongodb.tlsCASecret` | 空字串 | optional CA Secret，key 為 ca.crt，唯讀掛載到 API |

必須在 `networkPolicy.apiEgress` 允許所有 MongoDB replica-set 成員與 DNS；不需給 worker DB 網路或認證。Vault 輪替 Secret 不會更新既有 Pod env，需依 OnDelete 流程重建 API Pod。API `/ready` 會 ping DB，`/health` 只檢查程序存活。

API 仍不可增加副本，外層 SQLiteSession 對話與 worker SQLite UID metadata 獨立保存。

## Session 自動清理

建立 session 時預設 `retention: managed`，只有明確 DELETE 才刪除。一次性工作可使用：

```json
{"retention":"ephemeral","idle_ttl_seconds":3600}
```

POST 至 `/agents/{agent_id}/sessions`，可另外指定 `sandbox_id`。PATCH `/agents/{agent_id}/sessions/{session_id}/retention` 可切換策略。省略 TTL 時使用 chart 設定的預設；舊 sessions 維持 managed。

兩種策略都可以暫停閒置 Pi 程序，保留 workspace／venv／歷史，下次操作自動恢復。暫停也會終止 namespace 中的背景子程序；需要常駐子程序時設定 `piIdleDisconnectSeconds: 0`。暫停不釋放 session 名額或磁碟，只有完整刪除才釋放。

TTL 清理會跳過忙碌 Agent／session，失敗時保留 `deleting` 記錄供後續重試。帶 API token 呼叫 `POST /sessions/cleanup?dry_run=true` 預覽，`dry_run=false` 實際清理，`GET /sessions/cleanup` 查看最近結果。此為全域管理操作，沒有額外多租戶 RBAC。

刪除會移除生成程式、Pi transcript 與 session 本地套件，不會自動備份；需要證據時先匯出。API Agent 記錄、外層對話歷史與 PVC 不會隨 session TTL 刪除。

## 升級、擴縮容與解除安裝

兩個 StatefulSets 都是 **OnDelete**，`helm upgrade --wait` 不會自動替換既有 Pods：

1. 升級 image／values／Secret 前，停止新工作並等待正在執行的工作完成。
2. Helm upgrade 後，明確刪除受影響 Pods，由 StatefulSet 使用原 PVC 和新 template 重建。
3. 檢查所有 worker 健康、Pi session 能恢復，再恢復流量。

擴容時先增加 replicas，等待新 worker Ready，再於無進行中工作時重建 API Pod，讓 API 採用新增 endpoints。縮容前必須停止新配置，先清理最高 ordinal 上所有 sessions／待刪 binding，再降低 replicas 並重建 API。沒有自動 drain、遷移或 HPA。

PVC 使用 **Retain**：縮容與 `helm uninstall demo -n agents` 不會刪除 PVC。若先縮容卻保留 binding，請恢復原 ordinal 與 PVC；不能把資料默默轉派到別的 worker。只有確認備份與保留政策後才人工刪除 PVC。

## 驗證與進一步文件

從專案根目錄執行：

```bash
make lint-helm
make test-container
```

`lint-helm` 檢查 chart、1／3 workers 的渲染與路由／PVC／Secret／NetworkPolicy，以及非法 values；容器測試驗證真正 Pi 的隔離、暫停、恢復與清理，不呼叫付費模型。

目前通過 Helm 渲染、Kubernetes 1.33 schema 與無外網容器測試；**尚未完成真實叢集安裝驗證**。實際叢集仍須 server dry-run 及逐 worker Pi 啟動驗證。

本專案內另有 [完整 Kubernetes 手冊](../../docs/kubernetes.md)、[Session 生命週期](../../docs/session-lifecycle.md)、[環境設定](../../docs/configuration.md) 與 [執行證據／token tracing](../../docs/session-verification.md)。

MongoDB 設計文件：[Schema 與 index design](../../docs/mongodb-schema.md)、[Registry 架構設計](../../docs/mongodb-registry-architecture.md)。Document models 使用 Pydantic，來源為 [`orchestrator/documents.py`](../../orchestrator/documents.py)。
