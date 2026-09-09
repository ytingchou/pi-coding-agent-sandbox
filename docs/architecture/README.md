# 架構設計總覽

本文件集描述目前 repository 的實作，涵蓋 Agents SDK、Session Manager、MongoDB、Pi worker、Python 3.12 隔離環境與兩種部署方式。這是一個同步 HTTP、單 API process、多 sandbox、多 session 的 sample；圖中的外部系統不是專案額外部署的服務。

## 文件地圖

| 設計面向 | 文件 | 實作來源 |
|---|---|---|
| 服務與部署拓撲 | [部署架構](deployment.md) | compose.yaml、charts/pi-sandbox/templates |
| Agent、RPC 與 session 狀態 | [執行架構](runtime.md) | orchestrator、sandbox/app.py、sandbox/rpc.py |
| 隔離、信任與資源 | [安全與資源架構](security.md) | sandbox/isolation.py、process.py、Dockerfile |
| 資料、索引、DB 邊界 | [MongoDB schema](../mongodb-schema.md)、[Registry 架構](../mongodb-registry-architecture.md) | documents.py、registry.py |
| Skills、extensions、packages | [資源擴充設計](../resources.md) | sandbox/resources |
| 離線 Python 套件 | [Python packages](../python-packages.md) | pyproject.toml、python_inventory.py |
| 清理與長駐程序 | [Session lifecycle](../session-lifecycle.md) | SessionManager.cleanup_once、Worker.suspend_idle |
| 追蹤、證據與用量 | [Session verification](../session-verification.md) | scripts/verify_sessions.py |
| 設計取捨與演進 | [架構決策](decisions.md) | 跨元件限制與後續方向 |
| 設定、操作 | [設定](../configuration.md)、[MongoDB](../mongodb.md)、[Kubernetes](../kubernetes.md) | .env.example、Helm values |

## 核心模型

一個邏輯 Agent 可以擁有多個 session，分配到不同 worker；每個 session 固定綁定一個 worker。Agent 是控制層的所有權與對話識別，不是一個永遠存活的 Python Agent 物件。每次 `/run` 都建立 SDK Agent，再載入該邏輯 Agent 的 history。

Session Manager 是 API 程序內的元件，不是獨立 container。它持有 HTTP pool、Agent locks 與 cleanup task；Worker supervisor 持有各 session 的 Pi subprocess、RPC pipes、session locks 與閒置暫停 task。MongoDB 不儲存 live connection，也不提供程序內鎖的替代品。

## 互動 HTML 文件

開啟 [architecture.html](../architecture.html)。此單一檔案內含本文件集與既有操作手冊的快照，可離線閱讀，不載入 CDN、不呼叫 API，也不會接觸 `.env`。

可切換 Compose／Kubernetes／session 圖，點選元件查看設定與限制，調整 Kubernetes worker 數量以探索拓撲，逐步查看 prompt 路徑，搜尋文件章節，以及列印成 PDF。圖是架構示意，不是即時監控；replicas 控制不會修改 Helm 或正在運行的系統。

從 repository 根目錄重建：

```bash
uv sync --locked --group docs
make docs
make docs-check
# 可直接用瀏覽器開 docs/architecture.html；也可啟動只綁 localhost 的預覽：
uv run --locked python -m http.server 8765 --bind 127.0.0.1 --directory docs
# http://127.0.0.1:8765/architecture.html
```

預覽伺服器只提供 docs 目錄；頁內各手冊可正常閱讀，跳到 docs 以外的 source code 連結請直接在 repository 開啟。使用 file:// 開啟 HTML 時，原始碼相對連結可定位本機檔案。

Markdown 是說明文字的來源；`docs/architecture/topology.json` 是互動圖來源；`docs/architecture/template.html` 是 UI。`scripts/build_architecture_docs.py` 產生 HTML，`--check` 檢查產物是否與來源一致。修改來源後須重新產生 HTML。Mermaid 原始碼保留在 HTML 的可展開區域，三張總覽圖由內嵌 SVG 呈現；無需外部 Mermaid runtime。

## 驗證邊界

目前已驗證本機 Compose、真實本機 MongoDB 與 Linux 容器隔離；Helm 有 lint／render／schema 檢查。公司實際的 CNI、CSI、Pod user namespaces、Vault controller、模型 gateway 相容性與 MongoDB failover 仍需在公司環境驗證。歷史 token 報告是指定執行的證據，不是目前版本每次執行的固定成本。
