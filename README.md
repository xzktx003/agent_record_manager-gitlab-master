# 🧠 Work Memory · 个人工作中枢

把多台服务器上的 Copilot / Codex / Claude / OpenCode 对话自动沉淀为可搜索、
可审阅、可继续执行的任务、项目、决策与解决方案。原始会话始终作为
不可变证据保留。

## 功能

- 📥 **自动备份**: 从多台服务器 SSH 同步 Copilot、Codex、Claude 和 OpenCode 对话记录
- 🔍 **全文搜索**: SQLite FTS5 驱动的高速全文搜索
- 📊 **统计概览**: 查看会话数量、来源分布、服务器分布
- 📤 **导出功能**: 支持 JSON / Markdown / OpenAI Fine-tune JSONL 格式
- 🤖 **MCP 接口**: 专为 Agent 设计的 API，方便二次分析
- ✦ **Agent 自动理解**: Codex CLI 自动复用本机登录态，无需额外 API Key
- ✅ **大任务级记忆**: 任务、决策、方案和经验归入同一持续目标，并聚合跨会话证据
- ◌ **实时会话状态**: 新会话先出现，再从“已捕获 → 正在理解 → 已形成任务”原位演进
- 🔗 **消息级证据**: 摘要、根因、方案和结果都可跳回原始消息
- 🗂️ **工作管理**: Today、待确认箱、任务、项目、知识、时间线和复盘
- ⌕ **渐进式智能搜索**: 完整短语原文与结构化结果并行返回，按需扩大检索，再由 Codex 重排并回答
- 💬 **会话阅读视图**: 精简对话、只看 AI、只看用户、工具与思考、全部记录五种服务端分页视图
- ▶ **继续工作上下文包**: 汇总现状、关键结论、下一步和 Resume 命令
- ⏰ **定时同步**: 后台每 N 分钟自动拉取最新对话
- 🖥️ **设备管理**: 在页面新增、编辑、测试、停用或删除本机/Linux/macOS 数据源
- 🔐 **用户隔离**: 登录保护；每个用户拥有独立的会话、事项、设备和模型数据空间
- 🖥️ **Web 界面**: 暗色主题的现代化界面

## 快速开始

```bash
# 1. 运行 bootstrap 脚本创建项目
python3 agent_memory_setup.py --target-dir ~/agent-memory --install --run

# 或者分步执行:
python3 agent_memory_setup.py --target-dir ~/agent-memory  # 创建项目文件
cd ~/agent-memory
pip3 install -r requirements.txt                            # 安装依赖
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 18080  # 启动服务
```

## 访问

- Web 界面: http://localhost:18080
- API 文档: http://localhost:18080/docs
- MCP 接口: http://localhost:18080/api/mcp/

## 服务运行与自愈

生产环境使用无热重载的单进程 Uvicorn（同步与 Agent worker 当前仍内置于同一
进程，因此不能直接增加多个 Web worker）。仓库提供用户级 systemd 单元与独立
watchdog：异常退出由 systemd 拉起；进程假活或线程池耗尽连续三次才重启。

```bash
# 查看服务、探活、运行指标和最近日志
./scripts/service_status.sh

# 开发时才使用热重载
./run-dev.sh

# 并发回归；p95 或错误率越线时退出码非 0
python3 scripts/smoke_load.py --requests 240 --concurrency 12
```

运维接口分为三层：`/health/live` 只验证事件循环存活；`/health/ready` 独立检查
SQLite、AnyIO 线程池、同步任务超时和 Agent worker 存活；`/api/diagnostics/runtime` 和 `/metrics` 提供并发、
慢请求、线程池及数据库/WAL 体积。详细的 27 项审查证据见
[可靠性审查记录](docs/RELIABILITY_REVIEW_2026-08-02.md)。

## 配置

### 登录与用户隔离

生产环境在 `config.yaml` 中启用登录：

```yaml
auth:
  enabled: true
  session_hours: 12
  cookie_secure: false       # HTTPS 反向代理上线后改为 true
  allowed_origins: []        # 默认只允许同源浏览器请求
```

首次启用前创建管理员。第一个管理员会承接原有 `agent_memory.db` 数据；以后创建的
用户从空的独立数据空间开始。用户可在“同步与整理 → 用户与访问保护”中增减，停用
用户或删除设备都不会删除已经同步的数据。

```bash
python3 scripts/auth_admin.py init-admin --username admin
python3 scripts/auth_admin.py list-users
python3 scripts/auth_admin.py reset-password --username admin
```

