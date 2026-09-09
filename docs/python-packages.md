# 離線 sandbox：Python 預裝套件與內部模型端點

此模式適用於 K8s Pod 無法連到外部網路、但可連線到公司內部模型 gateway 的環境。套件在 image build 時安裝，pi 執行期間使用 image 既有套件，不下載依賴。

## 1. 指定 image 預裝的 Python 套件

編輯 [`sandbox/python-requirements.txt`](../sandbox/python-requirements.txt)。預設包含：

| 套件 | 用途 |
|---|---|
| requests 2.32.5 | HTTP client |
| minio 7.2.20 | MinIO Python SDK，`from minio import Minio` |
| numpy 2.2.6 | 計算範例 |
| rich 14.0.0 | 輸出格式範例 |
| sqlite3 | Python 3.12 標準函式庫；不用也不應 pip install |

套件列表可以換成公司實際需要的項目。requests／MinIO SDK 的預裝不會授予網路權限，也不會自動提供 MinIO 憑證；是否可存取內部服務取決於部署的網路政策與你交給 session 的設定。

```bash
# 預設由 uv 在建置時安裝
docker compose build sandbox-1 sandbox-2

# 也可以選用 pip；兩種方式讀取同一份 requirements
docker compose build --build-arg PYTHON_PACKAGE_INSTALLER=pip sandbox-1 sandbox-2

# 更新本機服務
docker compose up -d --wait
```

建置環境需要能取得 base images、npm、PyPI 或公司套件鏡像。新增套件／版本後必須重建並部署新 image。需要 C/C++ 或 OS library 的套件需在 Dockerfile 補上相依套件。不要將 registry token、模型 key 或 MinIO key 寫進 Dockerfile 的 ARG／ENV 或 requirements URL；CI 的認證請使用 BuildKit secrets／公司建置憑證機制。

目前固定直接依賴版本；傳遞依賴的實際版本保存在 image manifest。若公司要求完全可重現建置，應另提交包含所有傳遞依賴與 hashes 的 lock requirements，並使用 image digest 部署。

## 2. venv 與隔離如何運作

Image 的應用套件安裝在 `/usr/local/lib/python3.12/site-packages`；每個 session 使用 `--system-site-packages` 建立自己的 `/workspace/venv`。共享的是 **唯讀 image 套件**；每個 session 的 workspace、HOME、venv 本地內容、cache、程序仍獨立。

`/usr` 與 `/opt/python-runtime` 以 Bubblewrap 唯讀掛載，session 無法覆寫共享套件。Supervisor 依賴另裝在 `/opt/server`，不會暴露給 session，也不會混進 pi 的應用套件清單。

Runtime 設定：

- `PIP_NO_INDEX=1`：pip 預設不查詢 package index。
- `UV_OFFLINE=true`、`UV_PYTHON_DOWNLOADS=never`：uv 預設不連網或下載 Python。
- `PIP_REQUIRE_VIRTUALENV=true`：pip 必須在 venv 內使用。
- pi system instructions 明確要求只使用預裝依賴；缺少套件時回報名稱，要求修改 requirements 並重建，不重試安裝。

這些環境變數和提示不是網路安全邊界；任意程式仍可能覆寫變數、用 URL 或其他工具嘗試連線。正式 K8s 環境必須由公司的 CNI／NetworkPolicy 限制外部 egress，並允許必要的內部 DNS、模型 gateway 和業務服務。pip／uv binaries 仍保留供診斷、建置／隔離測試使用；本機 wheel 安裝能力不代表離線部署中建議動態安裝。

既有 3.12 session 重連時會更新 `pyvenv.cfg` 以看見共享套件，不重裝或刪除本地套件；本地同名版本可能遮蔽 image 版本，清單會顯示有效版本。為了部署結果一致，建議換 image 時建立新 sessions。既有 3.11 venv 不會自動遷移：先匯出資料、建立 3.12 session，再還原工作檔。

