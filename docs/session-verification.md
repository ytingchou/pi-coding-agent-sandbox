# Session 驗證、執行證據與 tracing 操作手冊

這份文件對應本專案的 Docker Compose、Agents SDK orchestrator 與 pi RPC worker。目的是讓你自行驗證「模型真的呼叫工具寫檔、執行 Python，並將實際結果回傳給外層 Agent」，保存可檢查的證據，並分別計算兩層模型的用量。

提供工具：[`scripts/verify_sessions.py`](../scripts/verify_sessions.py)。主機使用 **Python 3.12、uv、Docker 與 Docker Compose**，先執行 `uv sync --locked`。工具本身只用 Python 標準函式庫；透過 uv 使用專案指定的 Python。工具在主機執行，再透過 `docker compose exec` 存取容器；不要在 API container 裡執行這個工具。它不需要把 Docker socket 掛進 sandbox。

## 1. 一次完成真實 API 驗證

在 repository 根目錄執行：

```bash
# 尚未建立 .env 時才複製；已有金鑰就保留原檔
cp -n .env.example .env
# 用編輯器填入 OPENAI_API_KEY；不要將真實 key 貼進指令或文件
chmod 600 .env
docker compose up --build -d --wait

uv run --locked python scripts/verify_sessions.py verify --output artifacts/my-verification
```

`--output` 必須是尚未存在的目錄，避免覆蓋之前的證據。不指定時使用 `artifacts/verification-日期-時間`。工具固定從 repository 根目錄執行 Compose，即使你從其他工作目錄呼叫也能找到服務；相對 output 路徑則以你的目前工作目錄解析。

API container 使用已載入的 `API_TOKEN`，模型使用容器中的 `OPENAI_API_KEY`。修改 `.env` 後要重新執行 `docker compose up -d --wait`，讓容器載入設定。不要使用 `docker compose config` 或列印全部環境變數來確認金鑰。

這個指令會使用真實 API、產生費用。預設沿用 Compose 的 `OPENAI_MODEL` 與 `PI_MODEL`。模型產生的程式和呼叫次數可能不同，每次 token 總量也會不同；沒有硬性 token 預算或自動重試。

### 驗證情境與通過條件

工具建立三個全新 Agents、各一個全新 session，避免既有對話和用量混入：

| 情境 | Worker | 實際驗證 |
|---|---|---|
| Python 委派 | sandbox-1 | 外層 Agent 委派給 pi；pi `write` 建立 `verification.py`、`bash` 執行平方和，stdout 為 55，外層收到 55 |
| Standalone skill | sandbox-1 | 直接送 `/skill:python-stats 3,6,9` 給 pi；transcript 有原生 skill 展開、成功 write/bash，以及 `analysis.json` 的平均值 6 |
| Package 與 extension | sandbox-2 | 原生安裝 stats-kit；外層 Agent 委派 package skill；pi 實際呼叫 `python_stats` 與 `session_info`，產生平均值 6，Python 路徑是 session venv，外層收到結果 |

另外查詢兩個未安裝 package 的 sessions，確認 package command 沒有出現。這項檢查證明此次安裝沒有影響其他 worker 上的 sessions；完整同 worker 的檔案、程序和 Python 環境隔離仍由既有 isolation tests 驗證。

Skill 情境刻意直接呼叫 `/pi/prompt`，確保使用 pi 原生 skill command 展開。Python 與 package 情境則走完整 `/run` → Agents SDK tool → Session Manager → pi → Agent 路徑。

通過時 `report.json` 的 `status` 為 `passed`，並有 `checks`。只有模型回答「已執行」不算通過：工具還會檢查 transcript 的工具呼叫、成功 toolResult、實際檔案與外層結果。這是對已知範例的功能驗證，不是任意程式正確性證明。

工具預設保留 sessions，供你繼續查看。讀完證據後執行：

```bash
uv run --locked python scripts/verify_sessions.py cleanup --report artifacts/my-verification/report.json
```

