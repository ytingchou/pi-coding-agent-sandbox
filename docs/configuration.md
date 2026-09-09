# 環境變數設定參考

本文件逐項對應 [`.env.example`](../.env.example)、[`compose.yaml`](../compose.yaml) 和目前程式的預設行為。**內部 gateway 部署至少要明確設定兩層的 base URL、model 和可用金鑰，並更換兩組控制 API token。** 表格中的「選填」表示程式有預設或 fallback，不表示該預設適合公司環境。

## 設定從哪裡生效

Compose 讀取 `.env` 作變數替換，再把 `compose.yaml` 列出的值傳入 container。Shell 中已 export 的同名變數可能覆蓋 `.env`；遇到設定不符時先檢查是否有舊的 export。不要列印含金鑰的完整環境或 Compose 展開結果。

這份 Compose 的 `${VAR:-default}` 在變數未設定或留空時使用 default。以下預設值以透過本專案 Compose 啟動為準；K8s 不會自動讀取 `.env`，應以 StatefulSet env／Secret（見 [Helm 部署手冊](kubernetes.md)） 注入。

- **條件必填**：執行特定功能時需要提供。
- **選填**：可省略，會採預設／fallback。
- **部署必改**：技術上可使用範例值，但共用／公司環境應設定自己的值。

外層與 pi 可使用完全不同的 gateway、API key 和 model name。這裡的「OpenAI 相容」是模型 HTTP API（Chat Completions／Responses）的相容性；僅有 OpenAPI／Swagger schema 本身不足以執行此協定。

## 1. 外層 Agents SDK：API container

| 變數 | 是否必填 | 範例檔／未設定時的行為 | 建議 |
|---|---|---|---|
| `OPENAI_API_KEY` | 模型 `/run` 條件必填 | 範例留空，沒有可用金鑰預設；空值呼叫 `/run` 回 503 | 使用 gateway 配發給外層 Agent 的 key；K8s 用 Secret。只跑無模型測試可留空。若內部 gateway 確實不驗證認證，仍需填非空 placeholder，才能通過此 sample 的檢查 |
| `OPENAI_MODEL` | 選填；內部部署應明填 | `gpt-4.1-mini` | 填 gateway 實際支援且可 tool calling 的 model ID；範例值只是 sample 預設，不代表內部存在此模型 |
| `OPENAI_BASE_URL` | 選填；內部 gateway 條件必填 | 範例留空；使用 `https://api.openai.com/v1` | 公司環境明填內部 API prefix，例如 `http://llm-gateway.models.svc.cluster.local:8000/v1`，以公司實際協定／位址為準 |
| `OPENAI_API_MODE` | 選填 | 範例留空；有 `OPENAI_BASE_URL` 時為 `chat_completions`，無 base URL 時為 `responses` | 內部環境建議明填 `chat_completions`；若 gateway 明確支援 Responses 才填 `responses` |

API key 與下方 `API_TOKEN` 是不同用途，不能混用。Base URL 不可含 username/password、query 或 fragment，也不要填完整 `/chat/completions`／`/responses` 路徑。

## 2. Pi 模型：sandbox worker containers