浏览器使用 HttpOnly、SameSite=Strict 会话 Cookie，写操作还需要 CSRF Token。
CLI/MCP 可由用户通过认证 API 创建个人 Bearer Token，或由本机管理员签发：

```bash
python3 scripts/auth_admin.py create-token --username admin --name mcp
curl -H 'Authorization: Bearer arm_...' http://localhost:18080/api/mcp/summary
```

密码、会话 Token 和 Bearer Token 均不会明文入库。HTTP 登录只能提供访问控制，
不能防止链路窃听；服务跨主机访问时应配置 HTTPS，并将 `cookie_secure` 改为
`true`。

### 同步设备

首次启动会把 `config.yaml` 中的服务器导入独立的私有设备库。之后可直接在
“同步与整理 → 同步设备”中新增、编辑、测试、停用或删除设备，修改从下一次
同步开始生效。删除设备只删除连接配置，已经同步的会话、消息和 Agent 整理结果
不会删除；SSH 密码为只写字段，接口不会返回原值。

下面的 YAML 仍可用于首次导入：

```yaml
servers:
  - name: "My-Server"
    host: "10.0.0.1"
    port: 22
    user: "username"
    password_env: "AGENT_MEMORY_SERVER_PASSWORD"
    copilot_dir: "~/.copilot/session-state"
    codex_dir: "~/.codex"
    claude_projects_dir: "~/.claude/projects"
    opencode_dir: "~/.local/share/opencode"
    enabled: true

local:
  copilot_dir: "~/.copilot/session-state"
  codex_dir: "~/.codex"
  claude_projects_dir: "~/.claude/projects"
  opencode_dir: "~/.local/share/opencode"
```

Claude Code / Claude CLI 会话从 `~/.claude/projects/{project}/{sessionId}.jsonl` 导入；未配置 `claude_projects_dir` 时会默认使用 `~/.claude/projects`。

OpenCode 默认从 XDG 数据目录读取：Linux 通常是
`~/.local/share/opencode/opencode.db`。当前 SQLite 格式和旧版
`storage/session`、`storage/message`、`storage/part` JSON 格式都会识别；若配置
`opencode_dir`，可填写 OpenCode 数据根目录或直接填写 `opencode.db`。
本机读取使用只读 SQLite 事务，因此能看到 WAL 中已提交的数据；远端同步会先在
远端生成只含 `project/session/message/part` 的 `0600` 一致性快照，再下载解析，
避免只复制主库而漏掉 WAL，也不会把 OpenCode 凭证表带离源机器；DB/WAL 指纹
未变化且历史根会话仍完整时会直接跳过快照与传输。

推荐通过环境变量保存 SSH 密码。旧的 `password` 字段仍兼容，但不建议继续
把新密码明文写入 YAML。Agent 分析前会自动遮蔽常见密码、Token、API Key、
Authorization Header 和私钥块。

## Agent 工作记忆

系统自动创建且保留一个 Codex CLI Provider，复用 `~/.codex` 的登录态。模型和
思考强度默认跟随 `~/.codex/config.toml`，也可以在“模型管理”中从当前账号的
Codex 模型目录显式选择。分析使用 ephemeral 会话、临时目录、只读沙箱并禁用
工具，通过 JSON Schema 约束输出。Agent 不可用时，原始会话同步、FTS 搜索和
导出不会受影响。

“同步与整理”页提供模型管理：Codex CLI 自动接入且不可删除；OpenCode CLI 和
OpenAI-compatible 服务由用户手动新增。可以编辑、测试和切换默认 Provider，
也可以为每个 Provider 单独选择“使用服务端系统代理”或“直连”。该选择随用户的
独立 Provider 数据库保存：开启时 CLI 和 HTTP 客户端继承服务环境中的代理，关闭
时会显式移除代理环境。模型配置、连接测试结果和写入式 API Key 保存在独立的
`agent_memory.providers.db` 中，不受会话同步写锁影响；API 只返回密钥是否已
配置，不返回原值。
数据库及 WAL/SHM 文件会强制设置为 `0600`。HMDeepSeek 使用标准
`/chat/completions` + JSON Schema，火山 Agent Plan 的 `/api/plan/v3` 会自动改用
function tool 结构化返回。

systemd 部署可在 `~/.config/agent-record-manager/environment` 中配置宿主机代理；
未创建该文件时服务仍可正常启动：

```ini
http_proxy=http://127.0.0.1:7990
https_proxy=http://127.0.0.1:7990
no_proxy=10.0.0.0/8,127.0.0.1,localhost
```

