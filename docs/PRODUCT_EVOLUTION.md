# Work Memory 产品演进基线

更新日期：2026-09-03

## 北极星

用户只记得零散线索时，也能在几秒内找回一个完整工作任务，理解它为何相关，回到原始证据，并继续执行。原始会话是不可变证据；事项、项目、知识和 Agent 回答都是可重建、可修正的派生层。

## 外部案例与取舍

- [Operon](https://github.com/hasanyilmaz/operon)：父任务汇总所有后代进度；[Tududi](https://github.com/chrisvel/tududi/blob/main/docs/00-tasks-behavior.md) 明确采用单层子任务、父级 `x/y` 汇总、折叠展开，以及搜索命中子项时显示父级上下文。Work Memory 因此只引入一层“大任务 → 派生事项”，不制造无限任务树。
- [openCode History Search](https://github.com/joeyism/opencode-history-search)：提供关键词、正则、模糊、多词 AND、日期与 user/assistant 角色筛选；[Echomine](https://github.com/aucontraire/echomine) 提供 BM25、完整短语、布尔条件、角色排除与 provider-neutral 内容分类。Work Memory 对应采用“完整短语先返、用户按需扩大”以及面向阅读意图的消息预设，而不是暴露内部类型复选框。
- [next-wiki](https://github.com/hugogu/next-wiki)：区分不可变原始材料、AI synthesis/draft 与审核发布；[Kompl](https://github.com/tuirk/Kompl) 用 typed claims、关系、摘要和确定性页面归属维护知识并保留引用。Work Memory 对应把知识按大任务持续汇编，摘要可重建，原事项与原会话仍是出处。

- [Open WebUI](https://github.com/open-webui/open-webui)：采用搜索结果原地预览、状态筛选、分页和轻量字段投影。Work Memory 对应采用“事项先返回、原始会话异步补充”、筛选视图与 compact API。
- [AppFlowy](https://github.com/AppFlowy-IO/AppFlowy)：数据库分组使用可折叠区，并提供紧凑模式提高单屏信息量。Work Memory 保留快速本地检索，Codex 只做第二阶段理解和重排；首页工作组同样支持折叠但不牺牲任务入口。
- [Plane](https://github.com/makeplane/plane)：用轻量列表、筛选、保存视图和工作项状态组织大量工作。Work Memory 的保存搜索必须可创建、复用和删除，任务层避免重复使用重卡片。
- [GitHub Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-table-layout)：把分组数量放在组头，并允许按需显示字段。Work Memory 的会话组头只保留来源、标题、时间、项目和任务数，详情渐进披露。
- [Huly](https://docs.huly.io/task-tracking/viewing-issues/)：默认列表按状态组织，并把预览、排序、筛选和字段显示作为独立动作。Work Memory 将“折叠任务”和“打开原会话”拆成两个明确入口，避免一处点击承担两种语义。
- [LobeHub](https://github.com/lobehub/lobehub)：强调白盒记忆、多 Provider 和最近使用优先。Work Memory 的记忆必须可编辑、有证据，来源与 Agent Provider 不写死在页面分支中。
- [AFFiNE](https://github.com/toeverything/AFFiNE)：本地优先并以可扩展构件组织知识。Work Memory 保持 SQLite 本地证据层，同时让新增来源、Provider 和视图可以增量接入。
- [OpenHands Agent Canvas](https://github.com/OpenHands/OpenHands)：实时执行状态优先于缓存接口，运行中的动作与完成结果使用同一位置渐进更新。Work Memory 同样让原始会话先出现，再由轮询对账把它原位演进为任务组。
- [work-buddy Threads](https://work-buddy.ai/)：以渐进披露的行动队列汇集捕获内容，先让用户知道系统看见了什么，再展开需要判断的细节。Work Memory 只对尚无任务的新会话展示完整阶段轨道，已有任务更新时使用轻量状态提示。
- [OpenAI Symphony](https://github.com/openai/symphony/blob/main/SPEC.md)：用持久状态、轮询和 reconciliation 管理异步 Agent 运行。Work Memory 的前端按同步与 Agent 状态指纹刷新 Today，不依赖一次请求恰好赶上完成瞬间。

## 三轮审查法

每次重要迭代至少连续复查三轮，每轮都回答六个问题：

1. 美学：层级是否清楚，动效是否表达真实状态，是否存在无意义装饰？
2. 功能：用户能否从发现、判断、核验走到继续执行？
3. 使用者：只记得模糊线索、只用键盘或使用窄屏时是否仍能完成任务？
4. Bug：异步响应、翻页、筛选切换、暂停/失败和空数据是否会产生陈旧状态？
5. 速度：首屏是否只取可见字段，长列表是否分页，慢检索是否阻塞快结果？
6. 扩容：新增来源、Agent Provider、事项类型或视图时，是否只需修改一个注册点？

## 当前结构边界

- `sessions/messages`：原始证据层，只追加和索引，不被 Agent 覆盖。
- `major_tasks → work_items/*`：一层派生工作记忆；大任务代表跨会话持续目标，任务、决策、方案和经验是子项，均保留输入指纹、模型版本、证据和人工审核状态。
- 历史归组：旧事项依据项目与稳定 `dedupe_key` 本地确定性回填，不调用模型；新分析显式输出 `major_task_key/title/summary`；用户修改的父级标题不会被 Agent 覆盖。
- 快速搜索：完整短语原文检索与结构化事项并行；分词/近义扩大由用户触发；Codex 查询理解为可关闭的异步增强。搜索结果先显示大任务，再显示子项和原会话。
- 会话阅读：服务端在分页前应用 `conversation/assistant/user/process/all` 五种 provider-neutral 视图，并同时返回各视图计数；证据锚点临时打开全部记录，避免目标消息被偏好筛掉。
- 知识库：复用大任务归属，将问题/根因、可复用方案、经验/结论、待验证下一步生成分点汇总；汇总是派生展示，证据仍落在子项和原消息。
- 首页接口：使用 `compact=true` 只返回可见字段；详情在用户打开抽屉时再加载。
- 今日进行项：按“最近关联会话 → 任务”展示；同步完成后即显示会话壳，并明确经过“已捕获会话 → 正在理解 → 已形成任务”。尚无任务时展示完整阶段轨道，已有任务吸收增量时只显示轻量提示；会话组可折叠并在本地记忆状态，原会话使用独立入口，跨会话任务只归到最近上下文并标记关联会话数，手动任务进入独立分组。
- Agent 队列：人工指定优先级优先；同一优先级按会话最近活动时间处理，而不是按历史入队 ID，避免刚开始的工作被陈旧积压淹没。
- 状态对账：Today 可见时轮询紧凑的同步与 Agent 状态；状态指纹变化、显式同步完成或页面重新可见时自动刷新业务数据，迟到响应不能覆盖新结果。
- 待确认箱：支持逐项选择、全选本页和升级为全选全部待确认；批量确认使用单事务并保留逐项反馈记录，全量选择提交时校验选择时的总数，防止新到事项被意外包含。
- 来源注册：前端 `SOURCE_CATALOG` 提供稳定名称，同时自动合并后端发现的新来源。
- Provider：UI 从 `/api/work/agent/status` 读取当前 Provider；配置层保留 CLI、本地和 OpenAI-compatible 接入。

## 性能护栏

- 今日页初始业务请求不超过 4 个；会话列表、项目和任务等非当前页面数据延迟加载。
- `/api/work/today?compact=true` 在当前真实数据规模下目标小于 25 KB。
- `/api/work/agent/status?compact=true` 目标小于 2 KB，不返回任务历史。
- Agent Provider 可用性探测采用短时缓存，状态轮询不得反复启动 CLI；结构化搜索每页只渲染 20 条。
- 完整短语原文搜索与结构化搜索并行，任一结果不得等待 Codex；扩大搜索只能由用户动作或异步 Agent 触发。
- 新查询必须取消旧的浏览器请求；迟到响应不得覆盖新筛选结果。
- 单页列表必须数据库分页或显式分批显示，禁止无上限 DOM 累积。

2026-09-03 在 14,534 条真实事项的隔离副本上：包含旧 FTS 升级的一次性父级迁移约 4.4 秒（纯父级回填约 1.1 秒）；结构化查询 `qwen3.8` 约 138 ms、`显存 OOM` 约 48 ms、`M50` 约 17 ms，较改造前约 0.58 秒的常见查询明显下降。真实 14 GiB 会话库的完整短语原文查询约 2–64 ms；宽词扩大查询仍可能达到秒级，因此不进入默认首返路径。数据增长后若越过上述护栏，应先做字段投影、查询计划和分页审计。

## 下一阶段路线

1. 为原始 FTS 建立固定真实语料基准，优化高频宽词的候选裁剪，同时保持会话级完整召回。
2. 增加搜索结果的双栏证据预览，减少在“结果—会话—返回”之间跳转。
3. 把单文件前端拆成搜索、会话、工作项和运行状态模块，并增加浏览器端回归测试。
4. 记录“打开、复制上下文、确认、忽略”等本地行为，用于评估召回质量；默认不上传遥测。
5. 为来源适配器和 Agent Provider 定义插件协议；向量检索保持可选，不能成为原始搜索的硬依赖。

每个新功能都应同时给出用户任务、可验证状态、失败退路、数据规模上限和扩展接口；只有视觉展示而没有动作闭环的组件不进入首页。