`cleanup` 只刪除報告列出的 sessions，且要求已執行 collection；本機匯出檔保留。API 目前沒有刪除邏輯 Agent 的 endpoint，所以 Agent registry 留在 MongoDB，外層歷史仍留在 API volume。不要以 `docker compose down -v` 代替這個步驟，它會刪除整個 sample 的資料。

## 2. 匯出檔案與證據來源

```text
artifacts/my-verification/
  report.json
  sessions/<session-id>/
    state/session.jsonl
    state/python-packages.json # 啟動時的有效 Python 套件與 sqlite3 版本
    trace.json
    outer-history.json
    extension-audit.jsonl       # 有工具執行時才會存在
    verification.py            # Python 情境
    analysis.py                # skill 情境
    analysis.json
    package-stats.json         # package 情境
```

| 證據 | 用途與範圍 |
|---|---|
| report.json → requests | 每次 HTTP 請求的時間、method、path、body、status、response；先保存 intent，再呼叫 API；收到回應後先落盤才做 assertion |
| requests → response.sandbox_results | 外層實際收到的 pi 結果；不是外層模型自行描述的紀錄 |
| 原生 session.jsonl | pi 持久化的完整 session entries，包含技能展開後的 user prompt、assistant tool calls、toolResult、assistant usage、最後回答，以及其他原生 entry |
| trace.json | 從原生 entries 抽出所有 message，保留 entry ID、parent ID、timestamp；方便逐步閱讀 |
| outer-history.json | 以 agent_id 查詢 Agents SDK SQLiteSession 的對話歷史，包含 function call／output 和最終訊息；另記錄目前模型設定 |
| extension-audit.jsonl | bundled extension 的 tool_result hook：工具名稱、isError、時間；可交叉核對工具執行 |
| Python／JSON artifacts | sandbox 真正留下的檔案內容；搭配 bash stdout 或 python_stats toolResult 確認結果 |

Worker 內 `/sessions/<session-id>/data` 對應 pi 看見的 `/workspace`。例如：

```text
pi:     /workspace/analysis.py
worker: /sessions/<session-id>/data/analysis.py
```

每個 session 的 HOME、venv、workspace 分離。相同 `/workspace/analysis.py` 出現在兩個 sessions 不代表共用檔案。

`report.json` 內也包含證據內容，方便作單檔檢查。輸出預設放進 Git 忽略的 `artifacts/`，新驗證目錄權限為 0700、匯出檔為 0600。不要自行 commit 實際使用者的 prompts、程式或工具輸出；這些紀錄可能包含敏感內容。工具不匯出 HOME/auth 設定或 API key 環境值，但不會替任意模型輸出做全面秘密遮罩。

## 3. 查看完整 session tracing

本工具提供的是 **應用層持久化 session tracing**。目前沒有保存 pi 每個 RPC 串流 delta、完整程序 syscall trace 或每個子程序的原始 stdout/stderr。原生 transcript 中的 toolResult 也可能已被 pi 工具自身截斷；匯出完整 transcript 不等於還原未被工具保存的輸出。重要大量輸出應由程式寫到 workspace 檔案，再用 `--artifact` 收集。

依照以下關係追蹤單次執行：

```text
HTTP requests[i]（開始／結束時間、agent_id、允許的 session_ids）
  → outer-history: run_python_in_sandbox 的 function_call arguments
  → report: sandbox_results[].session_id
  → session.jsonl: user prompt（含實際展開的 skill 內容）
  → assistant.content[].type == toolCall（name、arguments、id）
  → toolResult.toolCallId 對應 toolCall.id（content、isError）
  → assistant 最終回答
  → sandbox_results.output / tool_results
  → outer-history function_call_output → 外層最終回答
```

外層的 `call_id` 與 pi 的 `toolCallId` 屬於不同層，不能直接視為同一個 trace ID。本範例沒有跨服務的統一 trace ID；以 agent_id、session_id、HTTP 時間和 prompt 串接。工具使用全新 session 並依序呼叫，因此本次驗證的對應關係明確。pi 的 entry `id`／`parentId` 可以用來追蹤原生歷史的父子關係。

