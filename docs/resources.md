# Skills、extensions 與 pi packages

Sandbox 使用 pi 的原生資源系統。每個 session 的 `HOME`、pi settings、已安裝 packages、npm cache 都在自己的 `/workspace`；相同 worker 裡的 session 不共用可寫資源。

## 一次跑完 demo

```bash
docker compose up --build -d --wait

# 不需要 API key：查詢 skill、執行 extension、安裝／執行／移除 pi package
docker compose exec api python scripts/demo_resources.py

# .env 填好 OPENAI_API_KEY 後：額外執行真實 skill 及 Agents SDK → pi 工具流程
docker compose exec api python scripts/demo_resources.py --live
```

預設 demo 會清理自己的 sessions。加 `--keep` 可保留它們及產生的檔案；程式一開始會印出 `agent_id` 與各 `session_id`。Package 在 demo 結束前會被移除，`--keep` 保留的是 session 及 package 原始檔，可以再安裝。

Demo 建立三個 sessions：兩個在 `sandbox-1`，一個在 `sandbox-2`。只在第一個 session 安裝 package，並檢查另外兩個沒有載入它。

| 能力 | 範例 | 離線 demo | `--live` 額外示範 |
|---|---|---|---|
| Skill | `python-stats/SKILL.md` | 確認原生 skill command 被發現 | `/skill:python-stats` 展開指令，模型寫入並執行 `analysis.py` |
| Extension | `session-tools.ts` | `/session-info` 回傳 UID、HOME、venv、已載入工具 | 模型可呼叫 `session_info`；`tool_result` hook 寫 audit log |
| Pi package | `stats-kit/package.json` | 真正 `pi install`、`/package-stats` 執行 Python、reload 與 `pi remove` | 外層 Agents SDK 委派 pi 使用 package skill 與 `python_stats` 工具 |

離線模式會執行真正的 extension handler 與 Python helper，但不會呼叫模型，也不宣稱已驗證模型遵循 skill 的行為。

## 資源位置與生命週期

| 映像內唯讀 seed | 每個 session 的可寫副本 |
|---|---|
| `/opt/pi-resources/skills/` | `/workspace/home/.pi/agent/skills/` |
| `/opt/pi-resources/extensions/` | `/workspace/home/.pi/agent/extensions/` |
| `/opt/pi-resources/packages/` | `/workspace/packages/` |

這些 seed 的原始檔在 repository 的 `sandbox/resources/`。啟動時先降成 session UID、進入 Bubblewrap，才執行 `bootstrap.py` 複製檔案。Supervisor root 不執行 extension、package hook 或 bootstrap 的 session 內容。

初始化只做一次，以 session HOME 裡的 `.sample-resources-v1` 標記記錄。重連與 reload 保留使用者對副本的修改，也不會把已移除的 package 自動裝回來。既有舊版 session 在新版 worker 第一次啟動其 pi 程序時也會補上 seed；這不會覆寫已存在的同名檔案或設定。

`stats-kit` 預設只複製原始碼，需透過 API 或 `pi install` 安裝才會載入。原生安裝把來源記錄在 `/workspace/home/.pi/agent/settings.json` 的 `packages`；npm/git 的下載目錄也留在同一個 session 的 pi user directory。npm cache 與 prefix 都指向 session HOME。

`resources/reload` 會停止該 session 的 pi 程序，再用原本的 transcript 啟動；因此 HOME、venv、檔案與 pi 對話保留，extension 的記憶體狀態則重新初始化，暫存目錄重建。Package 安裝／移除成功後自動做同樣的 reload。

## API

下列 endpoints 都沿用 `Authorization: Bearer <API_TOKEN>`，先檢查 session 所屬 Agent，並與該 Agent 的其他操作序列化。Worker 也以 session lock 保護安裝、reload 與 prompt，避免同時修改設定。

| 方法 | Endpoint | Body |
|---|---|---|
| GET | `/agents/{agent_id}/sessions/{session_id}/resources` | 無 |
| POST | `/agents/{agent_id}/sessions/{session_id}/resources/reload` | 無 |
| POST | `/agents/{agent_id}/sessions/{session_id}/packages` | `{"action":"install","source":"/workspace/packages/stats-kit"}` |
| POST | 同上 | `{"action":"remove","source":"/workspace/packages/stats-kit"}` |
| POST | `/agents/{agent_id}/sessions/{session_id}/pi/prompt` | `{"prompt":"/session-info"}` |

`GET resources` 回傳 pi `get_commands` 的原生清單，以及 `pi list` 的文字輸出。清單的 `source` 可以是 `skill`、`extension` 或 `prompt`；工具清單可透過 demo extension 的 `/session-info` 查詢。

`/pi/prompt` 是直接呼叫這個 session 的 pi，不經外層 Agents SDK，也不寫入外層 Agent 對話；pi 自己的對話仍會保留。原本的 `/agents/{agent_id}/run` 流程不變，外層 Agent 的工具同樣可以委派 `/skill:...` 或 extension 指令給 pi。

例如，取得 session IDs 後：

