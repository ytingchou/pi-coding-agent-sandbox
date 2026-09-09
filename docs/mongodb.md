# MongoDB Session Manager、Compose 與 Vault Secret

Session Manager 現在使用 MongoDB 保存 Agent 與 session registry。Python driver 使用 thread-safe PyMongo client／連線池，資料庫操作透過 `asyncio.to_thread` 執行，避免阻塞 FastAPI event loop。MongoDB 不可用時回傳一般化 503，不把連線 URI／帳密放進 API 錯誤。

## 保存哪些資料

| 位置 | 資料 |
|---|---|
| MongoDB `agents` | 邏輯 Agent ID |
| MongoDB `bindings` | session ID、Agent／sandbox 路由、status、retention、TTL、活動時間、expires_at |
| API `/state/conversations.sqlite` | Agents SDK 對話歷史，仍使用 SQLiteSession |
| Worker `/state/worker.sqlite` | 本 Pod 的 UID slot 與 session 活動 metadata |
| Worker `/sessions` | 每 session 的 workspace、venv、Pi transcript 與生成檔案 |

Session Manager 僅使用 MongoDB registry。MongoDB 不代表 API 現在可多副本：Agent lock／HTTP 協調仍在程序內，API 保持 **1 replica、1 Uvicorn process**。

`bindings._id` 為 session UUID，另建立 Agent／建立時間、sandbox、status、retention／expires_at 查詢 indexes。**沒有 MongoDB TTL index**：仍須先停止 worker 程序並刪除目錄，成功才移除 binding。DB 或 worker 不可用時保留可恢復狀態；不會把生成程式的 prompt 自動重送。

## 本機 Compose

全新安裝：

```bash
cp -n .env.example .env
# 編輯模型設定；MongoDB 本機設定已有 sample defaults。
docker compose up --build -d --wait
docker compose exec api python scripts/smoke.py
docker compose exec api python scripts/demo_resources.py
# 有模型 key 且要執行付費模型時才執行：
docker compose exec api python scripts/demo.py
```

Compose 包含 `mongo:8.0.16`、`mongodb-data` named volume、健康檢查及 `mongodb/init.js` 的應用帳號初始化。MongoDB 沒有 host port，只能在 Compose network 存取。預設 API 使用 `pi_demo`，只授予 `pi_agents` 的 `readWrite`；root 帳號只供初始化／本機測試。sample 密碼只適用本機 demo，共享環境請更換。

初始化 script **僅對空資料 volume 執行**。修改 `.env` 的帳號、密碼或 database 不會更新 MongoDB 已有使用者；請依 MongoDB 管理流程更新使用者，再重新建立 API container。不要為改密碼刪掉資料 volume。`docker compose down` 保留資料；`down -v` 會刪除 MongoDB 與所有原有 sample volumes。

API 的 `/health` 是程序存活；`/ready` 會 ping MongoDB，Compose 與 Helm readiness 使用 `/ready`。首次啟動會建立 indexes；API 帳號需要該 database 的 read/write 與 createIndex 權限。MongoDB 不可用時 readiness 失敗。

## 環境變數

| 環境變數 | Direct process／Helm | Compose 預設 | 說明 |
|---|---|---|---|
| `MONGODB_URI` | 必填，無 fallback | `mongodb://mongodb:27017` | 支援 MongoDB URI／SRV URI；外部 replica set 的 hosts、TLS、replicaSet 等由 URI 設定 |
| `MONGODB_DATABASE` | 選填 `pi_agents` | `pi_agents` | 使用的 database，不由 URI path 決定 |
| `MONGODB_USERNAME` | 選填，須與 password 成對 | `pi_demo` | 獨立 env 認證；不需自行 percent-encode |
| `MONGODB_PASSWORD` | 選填，須與 username 成對 | `pi-demo-change-me` | 可由 Vault 同步的 Secret 注入 |
| `MONGODB_AUTH_SOURCE` | 選填 `admin` | `pi_agents` | 獨立 username/password 啟用時使用；URI 內認證則由 URI 控制 |
| `MONGODB_TIMEOUT_MS` | 選填 `5000` | `5000` | 正整數，server selection／connect／socket timeout，建議先保留 5 秒 |
| `MONGODB_TLS_CA_FILE` | 選填，預設未設定 | 未自動掛載 | 指定 CA 檔案會啟用 TLS；URI `tls=true` 且未提供時使用系統信任鏈 |
| `MONGO_ROOT_USERNAME` | Helm 不使用 | `root` | 本機 MongoDB 初始化用 |
| `MONGO_ROOT_PASSWORD` | Helm 不使用 | `mongo-root-demo-change-me` | 本機 MongoDB 初始化用，API 不使用 root |