快速查看所有工具呼叫、結果與 usage（在主機執行）：

```bash
uv run --locked python - artifacts/my-verification/report.json <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
for e in r['evidence']:
    print('\nSESSION', e['id'], e['sandbox_id'], e['usage'])
    for row in e['trace']:
        m = row['message']
        for part in m.get('content', []):
            if isinstance(part, dict) and part.get('type') == 'toolCall':
                print(row['timestamp'], 'CALL', part['id'], part['name'], part['arguments'])
        if m.get('role') == 'toolResult':
            print(row['timestamp'], 'RESULT', m.get('toolCallId'),
                  'error=', m.get('isError'), m.get('content'))
print(json.dumps(r['token_summary'], indent=2))
PY
```

要在驗證執行中查看，另開終端，從正在寫入的 report 取得 session ID，將下列範例的 UUID 換成實際值：

```bash
# worker 必須與 report 的 sandbox_id 一致；Ctrl-C 只停止 tail
# transcript 通常在第一次模型回合持久化後才出現
docker compose exec -T sandbox-1 tail -n 100 -F \
  /sessions/REPLACE_WITH_SESSION_UUID/data/state/session.jsonl

# API／worker 的健康、HTTP 和程序錯誤；不是模型 usage 的來源
docker compose logs --since 10m --timestamps api sandbox-1 sandbox-2
```

若要永久保存 container logs，可加上 `> artifacts/container-logs.txt`。worker stderr 目前只在記憶體保留最後 20 chunks，主要用於啟動錯誤回報；並沒有完整落盤，不能將 compose logs 視為完整 pi stderr tracing。

Compose 的 `OPENAI_AGENTS_DISABLE_TRACING=1` 表示目前未開啟 SDK 的雲端 tracing export。本文件的本機證據收集不依賴雲端 tracing。原生 extension command 可能完全不呼叫模型，也可能還沒有產生 transcript；不能把這種 command 回應當成模型執行成功。

## 4. Token 如何計算

兩層用量來源不同，不能只看 `/run` 回傳的 `usage`：

```text
外層 = 每次 /run 回應的 usage.total_tokens 加總
pi   = 每個 session.jsonl 中，所有 assistant message 的 usage.totalTokens 加總
總量 = 外層 + pi

pi 的輸入 = usage.input + usage.cacheRead + usage.cacheWrite
pi 的輸出 = usage.output
```

這個版本的 pi `input` 不含 cacheRead／cacheWrite；但 `totalTokens` 已包含快取，所以不能再把 cacheRead 加到 totalTokens。外層 SDK `input_tokens` 已經是輸入總數，也不要另外加快取。內部 gateway 若不回傳 usage，或用零填補缺失欄位，本地資料無法證明精確消耗，仍需對照 gateway 的用量紀錄。

工具直接加總原生 assistant usage，**不**從 `agent_end` 再加一次、不從 extension audit 計數，也不把 outer-history 複製的 pi 內容再當成 pi 呼叫用量。外層模型讀取工具結果所產生的輸入 token 本來就是另一筆實際用量，應保留在外層統計。

`token_summary` 的欄位：

- `agents_sdk`：requests、input_tokens、output_tokens、total_tokens。
- `pi`：input、output、cacheRead、cacheWrite、totalTokens。
- `recorded_total_tokens`：已取得 usage 的總和。
- `complete_for_this_run`：這份工具帳本是否具備本次 fresh-session run 的完整紀錄。
- `limitations`：缺 transcript、缺 usage、錯誤／中斷，或既有 session 的範圍限制。

HTTP 502 的 `detail.usage` 若存在也會納入。若請求斷線、process 被殺、供應商未回 usage，不能從本地檔案推算精確 token；工具保留已知用量，標示不完整，不猜測為零。這是 provider／SDK 回傳用量的統計，不是帳戶帳單查詢；token 數不等同美元費用。

### 先前實際驗證結果

2026-09-09 的三個 fresh sessions，全部通過、沒有遺失的模型 usage：

