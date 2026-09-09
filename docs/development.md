# 開發、uv 與程式品質檢查

## 安裝與依賴分組

主機使用 Python 3.12、uv 0.10.x（CI／Docker 固定 0.10.9）、GNU Make、Helm 3，以及 Docker Compose。uv 安裝方式見 [官方安裝文件](https://docs.astral.sh/uv/getting-started/installation/)。Hadolint 預設使用固定版本 Docker image，不必在主機另裝 binary。

```bash
uv sync --locked
make check
# 修改 Docker／隔離／bootstrap／model transport 時再跑真正容器測試
make test-container
```

`pyproject.toml` 定義依賴，`uv.lock` 固定所有解析出的版本與來源。`.python-version` 是 3.12，project 也限制為 `>=3.12,<3.13`。這是應用專案，不建立／發布 Python wheel（`tool.uv.package=false`）。

| 分組 | 用途 | 是否進正式映像 |
|---|---|---|
| project.dependencies | API／worker supervisor 的執行依賴 | 是，獨立 `/opt/server` |
| test | pytest、pytest-asyncio | 只有 sandbox 的 `test` build target |
| dev | Ruff、yamllint、pre-commit，並包含 test | 否，主機／CI 使用 |
| sandbox | pi 生成的程式可用的應用套件 | 是，唯讀 base site-packages |

常用依賴操作：

```bash
uv add package-name
uv add --group dev tool-name
uv add --group sandbox 'package-name==1.2.3'
# 手動修改 pyproject.toml 後才執行；正常測試不會自動更新 lock
uv lock
uv sync --locked
```

根目錄與 sandbox 舊的 requirements 檔已移除。Docker build 會由 `uv.lock` 匯出 sandbox group 的完整含 hash requirements，到 image 的 `/opt/python-runtime/requirements.txt`，再由 pip 或 uv 安裝。修改 sandbox group 後記得重建 image；runtime 不從網路補依賴。所有 groups 共用解析約束，若同一套件版本互斥，uv lock 會報錯，不能忽略衝突。

Intel macOS 主機使用條件式 `cryptography<49` 約束，因為 [cryptography 49 移除了 x86_64 macOS 支援](https://cryptography.io/en/49.0.0/changelog/)。此條件已寫入 pyproject 與 lock，Linux 部署及 Apple Silicon 不受這個上限影響。

## Ruff：formatter 與 linter

```bash
make format       # safe fixes + format，會修改原始碼
make lint-python  # check + format --check，不修改
```

Ruff 與 VS Code 使用同一份 pyproject 設定：Python 3.12、100 欄、4 spaces、雙引號、LF。Lint 明確啟用 `E4/E7/E9`、`F`、`I`、`UP`、`B`、`SIM`、`C4`、`PIE`、`RUF`，涵蓋語法／未使用名稱、import 排序、現代 Python、常見 bug 與可維護性；不使用 `ALL` 或 preview，也不與 formatter 重複管控換行長度。

FastAPI 的 Depends／Header 宣告和 Path 不可變預設值列入 Bugbear 的 immutable calls；其餘例外應盡量縮小到個別行並說明原因。`artifacts/` 與歷史 `reports/` 不作為 Python source 格式化。嵌入測試中的 Python／shell 文字是 runtime fixture，不應因格式化而改變實際語意。

## YAML 與 Dockerfile

```bash
make lint-yaml
make lint-docker
make lint-helm   # Helm lint、渲染與結構驗證
make lint        # Python、YAML、Dockerfile、Helm
```

- yamllint：YAML 語法、重複 key、縮排、尾端空白與 truthy 值；使用 strict 模式。允許省略 document-start；GitHub Actions 的 `on` key 不當作布林錯誤。Compose 的 `!reset` 保留。
- Hadolint：固定 `hadolint/hadolint:v2.12.0-debian`，檢查兩個 Dockerfile 和內嵌 shell；warning 即失敗。只豁免 `DL3008`：Debian 安全套件版本隨 base image 更新，避免硬綁過時 apt 版本。Python 依賴仍完整鎖定。
- Hadolint 容器以唯讀掛載程式碼且 `--network none` 執行；第一次取得工具 image 仍需 Docker daemon 可存取 registry。離線公司環境應先將 image 匯入／鏡像，必要時覆寫 `HADOLINT_IMAGE` 指向內部的同版本 image。

## 測試與 Docker targets

`make test` 在主機執行 pytest，Linux sandbox 測試會 skip；`make test-container` 才驗證 Bubblewrap、Pi RPC、預裝套件、兩個本機 mock gateways 和完整工具執行。

正式 Compose 映像不包含 pytest、Ruff、測試 fixtures；API 使用 `/opt/server/bin` 的 runtime environment。`compose.offline-test.yaml` 選擇 sandbox Dockerfile 的 `test` target，使用獨立 image 名稱 `pi-agents-sample-sandbox-test`，避免測試 build 覆蓋正式 image tag。測試 container 只有 loopback，無公網、無真實 API key；測試程式與歷史回歸 fixture 只存在 test image；API 正式映像仍保留 demo scripts。

```bash
# 只跑指定容器測試，也要先 build 正確的 test target
docker compose -f compose.yaml -f compose.offline-test.yaml build sandbox-1
docker compose -f compose.yaml -f compose.offline-test.yaml run --rm --no-deps \
  -e RUN_ISOLATION_TESTS=1 sandbox-1 /opt/server/bin/python -m pytest tests/test_gateway.py -q
```

## VS Code 與 Git hooks

開啟 repository 後安裝 `.vscode/extensions.json` 推薦的 Python、Pylance、Ruff、YAML、Docker、EditorConfig 擴充，先 `uv sync --locked`，再選取 `.venv/bin/python`。Python 儲存時由 Ruff format，explicit save 執行 safe fixes 和 imports 整理；YAML／Dockerfile 的完整驗證以 Tasks 的「Quality: lint all」為準。Hadolint editor 擴充若需 native executable 可自行安裝，不影響 Docker 版檢查。

VS Code tasks 包含 sync、lint、format、host tests 與 offline sandbox tests。`.editorconfig` 統一 UTF-8／LF／尾端換行，Python 4 spaces、JSON/YAML/TOML 2 spaces、Makefile tab；Markdown 保留有意義的尾端空白。

需要本機 commit hook 時自行啟用：

```bash
uv run --locked pre-commit install
uv run --locked pre-commit run --all-files
```

Hook 使用本專案 uv lock，不另外下載不一致的 Ruff 版本；Dockerfile hook 需要 Docker。沒有自動修改你現有的 Git hooks。GitHub Actions 的 Quality workflow 會執行 `make check` 和 `make test-container`，不使用模型金鑰。

Agent 協作規範見 [AGENTS.md](../AGENTS.md)，部署環境變數見 [configuration.md](configuration.md)。


Helm 模板由 `make lint-helm` 渲染後檢查，原始 Go templates 不直接交給 yamllint；渲染 YAML 僅額外允許 Helm toYaml 的 indentless lists（`.yamllint-rendered.yaml`）。Chart 測試涵蓋 1／3 workers 的直接 DNS 路由、PVC、Secret refs、NetworkPolicy 和不合法 values。修改生命週期／chart 時還需 `make test-container`；K8s schema／server admission 驗證方式見 [kubernetes.md](kubernetes.md)。
