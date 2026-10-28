# 桂科融 V3

现有桂科融工作台已迁入 `src/guikerong_finance`，使用项目锁定的 **FrameX 0.4.2 + Mindforge 0.0.40**。保留企业资料、逐产品匹配、聊天、专业知识、人工复核、历史继续和 SSE 进度。

## 启动

当前目录已复制原项目的产品库、历史会话、公司资料和本地模型配置。Windows 可直接运行 `start.cmd`，或执行：

```powershell
$env:UV_CACHE_DIR = "$PWD/.uv-cache"
uv sync --locked --group dev
uv run python -m guikerong_finance
```

工作台：<http://127.0.0.1:8000/>；FrameX 文档：<http://127.0.0.1:8000/docs>。
`/health` 检查产品库与配置状态，不发起付费模型调用。

模型配置统一使用根目录 `config.yaml`。新部署可复制 `config.example.toml` 为 `config.toml`、`config.example.yaml` 为 `config.yaml`，填写 `sources.finance.api_key`、`sources.finance.models.matching` 和 `sources.finance.base_url`。同一配置段可调整超时、重试、并发、输出长度和思考模式；`finance` 段配置应用环境与匹配总超时。模型调用继续复用 Mindforge 服务和包内提示词。

`start.cmd` 和上述启动命令不再加载 `finance.env`，该文件仅保留作迁移备份。进程中显式设置的环境变量仍可覆盖 YAML；修改 YAML 后需要重启服务。不要把旧业务变量放入根目录 `.env`，FrameX 0.4.2 会严格解析该文件并拒绝额外字段。配置、密钥、数据库和企业原文均已加入忽略规则，不进入安装包。

## V3 目录职责

| 位置 | 职责 |
| --- | --- |
| `__init__.py` / `api.py` | 插件元数据；`BasePlugin`、`on_register`、`on_request` 注册 |
| `__main__.py` / `framework.py` / `application.py` | FrameX 应用装配、生命周期、静态工作台及 HTTP 边界 |
| `models/` | Pydantic 输入、输出和业务模型 |
| `service/` | 匹配编排、证据校验、会话、知识、历史投影及模型适配 |
| `db/` | SQLModel 表模型、查询、事务和 SQLite 会话适配 |
| `prompts/` | 版本化 YAML、suffix、匹配及聊天上下文 |
| `static/` | 原工作台 HTML、CSS、JavaScript |

FrameX 插件从 `guikerong_finance.api` 加载；Mindforge 注册及资源包名统一为 `guikerong_finance`。现有 HTTP 路径仍使用 `/api/v1`，返回原业务 JSON；保留 SSE 的原事件协议。OpenAPI 位于 `/api/v1/openapi.json`，旧 `/openapi.json` 会重定向过去。

当前持久化继续使用两个 SQLite 文件：`data/products.sqlite3` 和 `data/harness.sqlite3`。SQL 查询已改为 SQLModel，保持原表结构、事件内容和事务语义。Mindforge 0.0.40 的 SQLManager 不能直接初始化 SQLite，因此提供集中式 SQLModel 会话适配，没有伪造 MySQL 配置或修改框架源码。版本兼容处理见 [迁移说明](docs/MIGRATION.md)。

## 产品与知识导入

迁移后的产品数据库已可用，无需重复导入。CSV 只作为显式导入源，不在运行时自动回退。

```powershell
# 默认预览；添加 --apply 才发布目录版本
uv run python -m guikerong_finance.service.catalog_import

# 默认预览；添加 --apply 才发布知识事件
uv run python -m guikerong_finance.service.knowledge_import knowledge/2026-10-27/manifest.json
```

在其他部署复制旧数据库时，可使用 `python scripts/migrate_data.py <旧项目目录> --apply`；默认目标为当前目录 `data`，已存在的数据库会拒绝覆盖。该命令保留 WAL 中的已提交事务并进行完整性检查，不修改源库。

`GUIKERONG_DATA_ROOT` 可指定数据根目录（默认启动时的工作目录）；也可分别设置 `GUIKERONG_CATALOG_DB`、`GUIKERONG_HARNESS_DB`、`GUIKERONG_CSV_PATH`。`GUIKERONG_MINDFORGE_CONFIG` 可指定 YAML 文件，空字符串禁用 YAML 配置。

## 验证

```powershell
uv run --group dev poe test-ci
uv run ruff format --check src tests scripts
node --check src/guikerong_finance/static/chat.js
uv build
```

测试使用临时数据库和模拟模型 HTTP，覆盖率门槛保持 80%。源码仓库未附带私有公司资料时，测试生成同规模的合成资料夹具；本地存在公司资料时会复制到隔离目录验证完整转发。真实数据库、原始企业文件和模型密钥不被测试写入。

本次迁移核验了原库与副本的 111 条事件、36 条产品记录、目录版本和活动指针，逐条内容摘要一致。原项目目录保持不变。旧模板 YAML 备份在 `.local/config.pre-migration.yaml`，历史导出和研究资料副本在 `.local/legacy-artifacts/`。未进行真实模型联网推理、生产部署或远程提交。
