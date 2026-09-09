# Session 保留、自動清理與 Pi 程序暫停

此功能適用於長時間運作的 worker Pods。Pod 本身不因 session 結束而重建；一個 Pod 能同時保留多個獨立 session。

## 兩種保留策略

| retention | 預設 TTL | 閒置後行為 | 適用情境 |
|---|---|---|---|
| `managed`（建立時預設） | 無 | 保留檔案與 metadata，直到明確 DELETE | 需要持續工作、保留證據的 session |
| `ephemeral` | 3600 秒 | TTL 到期後停止程序，刪除 session 目錄、worker 記錄與 API binding | 一次性工作／可重新產生的資料 |

`managed` 和 `ephemeral` 都能暫停閒置 Pi 程序，預設 300 秒後停止程序、保留 workspace／venv／Pi 歷史。下次 prompt、connect 或資源操作會自動啟動新的 Pi 程序並載入既有 session。這會釋放程序記憶體，但**不會釋放 `MAX_SESSIONS` 名額或磁碟**；只有 DELETE／TTL 清理才會釋放名額與 session 檔案。

暫停會終止該 Pi namespace 中的背景子程序。需要持續運行背景工作的部署，可設 `PI_IDLE_DISCONNECT_SECONDS=0`；不應把這些 session 當成獨立服務管理平台。

舊 registry 自動補上欄位，既有 session 全部保持 `managed`，不會因升級開始刪除。TTL 存在每筆 binding 中，修改環境的預設 TTL 不會改變既有 session。切換保留策略會更新 TTL 並重新計算閒置時間。

## API 使用

以下操作適用於 Compose 或 Helm port-forward。先在目前 shell 設好 `API_TOKEN`（控制 API token，不是模型 key）、`AGENT_ID` 與 `SESSION_ID`；指令沒有預填任何秘密。`API_URL` 預設 `http://127.0.0.1:8000`。

```bash
export API_URL=http://127.0.0.1:8000
# 建立 Agent；記下回應的 agent_id
curl -fsS -H "Authorization: Bearer $API_TOKEN" -X POST "$API_URL/agents"

# 建立自動清理 session；省略 sandbox_id 由 manager 選擇 worker
curl -fsS -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"retention":"ephemeral","idle_ttl_seconds":3600}' \
  "$API_URL/agents/$AGENT_ID/sessions"

# 將特定 session 改成永久受管，供人工檢查／保存證據
curl -fsS -X PATCH -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"retention":"managed"}' \
  "$API_URL/agents/$AGENT_ID/sessions/$SESSION_ID/retention"

# 列出自己的 session：包含 retention、idle_ttl_seconds、last_activity（Unix 秒）
curl -fsS -H "Authorization: Bearer $API_TOKEN" "$API_URL/agents/$AGENT_ID/sessions"

# 全域管理操作：預覽當下可清理的 session（不刪除）
curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" "$API_URL/sessions/cleanup?dry_run=true"
# 實際清理所有符合條件的 session，包含明確 DELETE 失敗後待重試的項目
curl -fsS -X POST -H "Authorization: Bearer $API_TOKEN" "$API_URL/sessions/cleanup?dry_run=false"
# 最近一次 sweep 的結果；程序重啟後此摘要重設
curl -fsS -H "Authorization: Bearer $API_TOKEN" "$API_URL/sessions/cleanup"
```

全域 cleanup 共用目前的 `API_TOKEN`，不是每個 Agent 各自的管理權限；本範例尚未提供多租戶 RBAC。自動清理不會產生模型請求或 token 費用。`dry_run` 預覽不是預約，實際刪除時會再次檢查活動狀態。

## 清理規則與競態處理

- API 使用現有 Agent lock：`/run`、prompt、connect、package／resource 操作中不清理該 Agent 的 session。列出 session 不會延長 TTL。
- connect、prompt、resource/package 操作完成時更新活動時間，失敗／timeout 也更新，避免錯誤後立刻清理證據。
- Worker 使用自己的 session lock 與持久化活動時間再次判斷；忙碌或最近使用的 session 回傳 409，API 保留記錄並延後重試。Worker 的暫停 sweep 也跳過鎖定中的 session。
- 刪除先把 binding 標成 `deleting`，再停止 Pi／刪除目錄與 worker SQLite 記錄，成功後才移除 binding。Worker 不可達時保留待刪記錄，下一次 sweep 重試；不會把 session 移到別的 Pod，也不會自動重送 prompt。
- 背景 sweep 預設每 60 秒執行一次。實際刪除時間是 TTL 到期後的某次 sweep，還受正在執行的工作、worker 連線與檔案清理時間影響，不是精確 deadline。
- Worker 重啟後 Pi 程序原本就不在；metadata／檔案由 PVC 保留並於下次使用恢復。API 停止期間不會自動刪除 session；Worker 仍可自行暫停閒置程序。
- API registry 是刪除策略的來源；不會對掃描出的未知目錄擅自刪除。必須一起備份／恢復 API registry 與各 worker 的 state、sessions PVC；遺失 API registry 的孤兒資料需人工盤點。
- 刪除會移除該 session 的生成程式、Pi transcript、檔案和本地套件。API 的 Agent 記錄與 `conversations.sqlite` 對話歷史不會刪除，也不會自動匯出證據或回收 PVC 配額。需要保留時先用 [驗證工具](session-verification.md) collect。
- 刪除完成後重用已釋放 UID slot，避免 long-running Pod 的 UID 隨 session 總建立數無限增長；上限保持在 Pod user namespace 的 65536 UID 範圍內。不得繞過 worker 管理自行啟動同 UID 的宿主程序。

時間戳為壁鐘時間；叢集節點應維持 NTP 同步。此版仍使用單 API process／單 worker supervisor，沒有分散式 lease；不要增加 API replicas 或 Uvicorn workers。

## 設定與建議

| 環境變數 | Helm values | 預設 | 建議 |
|---|---|---|---|
| `SESSION_IDLE_TTL_SECONDS` | `lifecycle.ephemeralIdleTtlSeconds` | 3600 | 大於人工取回輸出與正常工作間隔；只套用新 ephemeral session |
| `SESSION_CLEANUP_INTERVAL_SECONDS` | `lifecycle.cleanupIntervalSeconds` | 60 | 通常 60；0 關閉 API 與 worker 背景 sweep，明確 DELETE／手動 cleanup 仍可使用 |
| `PI_IDLE_DISCONNECT_SECONDS` | `lifecycle.piIdleDisconnectSeconds` | 300 | 降低 idle 記憶體；0 關閉程序暫停 |
| `MAX_SESSIONS` | `sandbox.maxSessions` | 8 | 公司初始部署可從 2 開始，依 Python／模型工作量量測 |

TTL 是資料保留規則，不是執行 timeout；`PROMPT_TIMEOUT` 仍獨立控制單次 Pi prompt。沒有每個 session 的磁碟總量 quota、每 session cgroup 或 HPA；Pod resources 和 PVC 用量仍需監控。

## 自行驗證（無模型 token）

```bash
uv sync --locked
uv run --locked pytest tests/test_lifecycle.py -q
make test-container
```

主機測試涵蓋受管／ephemeral、dry-run、活動鎖、刪除失敗重試、權限、舊 DB 遷移、背景 sweep、UID 重用。容器測試另外啟動真正 Pi、寫 `result.py` 並執行得到 42、暫停程序、重建 worker 後再次執行同一檔案，最後確認程序與目錄刪除。這是 deterministic Pi RPC 驗證，不會把它稱為真實模型生成程式。