## 3. pi 如何知道哪些套件可以用

Build 時執行 `sandbox/python_inventory.py`，用實際 Python metadata 產生：

```text
/opt/python-runtime/requirements.txt  # 建置輸入
/opt/python-runtime/packages.json     # 實際版本、傳遞依賴、import 名稱提示、sqlite3 版本
/opt/python-runtime/SYSTEM.md         # 預裝套件摘要與禁止 runtime 安裝的工作指示
```

每次 pi 程序啟動時，把 `SYSTEM.md` 的文字傳入 `--append-system-prompt`；因此模型在第一次收到 prompt 前就知道套件與政策，不依賴它主動探索檔案。完整 JSON 位置也包含在提示中。

另用 session 的 Python 產生 `/workspace/state/python-packages.json`，反映本次啟動時真正可見的版本，包括舊 session 的本地覆蓋。pip distribution 名稱不一定等於 import 名稱，JSON 的 `imports` 是 metadata 提示，不代表每個 optional extra 都已安裝。執行中若有人手動改變套件，需重連或重新產生清單：

```bash
# 以下指令由 pi 的 bash 工具在 session 中執行
python /opt/python-runtime/inventory.py
python -c 'import requests, minio, sqlite3; print(requests.__version__, minio.__version__, sqlite3.sqlite_version)'
```

[`verify_sessions.py collect`](session-verification.md) 會自動匯出 `state/python-packages.json`，供追蹤執行環境。完整套件摘要會增加少量模型輸入 token；大量套件時可依實際需求精簡 image。

## 4. 設定公司內部 OpenAI 相容 gateway

每個 `.env.example` 欄位的選填／必填、預設值、容量與 timeout 建議，集中列在 [環境變數設定參考](configuration.md)；此節著重端點與離線部署方式。

外層 Agents SDK 和 pi **各自發出模型 HTTP 請求**，必須分別設定。以下 `.env` 是範例，替換成公司真實的 URL／model，金鑰使用你們的 Secret 管理方式注入：

```dotenv
# 外層 Agents SDK
OPENAI_BASE_URL=http://coordinator-gateway.models.svc.cluster.local:8000/v1
OPENAI_API_KEY=replace-with-outer-gateway-key
OPENAI_MODEL=company-coordinator-model
OPENAI_API_MODE=chat_completions

# Sandbox 裡的 pi；可以使用不同的端點、key 與 model
PI_BASE_URL=http://coding-gateway.models.svc.cluster.local:9000/v1
PI_API_KEY=replace-with-pi-gateway-key
PI_MODEL=company-coding-model
PI_API_MODE=chat_completions
# 必須換成模型／gateway 真實上限；128000 只是 sample 預設
PI_CONTEXT_WINDOW=128000
PI_MAX_TOKENS=4096
```

URL 填 API prefix，例如 `/v1`，不要包含 `/chat/completions` 或 `/responses`。model 必須是 gateway 實際接受的 model ID，不必存在於 pi 內建模型目錄。

| 設定 | 行為 |
|---|---|
| `*_API_MODE=chat_completions` | `/chat/completions`；適合多數相容 gateway |
| `*_API_MODE=responses` | `/responses`；只有 gateway 支援時使用 |
| 有 base URL，API mode 留空 | 預設 Chat Completions |
| base URL 與 API mode 都留空 | 保留原本 OpenAI Responses／pi 內建 OpenAI provider |
| PI_API_KEY 留空 | 沿用 OPENAI_API_KEY；設定 PI_API_KEY 時只把 pi key 送入 session |

不會從內部端點失敗後自動切換到公網 OpenAI。base URL 和 mode 在啟動／呼叫時驗證；無效設定報錯。金鑰不放在 pi argv 或清單內；pi 的 `models.json` 使用 `$PI_API_KEY` 環境變數參照。