```yaml
agent:
  enabled: true
  timeout_seconds: 600
  settle_seconds: 300        # 持续写入的会话静默后再分析
  max_input_chars: 12000
  max_segments_per_session: 2
  redact_before_send: true
  allowed_sources: []
  excluded_sources: []
```

新同步的会话会立即出现在 Today，并自动进入持久化队列；同优先级下最近活跃的
会话先整理，Agent 完成后页面会自动更新。历史数据可在 Web 的“数据源与 Agent”页
分最近 90 天或全量排队。worker 遇到意外异常会自动拉起；Provider 超时会把输入
压缩后重试一次，避免同一份超长上下文反复超时。也可使用 CLI：

```bash
python3 scripts/work_memory.py status
python3 scripts/work_memory.py backfill --days 90
python3 scripts/work_memory.py backfill                 # 全量排队
python3 scripts/work_memory.py analyze-session <session_id>
python3 scripts/work_memory.py search 'qwen3.5 122b golden 显存' --source codex
```

## API 接口

| 接口 | 说明 |
|------|------|
| `GET /api/sessions` | 会话列表（分页、筛选及标题/路径/标记搜索） |
| `GET /api/sessions/{id}` | 会话详情（支持消息窗口与 `message_view=conversation/assistant/user/process/all`） |
| `GET /api/sessions/{id}/messages/{message_id}` | 按需读取单条消息全文 |
| `GET /api/sessions/stats` | 统计数据 |
| `GET /api/search?q=keyword` | 会话全文搜索；`match_mode=precise` 优先完整短语，`expanded` 扩大召回 |
| `GET /api/work/today` | Today 工作台 |
| `GET /api/work/items` | 任务/决策/方案列表与结构化筛选 |
| `GET /api/work/items/{id}` | 工作项、实体、关系与消息级证据 |
| `GET /api/work/items/{id}/context-pack` | 继续工作上下文包 |
| `GET /api/work/major-tasks` | 大任务列表、进度与分点汇总 |
| `GET /api/work/major-tasks/{id}` | 大任务及其全部派生事项 |
| `GET /api/work/search?q=...` | 快速任务级搜索及原始会话回退 |
| `POST /api/work/search/agent` | 异步 Codex 查询理解与证据化回答 |
| `GET /api/work/projects` | 项目聚合与健康状态 |
| `GET /api/work/timeline` | 会话与工作项时间线 |
| `GET /api/work/knowledge` | 按大任务汇编的方案、经验、问题与可追溯证据 |
| `POST /api/work/reviews/generate` | 日报/周报/月报，可选 Codex 归纳 |
| `POST /api/work/items/bulk-approve` | 批量确认选中事项，或带数量校验地确认全部待确认事项 |
| `GET /api/work/agent/status` | Provider、队列和 Agent 运行记录 |
| `GET /api/sync/devices` | 同步设备列表与已保留会话数 |
| `POST /api/sync/devices` | 新增同步设备 |
| `PATCH /api/sync/devices/{id}` | 编辑或停用同步设备 |
| `DELETE /api/sync/devices/{id}` | 删除连接配置（保留历史数据） |
| `POST /api/sync/devices/{id}/test` | 测试本机或 SSH 连接 |
| `POST /api/sync/now` | 触发全量同步 |
| `POST /api/sync/local` | 仅同步本地 |
| `GET /api/export/session/{id}?format=markdown` | 导出单个会话 |
| `GET /api/export/all?format=openai_jsonl` | 导出 Fine-tune 数据 |
| `GET /api/mcp/search?q=keyword` | MCP 搜索接口 |
| `GET /api/mcp/session/{id}` | MCP 会话分页接口（支持 `msg_offset` / `msg_limit`） |
| `GET /api/mcp/summary` | MCP 概览接口 |
| `GET /api/mcp/work/search` | MCP 任务级搜索 |
| `GET /api/mcp/work/item/{id}/context-pack` | MCP 继续工作上下文 |
| `GET /health/live` | 不依赖数据库和业务线程池的存活检查 |
| `GET /health/ready` | SQLite 与线程池就绪检查，异常返回 503 |
| `GET /api/diagnostics/runtime` | 本地运行诊断快照 |
| `GET /metrics` | Prometheus 文本指标 |

## 数据与可靠性原则