```bash
export DEMO_TOKEN=local-demo-change-me  # 與 .env 的 API_TOKEN 一致
export SESSION_URL=http://localhost:8000/agents/你的agent-id/sessions/你的session-id

curl -fsS "$SESSION_URL/resources" -H "Authorization: Bearer $DEMO_TOKEN"

curl -fsS -X POST "$SESSION_URL/packages" \
  -H "Authorization: Bearer $DEMO_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"install","source":"/workspace/packages/stats-kit"}'

curl -fsS -X POST "$SESSION_URL/pi/prompt" \
  -H "Authorization: Bearer $DEMO_TOKEN" -H 'Content-Type: application/json' \
  -d '{"prompt":"/package-stats 2,4,6,8"}'
```

最後一個請求不需模型，`output` 是 JSON 字串，包含 `mean: 5`、`count: 4` 與 `/workspace/venv/bin/python`，並將結果寫到該 session 的 `/workspace/package-stats.json`。

## 加入自己的 skill 或 extension

新增到 `sandbox/resources/skills/<name>/SKILL.md` 或 `sandbox/resources/extensions/<name>.ts`，重新建置並建立新 session 即可。對已初始化的 session，請透過 pi 在自己的 HOME 寫入／修改檔案，再呼叫 `resources/reload`；重新建置不會覆寫既有 session 副本。

Skill 使用標準 `name`、`description` frontmatter。Pi 載入描述供模型選用；使用 `/skill:<name> 任務內容` 可明確要求原生 skill 展開。範例 `python-stats` 要求 pi 寫程式、執行、儲存 JSON 並回報實際輸出。

Extension 範例包含三種行為：

- `registerTool("session_info")` 提供模型可呼叫的工具。
- `registerCommand("session-info")` 提供不需模型的指令，用 `pi.sendMessage(..., {triggerTurn:false})` 回傳結果。
- `session_start` 寫入 `extension-loaded.json`；模型工具的 `tool_result` 事件寫入 `extension-audit.jsonl`，紀錄工具名、是否錯誤與時間，不記錄完整工具輸入。

RPC 現在會區分原生 extension command 與需要模型的 skill／一般 prompt。Extension command 若已完成且沒有啟動模型，回傳 `kind: "extension_command"`，不再永遠等待 `agent_end`；一般模型流程仍等待 `agent_end`。這支 sample client 不實作互動對話框：extension 請採用 headless handler，使用 `pi.sendMessage` 或 `ctx.ui.notify` 回傳資訊，避免直接向 stdout 印出非 JSONL 內容。需要 UI 選擇／輸入的 extension 可能逾時。

## 加入自己的 pi package

`stats-kit` 是完整的本機 package：

```text
stats-kit/
├── package.json            # pi.extensions / pi.skills / pi.prompts manifest
├── extensions/stats.ts     # python_stats tool 與 package-stats command
├── skills/package-stats/SKILL.md
├── prompts/package-report.md
└── scripts/stats.py        # 在 session venv 執行並寫結果檔
```

可以把其他本機 package 放到 `sandbox/resources/packages/`，重建後建立新 session，再用其 `/workspace/packages/...` 路徑安裝。原生本機安裝引用該目錄，不會另外複製；不要在仍需使用 package 時刪除其原始碼。

API 也接受 pi 原生 `npm:`、`git:`、HTTPS/HTTP 來源，例如 `npm:@your-org/your-package@1.2.3`、`git:github.com/your-org/your-repo@v1.0.0`。這些是來源格式示意，請替換成自己的 package。遠端安裝需要網路；本範例不提供私有 registry token、SSH agent forwarding 或互動登入。相對本機路徑請改成 `/workspace/` 開頭的絕對路徑。

只有 `install`、`remove` 兩種管理 action；來源是單獨 argv 參數，不會插入 shell 字串。Package 操作有 120 秒上限，失敗或逾時不自動重試，也不承諾 transaction rollback；可先查詢、修正或移除，再 reload。移除 package 會取消載入，保留產生的結果檔與本機原始碼。

Extensions、package 安裝 hooks 與 helper 都以該 session 的 UID、namespace 與環境執行，仍受原有檔案／程序隔離約束。它們可讀取同一 session 的 OPENAI_API_KEY，且網路仍共用；這不是第三方程式碼的額外權限層。隔離邊界見主 README。

## 測試

```bash
docker compose run --rm --no-deps -e RUN_ISOLATION_TESTS=1 sandbox-1 \
  /opt/server/bin/python -m pytest -p no:cacheprovider \
  tests/test_rpc.py tests/test_worker.py tests/test_isolation.py tests/test_resources.py -q
```

新測試以真正的 pi CLI 驗證 native discovery、extension 載入、自訂工具清單、無模型指令回傳、本機 package install/remove、Python helper 的執行、不同 session 的資源隔離，以及 reload／worker 重建後的持久化。遠端 npm/git 安裝需網路，未包含在離線測試中。

驗證結果：API／Agents SDK 測試 6 個通過，worker／RPC／隔離／資源測試 12 個通過；完整 Compose 的離線與 `--live` 流程均已執行。真實模型成功使用 `python-stats` skill、`session_info` extension tool 與 package 的 `python_stats` tool。整體 demo 的執行證據與用量限制詳見 [完整驗證報告](../reports/full-sandbox-demo.json)。

原生 API 格式參考：[Pi skills](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/skills.md)、[Pi extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md)、[Pi packages](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/packages.md)。實作與容器測試使用固定的 pi `0.85.1`。
