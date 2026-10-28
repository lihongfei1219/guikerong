# V3 迁移记录

迁移日期：2026-10-28。源项目为同级 `guikerong`，目标包为 `src/guikerong_finance`。

## 分层映射

| 原模块 | 新模块 |
| --- | --- |
| `app/main.py` | `application.py`、`framework.py`、`http_boundary.py` |
| `app/harness_api.py` | `api.py`（FrameX 插件） |
| `app/models.py`、`app/harness_models.py` | `models/domain.py`、`models/commands.py` |
| `app/harness_store.py`、`app/catalog_store.py` | `db/events.py`、`db/catalog.py`、`db/entities.py`、`db/session.py` |
| `app/service.py` | `service/matching.py` |
| `app/ark_client.py` | `service/ark_client.py`、`service/runtime.py` |
| 匹配与聊天上下文 | `prompts/`，固定指令迁入版本化 YAML |
| 其余业务模块 | `service/` 下对应模块 |
| `app/static/` | `static/` |

没有保留 `app` 导入别名。入口、测试和内部导入全部使用新包名。原有初筛口径、引用校验、一次纠正、人工覆盖、知识作用域、历史输入及删除边界均由迁移后的回归测试覆盖。

## 已按安装版本核对的接口

- FrameX 0.4.2：`PluginMetadata`、`BasePlugin`、`on_register`、`on_request`、`load_plugins`、`run(test_mode=True)`，以及 `create_fastapi_application`、`APIIngress` 的实际实现。
- Mindforge 0.0.40：`register_mindforge(config, package_name)`、`get_llm(source, model)`、`get_prompt(tag)`、提示词渲染和 `SQLManager` 的实际实现。
- 包版本在 `pyproject.toml` 中固定，`uv.lock` 已重新解析并核验；没有凭旧文档猜测装饰器、注入参数或服务路径。

## 必要的版本适配

1. **同一路径的多个 HTTP 方法。** FrameX 按路径去重注册，因此会话 GET/POST、会话详情 GET/DELETE、知识 GET/POST 合并成单个插件方法，再分派业务命令。应用中的路由副本按 HTTP 方法生成独立 OpenAPI operation ID，补齐对应请求模型。
2. **请求对象注入。** FrameX 已注入一个 FastAPI Request，再直接声明 Request 会覆盖框架参数。使用有类型的依赖包装提供业务请求上下文。
3. **响应与断流。** 使用框架支持的 `raw_response=True` 保留既有响应。FrameX 的响应包装 BaseHTTPMiddleware 会打断 SSE 断流后的 worker 清理；这些业务路由全部直接返回响应，因此应用装配时移除这一特定包装中间件，保留 FrameX 注册、应用、生命周期和其他中间件。通过 ASGI 2.0/2.4 的接收断开、发送失败和任务取消测试。
4. **SQLite 会话。** Mindforge 0.0.40 SQLManager 同时创建同步、异步引擎，且只转换 MySQL 驱动，直接配置 SQLite 会失败。保留原数据格式，用集中式 SQLModel Session 上下文管理读快照、写锁、提交和回滚。业务查询没有拼接 SQL；少量驱动语句只设置 SQLite 的 WAL、安全删除和事务模式。此适配不等于已验证 MySQL 部署；切换数据库需要另外的连接配置和数据迁移。
5. **严格模型结果。** Mindforge 的 `ResponseMessage.to_dict()` 会修复非标准 JSON，无法保持原系统对截断响应、重复键和响应体大小的拒绝规则。因此复用 Mindforge 已注册模型的 OpenAI 客户端，通过其公开 `with_options` 和 `with_streaming_response` 接口获取原始 HTTP 响应，再进行原有严格校验。没有每次初始化整套 Mindforge，也没有改变模型请求消息。禁止 Mindforge TRACE 输出配置和企业正文。
6. **配置文件。** 原 `finance.env` 的模型密钥、地址、模型和调优参数已迁入根目录 `config.yaml`，启动不再加载环境文件。`sources.finance` 与提示词绑定保持一致；`finance` 段承接应用环境与匹配总超时。进程环境变量仍可覆盖 YAML。FrameX 严格处理 `.env` 的额外字段，不在该文件放入业务变量。YAML 不进行 `${VAR}` 字符串替换；DeepSeek/Ark 切换不会复用另一提供方的配置。

## 数据、资源与验证范围

- SQLModel 表映射兼容现有 SQLite：原来的 111 条事件、36 条产品及版本指针逐条摘要一致，未重写历史 payload。核验摘要保存在本地 `.local/migration-verification.json`。
- 数据复制使用 SQLite backup。源库因只读目录无法创建共享内存文件时，仅在没有待写 WAL 且源文件复制前后摘要一致的情况下使用 immutable 读取；失败不发布最终目标文件。
- 原始资料、CSV、知识包和静态工作台均已迁移；历史导出与研究文件保存在忽略目录。只公开 `static/`，不公开数据库和企业文件。
- 匹配规则版本保留 `product-preliminary-policy-4.6`。YAML 中的字面 JSON 大括号按 Mindforge formatter 规则转义，真实渲染结果与业务使用的指令一致。
- 安装包检查确认包含静态工作台和 YAML 资源，不包含运行配置、环境密钥、数据库、公司原文或本地备份。
- 数据库与模型访问测试为本地临时库和 HTTP 模拟；另用迁移后的实际数据进行首页、静态文件、健康、产品和会话读取检查。没有请求真实模型，没有向生产库写入，也没有执行发布、推送或数据库重建。

## 后续使用

启动与导入命令见根目录 README。不要再使用旧的 `uvicorn app.main:app` 入口。此迁移没有实现 Ray 分布式运行；当前 SQLite 与进程内服务通过 FrameX 的本地模式装配。