- `sessions/messages` 是原始证据层，Agent 永不覆盖原文。
- `work_items/*` 是可重建的派生层，保存模型、Prompt 版本、输入指纹和置信度。
- AI 提取默认进入待确认或自动确认状态；人工编辑后不会被重新分析覆盖。
- 同一 `dedupe_key` 自动聚合跨会话证据，近似项建立可解释的相似关系。
- 每个派生事项都属于一个一层“大任务”；历史数据本地确定性回填，新分析由 Agent 输出稳定父级 key，人工可修改归属。
- 活跃会话采用静默期去抖；失败任务指数退避，三次后等待人工重试。
- 快速搜索不依赖 Agent；尚未分析的历史记录始终通过原始 FTS 参与召回。
- OpenCode 根会话会聚合子 Agent；会话指纹包含 session/message/part 更新时间与数量，工具状态变化后整会话替换，避免把运行中的工具结果误判为追加日志。

## 产品取舍与参考

- [Pieces Workstream Activity](https://pieces.app/features/long-term-memory/ai-workflow-history)
  的时间线、自动工作摘要和按应用筛选，落实为本项目的 Today、项目、时间线与来源筛选。
- [OpenMemory](https://mem0.ai/openmemory) 的项目作用域、可管理记忆和审计思路，
  落实为工作项证据、人工确认/修正、Agent 运行记录、上下文包和 MCP 接口。
- [Khoj Search](https://docs.khoj.dev/features/search/) 的自然语言检索与自托管取向，
  落实为“本地 FTS 立即召回 + Codex 查询规划/重排”的渐进式搜索；当前无需额外向量服务。
- [agent-sessions](https://github.com/jazzyalex/agent-sessions) 的多 Agent 统一会话模型、
  SQLite/旧 JSON 双后端和安全恢复命令，以及
  [claude-code-history-viewer](https://github.com/jhlee0409/claude-code-history-viewer) 的
  DB/WAL 兼容思路，落实为 OpenCode 双格式发现、未知 part 保留和 `ses_*` 精确恢复。
- [deja-vu](https://github.com/vshulcz/deja-vu) 与
  [opencode-session-recall](https://github.com/rmk40/opencode-session-recall) 的可重建索引、
  版本指纹和安全增量原则，落实为按根会话签名判断变化、工具状态变化时全量替换。
- [Operon](https://github.com/hasanyilmaz/operon) 与
  [Tududi 的任务行为规范](https://github.com/chrisvel/tududi/blob/main/docs/00-tasks-behavior.md)
  都强调单层父子任务、父级进度汇总和“命中子项时保留父级上下文”，落实为“大任务 → 派生事项”的唯一层级。
- [openCode History Search](https://github.com/joeyism/opencode-history-search) 与
  [Echomine](https://github.com/aucontraire/echomine) 的短语、角色、日期和渐进式搜索设计，
  落实为完整短语快速首返、按需扩大范围和五种消息阅读视图。
- [next-wiki](https://github.com/hugogu/next-wiki) 与
  [Kompl](https://github.com/tuirk/Kompl) 将原始证据、AI 汇编和来源追溯分层，
  落实为按大任务汇编知识，同时始终能回到具体事项与原消息。

这里不会照搬“把所有活动压成不可验证摘要”的路线：原始会话是证据层，结构化
工作项是可重建派生层，Agent 回答必须返回到具体任务和消息。

持续演进的审查维度、性能护栏和扩展路线见
[产品演进基线](docs/PRODUCT_EVOLUTION.md)。

## 大数据量策略

- 会话和消息保存在 SQLite，列表、详情和全文搜索均在数据库侧分页。
- Web 对话区固定只渲染当前 100 条消息；翻页时替换窗口，不累计数万 DOM 节点。
- 超过 8,000 字的消息先返回预览，点击展开时才读取该条全文。
- FTS5 trigram 索引支持中英文子串检索；新建或维护重建的索引使用紧凑模式。
- 内容检索至少需要 3 个字符；更短的查询只匹配轻量的会话元数据，避免退化为全表扫描。
- 定时同步先比较文件指纹，只解析变化的会话；追加型日志仅写入和索引新增消息。
- 单会话和全库导出采用数据库分批读取与 HTTP 流式输出，避免在内存中拼接完整数据集。

已有大库可在线迁移到紧凑索引（回填期间旧索引继续提供查询）：

```bash
python3 scripts/optimize_database.py
```

迁移期间请预留足够的 WAL 临时空间。旧索引页面会变成 SQLite 可复用空间；若要让数据库文件在操作系统层面缩小，仍需另行安排停机 `VACUUM`。

## MCP 使用示例

让 Agent 调用 MCP 接口进行分析:

```bash
# 搜索相关对话
curl http://localhost:18080/api/mcp/search?q=gemma4

# 获取活动概览
curl http://localhost:18080/api/mcp/summary

# 获取特定会话详情
curl http://localhost:18080/api/mcp/session/codex-local-xxx?roles=user,assistant
```