| 變數 | 是否必填 | 範例檔／未設定時的行為 | 建議 |
|---|---|---|---|
| `PI_MODEL` | 選填；內部部署應明填 | `gpt-4.1-mini`；**不繼承 OPENAI_MODEL** | 填 coding 模型 ID，需支援 tool calling 與 streaming tool calls；兩層模型可相同或不同 |
| `PI_BASE_URL` | 選填；內部 gateway 條件必填 | 範例留空；使用公網 OpenAI；**不繼承 OPENAI_BASE_URL** | 即使兩層使用同一個 gateway，也要分別填兩個 base URL |
| `PI_API_KEY` | 選填，但 pi 模型呼叫需有效認證 | 範例留空；fallback 到 worker 的 `OPENAI_API_KEY` | 同 key 可留空；公司環境建議獨立 key，方便權限及用量管理。worker 若只注入 PI_API_KEY 也可運作。無認證 gateway 可用非空 placeholder |
| `PI_API_MODE` | 選填 | 範例留空；有 PI_BASE_URL 時為 `chat_completions`；兩者都空時使用 pi 內建 OpenAI provider（預設 gpt-4.1-mini 使用 Responses） | 建議與實際 gateway 協定一致並明填；**不繼承 OPENAI_API_MODE** |
| `PI_CONTEXT_WINDOW` | 選填；只作用於自訂 pi provider | `128000`；內建 provider 模式忽略此值、沿用內建模型資料 | 必須改成模型／gateway 的實際 context 上限。只有服務確實支援時才能沿用 128000；這是告知 pi 的模型資料，不會擴大伺服器能力 |
| `PI_MAX_TOKENS` | 選填；只作用於自訂 pi provider | `4096`；內建 provider 模式忽略此值 | 4096 可作一般程式生成的起點；小任務可從 2048 試起，程式常被截斷再調高。須滿足 `0 < PI_MAX_TOKENS <= PI_CONTEXT_WINDOW` 且不超過 gateway 上限 |
| `PI_MODEL_COMPAT` | 選填；只作用於自訂 pi provider | 範例留空，等同沒有額外 overrides | 先留空；只有 gateway 錯誤或文件表明需要時才設定合法 JSON object。不要直接關掉 streaming usage，否則可能無法統計 pi token |

**自訂 pi provider** 指 PI_BASE_URL 或 PI_API_MODE 至少有一個非空，此時產生 `sandbox-openai` provider。只有設定 context、max tokens 或 compat，而 base URL／mode 都留空，不會啟用自訂 provider。

`PI_MAX_TOKENS` 是 pi 每次模型輸出的上限設定，不是整個 session、整個 `/run`、外層模型或兩層合計的 token 預算。一次任務可能包含多次模型呼叫，歷史／套件清單也會占用輸入 context。

### 協定選擇與 fallback

| Base URL | API mode | 有效行為 |
|---|---|---|
| 兩者都空 | 空 | 外層用公網 OpenAI Responses；pi 用內建 OpenAI provider |
| 指定內部 URL | 空 | 該層預設使用 Chat Completions |
| 指定 URL | `chat_completions` | 呼叫該 URL 下的 `/chat/completions` |
| 指定 URL | `responses` | 呼叫該 URL 下的 `/responses` |
| 空 | 明填合法 mode | 仍以公網 `https://api.openai.com/v1` 為 base URL；pi 啟用自訂 provider |

每層獨立套用以上規則；沒有端點失敗後切回公網的 fallback。**不能只填 API mode 而忘記內部 base URL。** 唯一跨兩層的憑證 fallback 是 `PI_API_KEY → OPENAI_API_KEY`；base URL、model、mode 都不互相繼承。

Chat Completions 的 pi 相容預設為：`supportsStore=false`、`supportsDeveloperRole=false`、`supportsReasoningEffort=false`、`maxTokensField="max_tokens"`。可用 `PI_MODEL_COMPAT` 覆蓋，例如只有 gateway 不接受 streaming usage 選項時才使用：

```dotenv
PI_MODEL_COMPAT={"supportsUsageInStreaming":false}
```

`PI_MODEL_COMPAT` 不會改變外層 Agents SDK 的請求參數。gateway 必須能接受外層 SDK 的 tool schema；本專案沒有為所有相容服務提供自動參數轉換。

## 3. 控制 API、port、timeout 與容量