可以把帳密放在 URI（注意 URI percent-encoding），這時不要再設定 username/password env（Compose 請在 .env 將兩者明確設為空字串，避免使用本機 defaults）；公司部署建議分開 keys 注入。不要使用 `tlsAllowInvalidCertificates` 等方式略過驗證。只設定 URI 的 database path 不會覆蓋 `MONGODB_DATABASE`，請明確設定後者。

## Helm：外部 cluster + Vault → Kubernetes Secret → env

Chart **不部署 MongoDB**。公司透過既有 Vault 整合 controller／流程同步 Kubernetes Secret，Chart 只引用 Secret。沒有安裝 Vault Operator、CRD、Vault policy 或憑證輪替控制器。

假設 Secret 名稱 `vault-pi-mongodb`，同 namespace 中包含：

```text
MONGODB_URI       mongodb://mongo-0.internal:27017,mongo-1.internal:27017/?replicaSet=rs0&tls=true
MONGODB_USERNAME  <Vault 提供的帳號>
MONGODB_PASSWORD  <Vault 提供的密碼>
```

values 設定只有 Secret 名稱與非秘密的參數：

```yaml
mongodb:
  existingSecret: vault-pi-mongodb
  uriKey: MONGODB_URI
  usernameKey: MONGODB_USERNAME
  passwordKey: MONGODB_PASSWORD
  database: pi_agents
  authSource: admin
  timeoutMS: 5000
  tlsCASecret: company-mongodb-ca  # optional，Secret key 為 ca.crt
```

`uriKey` 必須存在；username/password Secret keys 可省略，但應一起省略（用於 URI 已含帳密的配置）。Env 只注入 API，worker／Pi session 不取得 MongoDB 認證。若使用 `tlsCASecret`，Chart 將 `ca.crt` 唯讀掛載至 API `/etc/mongodb-ca/ca.crt`，並設定對應 env；預設空字串不掛載。

`networkPolicy.apiEgress` 必須允許外部 MongoDB **所有 replica-set 成員／SRV 發現目標**與 DNS；只允許 seed host 可能在拓撲變更後失效。Chart 的 internal values 有示意規則，需要換成公司實際 CIDRs／ports。模型 gateway egress 仍須另外允許。

Vault 更新 Kubernetes Secret 後，**現有 Pod 的 env 不會自動更新**。本 chart 為 OnDelete；安排停止新工作、等待現有工作結束，再重建 API Pod，讓它重新載入認證並建立連線池。動態帳密的 lease／輪替時間應預留新舊憑證重疊窗口。Vault 與外部 MongoDB 的實際整合需由公司環境驗證。

## 測試

```bash
make check          # registry 單元測試預設使用 mongomock
make test-container # 無外網 Pi 整合；registry 使用 mock
make test-mongodb   # 本機真實 MongoDB，獨立 pi_test_* databases，結束後清除
```

`test-mongodb` 固定連本機 Compose 的 `mongodb`，使用本機 root 帳號建立／移除測試 databases，不接收外部 URI。一般 pytest 若設定 `TEST_MONGODB_URI` 會使用該測試 cluster；**不要指向正式 cluster**，其帳號需能建立／刪除 `pi_test_*` databases。測試涵蓋唯一 ID、查詢索引、重建 manager 後持久化、所有權、TTL 清理、失敗重試；不使用模型 tokens。

MongoDB 設計文件：[Schema 與 index design](mongodb-schema.md)、[Registry 架構設計](mongodb-registry-architecture.md)。Document models 使用 Pydantic，來源為 [`orchestrator/documents.py`](../orchestrator/documents.py)。