| 層級 | 輸入（含快取） | 輸出 | 總計 |
|---|---:|---:|---:|
| Agents SDK | 2,107 | 347 | 2,454 |
| Pi | 12,463 | 608 | 13,071 |
| 合計 | 14,570 | 955 | **15,525** |

Pi 快取輸入為 7,168。原始證據見 [`reports/sandbox-capabilities-verification.json`](../reports/sandbox-capabilities-verification.json)。新工具重現相同三條情境，但每次實際消耗以新報告為準。

## 5. 收集自己的既有 sessions

先停止向這些 sessions 發新 prompt，等目前 run 結束，再收集。匯出採跨容器逐一讀取，不是分散式一致性快照；執行中讀取也可能遇到尚未寫完的 JSONL。

對工具建立、尚未刪除的 sessions：

```bash
uv run --locked python scripts/verify_sessions.py collect \
  --report artifacts/my-verification/report.json \
  --artifact my-result.json
```

`--artifact` 可重複指定，限 workspace 下的 UTF-8 文字檔；不能用絕對路徑、`..` 或直接要求 home/state/venv。不要用這個文字匯出功能搬運大型資料集或二進位檔。

若 sessions 是你自行建立的，先寫一個 manifest：

```json
{
  "fresh_sessions": false,
  "sessions": [
    {
      "agent_id": "替換成真實-agent-UUID",
      "id": "替換成真實-session-UUID",
      "sandbox_id": "sandbox-1"
    }
  ],
  "requests": []
}
```

存為 `artifacts/manual/report.json`，執行相同 `collect` 指令即可。`sandbox_id` 必須對應本機 Compose service；遠端 HTTP worker 不適用本工具的 Docker 匯出方式。新增 worker 可用 `verify --worker1 sandbox-1 --worker2 sandbox-3`，前提是 Compose 和 API endpoints 已設定好。

既有 session 的 pi 統計是 **整個 transcript 的累積用量**。沒有保存過 `/run` response 的外層用量無法從 SQLiteSession 對話歷史還原，所以 manifest 的 `requests: []` 不代表外層真的消耗零；工具會標示範圍不完整。

要精確衡量某一輪，優先使用全新 sessions 與全新 Agent。若必須延續既有 session，應在呼叫前後各保存一份 transcript，依 entry ID 找出新 assistant entries 計算差額，同時保存該次 `/run` 的 usage；不要把每次完整 collect 結果相加，否則重複計數。現有 `collect` 會更新同一份 snapshot，沒有自動計算差額的功能。

## 6. 失敗處理與工具驗證

- API quota、模型錯誤或 assertion 失敗：工具保留 requests、嘗試匯出當前證據，exit code 為 1，不自動重試模型、不刪除 sessions。
- HTTP 請求可能已被接受但連線中斷：先檢查 session trace／服務狀態，再決定是否重送，避免重複執行有副作用的程式。
- Session 配置失敗：response 若帶 session_id，可用 API `/connect` 或 DELETE；未成功返回的 session 不會自動加入工具清理清單，須依保存的錯誤回應處理。
- 無 transcript：可能尚未有模型回合、尚未落盤，或 session 已刪除。collect 保存 missing_files 並將用量標為不完整；刪除後無法復原原始檔案。
- Compose 呼叫失敗：確認 `docker compose ps`、Docker context 和 service 名稱。工具不將 container 全部環境值或 stderr 自動印出。
- 收集失敗後，可在 run 已停止且服務恢復時重做 `collect`；這不會呼叫模型、不增加模型 token。

工具本身的離線回歸測試：

```bash
uv run --locked python -m unittest discover -s tests -p test_verification_tool.py -v
```

測試涵蓋快取 token 不重複計算、錯誤回應 usage、缺失／既有 session 的統計限制，並使用先前真實執行報告檢查「移除 bash 執行證據後必須失敗」。Docker 匯出路徑也以不呼叫模型的原生 extension command 實測；真正模型行為請使用本文件的 `verify` 指令。