| 變數 | 是否必填 | 預設值 | 範圍與建議 |
|---|---|---|---|
| `API_TOKEN` | 選填；公司／共用環境部署必改 | `local-demo-change-me` | Client → API 的 Bearer token。建議用 32 bytes 隨機值產生的 hex 字串；所有呼叫 client 要同步設定。與模型 key 分開 |
| `SANDBOX_TOKEN` | 選填；公司／共用環境部署必改 | `worker-demo-change-me` | API → worker 的 Bearer token。使用另一組隨機值；API 與所有 worker 必須一致，不要與 API_TOKEN 共用 |
| `API_PORT` | 選填 | `8000` | 只改 Compose 主機的 `127.0.0.1:<port> → api:8000` 映射；建議留 8000，衝突時改 8001 等未使用 port。K8s Service port 不受此值控制 |
| `PROMPT_TIMEOUT` | 選填 | `180` 秒 | 限制單次 pi prompt；Session Manager 的 worker HTTP timeout 為此值加 60 秒、connect timeout 為 5 秒。先用 180；內部模型排隊／生成較慢時，可量測後調至 300。不是整個外層 Agent run 的總 timeout |
| `MAX_SESSIONS` | 選填 | 每個 worker `8` | 包含閒置與配置中的 sessions，**不是正在執行的模型請求數**。目前每 worker 2 GiB／2 CPU，初次公司部署可保守從 2 開始，再依實測記憶體與工作量增加；8 是範例值，沒有吞吐或容量保證 |

Timeout 應為正數，sessions 為正整數；`MAX_SESSIONS=0` 會拒絕新 session，不能拿來表示無上限。變更上限不會自動刪除既有 sessions。pi prompt 超時會停止該 session 程序、保留檔案，不會回滾已發生的副作用。

產生兩組控制 token（執行兩次，分別填入）：

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

## 4. 建議設定組合

### 本機 OpenAI demo

只跑啟動／離線工具測試可維持範例空 key。若要呼叫真實模型，在 `.env.example` 的基礎上填入 OPENAI_API_KEY 即可，pi 會沿用它；base URL、mode、compat 可留空，其餘沿用範例。gpt-4.1-mini 是這個 sample 的相容預設，不是對其他模型可用性或價格的推薦。

### 公司內部 gateway（建議起點）

```dotenv
OPENAI_BASE_URL=http://coordinator-gateway.models.svc.cluster.local:8000/v1
OPENAI_API_KEY=replace-with-outer-key
OPENAI_MODEL=replace-with-real-coordinator-model-id
OPENAI_API_MODE=chat_completions

PI_BASE_URL=http://coding-gateway.models.svc.cluster.local:9000/v1
PI_API_KEY=replace-with-pi-key
PI_MODEL=replace-with-real-coding-model-id
PI_API_MODE=chat_completions
# 32768 只是此範例假設，必須換成所選模型／gateway 的真實上限
PI_CONTEXT_WINDOW=32768
PI_MAX_TOKENS=4096
PI_MODEL_COMPAT=

API_TOKEN=replace-with-first-random-token
SANDBOX_TOKEN=replace-with-second-random-token
API_PORT=8000
PROMPT_TIMEOUT=180
MAX_SESSIONS=2
```

範例中的 URLs、model IDs 和 keys 都需要替換；依公司政策使用 HTTPS 與公司 CA。先驗證一次單 session 的 tool call／Python 執行，再調整 session 數、輸出上限與 timeout，不要同時放大所有限制。這些容量數字是保守起點，不是效能量測結果。

K8s 建議分配：

- API StatefulSet：OPENAI_*、API_TOKEN、SANDBOX_TOKEN、PROMPT_TIMEOUT，以及實際 SANDBOX_ENDPOINTS。
- Worker StatefulSet：PI_*、SANDBOX_TOKEN、PROMPT_TIMEOUT、MAX_SESSIONS、各自 SANDBOX_ID；需要 key fallback 時另外注入 OPENAI_API_KEY。
- keys／tokens 放 Secret，其餘可用 ConfigMap。API_PORT 是本機 Compose 設定，不必放進 worker。

公司網路部署、預裝 Python 套件與無外網驗證見 [離線 sandbox 文件](python-packages.md)。

## 5. 不屬於 `.env.example` 的設定