pi 啟動時會建立／更新保留名稱 `sandbox-openai` 的 provider，保留其他 providers；不要手動在這個名稱下維護設定。重新載入程序時更新 endpoint／model 設定；改 `.env` 後應重新建立容器以套用環境值，不需重新 build image：

```bash
docker compose up -d --wait
```

相容 gateway 必須支援 tool/function calling。pi 使用 streaming，因此 gateway 還需支援 SSE tool-call chunks。工具 schema 中的 enum、required 與 SDK 的 JSON schema 參數也必須可接受。不同服務的「OpenAI 相容」程度不同，不能只靠能返回純文字判斷。

pi 的 Chat Completions 預設使用 `system` role、`max_tokens`，不傳 store／reasoning_effort。需要調整時，可設定原生 pi compatibility 選項，例如：

```dotenv
PI_MODEL_COMPAT={"supportsUsageInStreaming":false}
```

這個設定適用於不接受 `stream_options.include_usage` 的 gateway，但可能無法取得 pi 的 token 用量。不能把缺失或回傳的零 usage 視為實際免費；本機驗證帳本不是 gateway 帳單。

K8s 中請把上述設定分別放進 API 與 worker Deployment 的 env；API keys 用 `secretKeyRef`，image 在有網路的 CI 建置後推到內部 registry。這個專案目前提供 Compose 範例，未提供完整 K8s manifests。內部 HTTPS 若使用公司 CA，需將 CA 加入 image 信任鏈，並依 Python／Node.js client 設定正確的 CA 檔；不要關閉 TLS 驗證。

## 5. 無外網實際驗證

先建置 image，再使用 overlay，將測試 worker 設定為 `network_mode: none`（只有 loopback）。overlay 僅供測試；正式服務仍需內部網路。

```bash
docker compose build sandbox-1
docker compose -f compose.yaml -f compose.offline-test.yaml run --rm --no-deps \
  -e RUN_ISOLATION_TESTS=1 sandbox-1 /opt/server/bin/python -m pytest \
  -p no:cacheprovider tests/test_preinstalled.py tests/test_model_config.py tests/test_gateway.py -q
```

`test_preinstalled.py` 在兩個真正的 pi sessions 中直接 import 預裝套件，以 numpy 計算 55、sqlite3 執行 SQL，以 requests 準備 HTTP request、建立 MinIO client（不連 MinIO）。檢查 image 套件與 manifest 唯讀、套件提示內容，以及舊 3.12 venv 重連後可見套件。

`test_gateway.py` 在測試容器 loopback 建立兩個不同 port／URL prefix 的模擬 gateways：真正的 Agents SDK 使用自訂 model、key、base URL 發送委派，真正 pi 接收 SSE tool calls、寫 Python、執行並回傳 42。測試檢查外層與 pi 各自的獨立 URL、Authorization、model、預裝提示與工具結果，確保請求沒有送到另一層的 gateway。這是協定與整合測試，**沒有真實模型推論、沒有付費 token**，也不代表已連到你們公司的 endpoint。

待部署到公司可存取 gateway 的環境後，可自行執行：

```bash
python3 scripts/verify_sessions.py verify --output artifacts/internal-gateway-verification
```

這才會呼叫設定好的模型。gateway 不支援的參數應依錯誤與服務文件調整；切勿為了通過測試而切回外部模型。

本次驗證：24 項容器測試（包含無外網套件測試與本機 gateway 整合）及 3 項主機工具回歸測試通過；未呼叫付費模型。公司 gateway 的實際連線仍需在你們的網路環境驗證。

參考：[Python 3.12 venv](https://docs.python.org/3.12/library/venv.html)、[uv Docker](https://docs.astral.sh/uv/guides/integration/docker/)、[OpenAI Agents](https://developers.openai.com/api/docs/guides/agents)。Pi 配置依 image 內 `@earendil-works/pi-coding-agent/docs/models.md` 的 0.85.1 文件實作。
