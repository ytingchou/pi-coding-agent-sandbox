# 架構決策與演進方向

以下記錄目前採用的設計，不代表已實作所有後續選項。

| 決策 | 理由與代價 | 未來擴展前提 |
|---|---|---|
| SDK coordinator + Pi coding runtime | 外層負責委派與整合，Pi 負責寫碼／工具；有兩層模型 latency 與 token 成本 | 保留分層 usage 與 execution evidence |
| 單 API process | Agent lock、SDK SQLite history、cleanup 協調在同 process | 多副本前先做 distributed leases／fencing、shared history、conditional writes |
| 固定 session → worker | 工作目錄、UID 與 transcript 在該 worker storage | 搬移需明確 drain、資料轉移與路由切換協定 |
| MongoDB agents + bindings | 持久化路由及生命週期，Pydantic 明確定義 documents | 完整 schema evolution、分頁、batch cleanup 與負載測試 |
| 同步 PyMongo + to_thread | driver thread-safe pool，避免阻塞 event loop；取消需等正在執行的 DB operation | 量測 thread pool、DB latency 與壅塞後再選 async driver |
| Worker 確認刪除後才刪 binding | 保留 ownership／路由以重試；DB 與 HTTP 非原子 transaction | 大規模 reconciliation／audit 可再持久化 |
| Pi process 可暫停、session 可保留 | 長駐 Pod 減少 idle memory；恢復需重新 bootstrap／載入對話 | 量測 resume latency 與磁碟用量 |
| UID + Bubblewrap | 多 session 共享 image，隔離可寫檔與程序；不隔離網路與 kernel | 高信任差異 workloads 考慮獨立 Pod／VM 邊界 |
| Image 預裝 Python 與 resources | runtime 無外網仍能寫碼與執行；image 更新成本較高 | 鎖版、供應鏈審查與內部 image promotion |
| 原生 Pi packages | 保留 Pi skills/extensions/prompts 行為；安裝 hooks 是程式碼 | 公司 package catalog 與來源政策 |
| OnDelete + Retain | 避免升級／縮容意外丟失活動 session | 平台需要人工或額外 controller 做 drain／更新 |
| 持久化應用 tracing | 可以交叉核對工具與檔案；不是全量 system trace | 統一 trace ID、OTel、監控 backend 都尚未提供 |

## 故障處理契約

| 故障 | 保留什麼 | 操作方向 |
|---|---|---|
| MongoDB 不可達 | worker 上既有檔案與程序可能仍在 | 恢復 DB，觀察 readiness；不要重送不確定 prompt |
| Worker 不可達 | binding 與原 sandbox_id | 恢復同 worker／PVC，或明確 DELETE 重試 |
| Pi timeout／異常 | transcript／已寫檔可能已存在 | 查看證據、reconnect；沒有自動 prompt 重試 |
| Package CLI 或 reload 失敗 | 設定／檔案可能已部分改變 | 查詢實際 package 狀態，修復後 reload |
| API 重啟 | MongoDB registry + history | 重新建立 pools／locks，重啟 cleanup |
| Worker 重啟 | UID metadata + session data | 按需載入相同 session；不恢復程序記憶體 |
| PVC 遺失 | MongoDB 只剩路由 metadata | 從協調好的備份恢復；不自動換 worker |

## 尚未提供的服務

專案沒有 message broker、job scheduler、MongoDB operator、Vault operator、Ingress controller、MinIO server、metrics collector 或 tracing backend。不要把已安裝 Python SDK／client library 當成已有相對應的服務。公司需要的服務、TLS、監控、備份與 image registry 由平台整合。

## 文件與版本維護

修改 endpoint／env／volume／lock／資料模型時，同步更新相關 Markdown 與 topology.json，再執行 make docs、make docs-check。HTML 只含文件與設定名稱，不打包真實 .env、session artifacts 或 credentials。