| 設定 | 目前來源／預設 | 如何修改 |
|---|---|---|
| `PYTHON_PACKAGE_INSTALLER` | Docker build ARG，預設 `uv` | `docker compose build --build-arg PYTHON_PACKAGE_INSTALLER=pip sandbox-1 sandbox-2`；只寫入 `.env` 不會自動生效 |
| 預裝 Python 套件 | `pyproject.toml` 的 `sandbox` dependency group 與 `uv.lock` | 修改後重新 build、部署 image；不是 runtime 環境變數 |
| `SANDBOX_ENDPOINTS` | compose.yaml 的 JSON，固定指向 sandbox-1、sandbox-2 | 增加 worker 時修改 Compose；Helm 依 replicas 產生固定 Pod DNS；只放 `.env` 不會覆蓋目前固定值 |
| `SANDBOX_ID` | Compose 每個 worker 的固定名稱 | 每個 worker 身分需唯一，並與 endpoints 的 key 一致 |
| `OPENAI_AGENTS_DISABLE_TRACING` | API Compose 固定為 `1` | 雲端 tracing 目前停用；本機 session tracing 不受影響。只放 `.env` 不會改變固定值 |
| `PIP_NO_INDEX`、`UV_OFFLINE`、`UV_PYTHON_DOWNLOADS` | isolation.py 分別固定為 `1`、`true`、`never` | 由程式控制 session 的預設安裝政策，不從 worker 任意環境變數繼承 |
| Worker CPU／RAM／PID 上限 | Compose 的 cpus=2、mem_limit=2g、pids_limit=512 | 修改 Compose 或 K8s resources；MAX_SESSIONS 不會自動增加這些資源 |

## 6. 套用與確認

```bash
# 首次設定；已有 .env 不要覆蓋
cp -n .env.example .env
chmod 600 .env
# 編輯 .env 後重建容器設定；單純 restart 不會重新套用新 env
docker compose up -d --wait
docker compose ps
```

只改 `.env` 不需要重新 build；改預裝套件或 Dockerfile 才需要 `docker compose up --build -d --wait`。K8s Chart 使用 OnDelete：更新 Helm values／Secret 後，先等工作完成再明確重建對應 Pods，詳見 [部署手冊](kubernetes.md)；已啟動的程序不會自動載入新的環境值。先完成正在執行的任務再更新，以免中斷 session。

可用 [session 驗證工具](session-verification.md) 檢查端點的實際 tool calling；`verify` 會呼叫設定好的模型，`collect` 不會。這份文件只說明設定，不會修改你的真實 `.env` 或觸發任何模型請求。


## Session 清理（全部選填）

| 環境變數 | 預設／範圍 | 使用位置與建議 |
|---|---|---|
| `SESSION_IDLE_TTL_SECONDS` | `3600`，正整數 | API：新建 ephemeral session 的 TTL；可用 request 的 idle_ttl_seconds 覆蓋，managed 不適用 |
| `SESSION_CLEANUP_INTERVAL_SECONDS` | `60`，非負整數 | API／worker：背景刪除／暫停 sweep 間隔；0 關閉背景 sweep |
| `PI_IDLE_DISCONNECT_SECONDS` | `300`，非負整數 | worker：暫停 idle Pi 程序，保留資料；0 關閉暫停 |

省略／留空 `.env` 使用 Compose 預設；直接注入 Pod env 時請提供有效整數，不要留空。Chart 由 `lifecycle.ephemeralIdleTtlSeconds`、`cleanupIntervalSeconds`、`piIdleDisconnectSeconds` 設定，JSON schema 會拒絕負值及 0 TTL。通常採用 3600／60／300，再依工作間隔與記憶體觀察調整；需要常駐背景子程序則關閉 Pi 暫停。舊 session 保持 managed，不會自動變成 ephemeral。詳見 [生命週期操作](session-lifecycle.md)。

Helm 的非環境設定、storage、image、replicas、NetworkPolicy 見 [values.yaml](../charts/pi-sandbox/values.yaml)；內部端點範例見 [internal.yaml](../charts/pi-sandbox/examples/internal.yaml)。`existingSecret` 預設 pi-sandbox-secrets，但 Secret 本身必須預先建立，四個 key 皆須存在；Chart 不提供可用的預設認證。
