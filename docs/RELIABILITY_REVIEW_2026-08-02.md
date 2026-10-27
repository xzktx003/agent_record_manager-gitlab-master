# 可靠性审查与故障复盘（2026-08-02）

## 结论

本次不是“端口没启动”，而是典型的假活：Uvicorn 监听进程和静态首页仍在，
但业务请求占满同步处理容量，用户看到空页面且没有任何解释。主因链为：页面存在
重叠/僵尸轮询，请求没有统一截止时间；FastAPI 的同步 handler 共用 AnyIO 默认
40 个线程令牌；SQLite 每次连接重复协商持久化 WAL 模式且锁等待长达 30 秒，
Provider 状态探测也可能并发启动外部 CLI。慢请求即使浏览器放弃，服务端工作仍会
继续，最终形成“进程活着、API 不可用”的容量耗尽。

修复将容量保护、故障可见性和进程自愈同时落地，避免把同类故障再次伪装成正常。

## 故障现场

- 旧服务自 7 月 30 日运行，8 月 2 日 worker PID `2268803` 仍监听 `18080`，并非进程退出。
- `/` 静态页面一度可快速返回，但 `/api/work/today`、`/api/sessions/stats`、
  `/api/work/agent/status` 及同步版 `/health` 均持续超时。
- worker 有 45 个线程，其中约 40 个处于同步请求池规模，连续超时请求会继续增加占用。
- 同一份 14 GiB 生产数据库在独立 Python 进程中生成 Today compact 只需约 `0.14s`，
  Agent compact 约 `0.07s`，排除了“Today 查询天然需要几十秒”。
- `get_db()` 每次请求执行 `PRAGMA journal_mode=WAL`、30 秒 busy timeout，且所有读取
  context 都执行 commit；页面 8 秒轮询允许重叠，两个 job poller 还存在完成后遗留
  interval 的顺序 bug。
- 受主机 Yama ptrace 策略限制，无法附加 `py-spy/strace` 获取旧 worker 的 Python 栈；
  因此不虚构某一条具体 SQL 栈，但线程池耗尽机制、请求行为和各候选阻塞点均有独立证据。

## 开源/官方设计基线

- [Starlette Thread Pool](https://www.starlette.io/threadpool/)：同步 endpoint 在 AnyIO
  线程池运行，默认仅 40 tokens，且与其他同步任务共享。
- [Kubernetes Probes](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-probes)：
  liveness 用于发现死锁并重启，readiness 用于停止向不可服务实例发送流量；两者职责不能混淆。
- [Uvicorn Deployment](https://www.uvicorn.org/deployment/)：`--reload` 面向本地开发；
  生产使用进程管理器，并设置并发、keep-alive 与健康检查边界。
- [SQLite WAL](https://www.sqlite.org/wal.html)：WAL 是数据库文件的持久属性；读写可并发，
  但仍只有一个 writer，应用必须设计 busy 预算与 checkpoint。
- [Dify docker-compose](https://github.com/langgenius/dify/blob/main/docker/docker-compose.yaml)：
  API、worker、beat 分离，配置 restart、healthcheck、执行超时和请求容量。
- [Open WebUI docker-compose](https://github.com/open-webui/open-webui/blob/main/docker-compose.yaml)：
  自托管服务明确声明 `restart: unless-stopped`，避免依赖交互终端存活。
- [OpenHands AGENTS.md](https://github.com/OpenHands/OpenHands/blob/main/AGENTS.md)：前端实时状态
  优先于陈旧缓存，同时保留数据获取/缓存管理边界；本项目采用“实时健康 + last-good 数据”。
- [systemd.service](https://man7.org/linux/man-pages/man5/systemd.service.5.html)：采用
  `Restart=on-failure`、启动频率限制、control-group 关闭和 journal 日志。
- [Prometheus Histograms](https://prometheus.github.io/client_python/instrumenting/histogram/) 与
  [Grafana k6 thresholds](https://grafana.com/docs/k6/latest/using-k6/thresholds/)：可靠性必须由
  延迟和错误率阈值自动判定，不能只人工点页面。

## 27 轮逐项审查

| # | 角度 | 发现 / 风险 | 学习并应用 | 状态与验证 |
|---:|---|---|---|---|
| 1 | 使用者 / Bug | 端口存在被误认为服务正常 | 拆出不做 DB/线程池工作的异步 `/health/live` | 已修；API 测试 |
| 2 | 功能 / Bug | 原健康检查无法表示业务是否可服务 | 参考 Kubernetes 增加 `/health/ready`，独立检查 SQLite 与线程池 | 已修；饱和时 503 测试 |
| 3 | 速度 | 所有同步 handler 共享 40 tokens，状态请求也会排队 | 同步状态改 async；就绪探测使用独立 2 线程 executor | 已修；并发测试 |
| 4 | Bug / 数据 | 每个连接重复执行持久化 `journal_mode=WAL` | 只在 `init_db()` 设置一次 | 已修；trace 回归测试 |
| 5 | 速度 / Bug | 30 秒数据库锁等待可一次占满整个池 | busy timeout 收紧为默认 5 秒，并允许环境变量配置 | 已修；锁预算审查 |
| 6 | 数据 / 扩容 | 纯读取 context 无条件 commit，语义含混 | 仅 `conn.in_transaction` 时提交 | 已修；全量回归 |
| 7 | 速度 / Bug | 高频状态请求可能并发运行 `codex --version` | Provider 探测非阻塞互斥、stale fallback、缓存 5 分钟 | 已修；24 路并发只探测 1 次 |
| 8 | Bug | Today 新请求不取消旧请求，慢响应可长期占用 | Today 使用 AbortController；quiet refresh 单飞 | 已修；静态回归 + 浏览器验证 |
| 9 | Bug / 速度 | 8 秒流水线 refresh 可重叠 | 共享 `pipelineRefreshPromise`，同一时刻只有一轮 | 已修；代码断言 + 压测 |
| 10 | Bug / 速度 | Agent 搜索 1.8 秒轮询可叠加 | 增加 in-flight guard 和 5 秒截止时间 | 已修；语法与回归测试 |
| 11 | Bug / 速度 | 同步任务 2.5 秒轮询可叠加 | 增加 in-flight guard 和 5 秒截止时间 | 已修；语法与回归测试 |
| 12 | Bug | eager tick 在 timer 创建前完成，会留下永久 interval | 两个 poller 均改为“先登记 timer，再 eager tick” | 已修；源码审查 |
| 13 | 功能 / 速度 | 全站 fetch 没有截止时间，失败永远转圈 | 中央 API helper：GET 15 秒、写请求 30 秒，并传递外部取消 | 已修；Node 语法检查 |
| 14 | 功能 / 扩容 | 长时 Codex 回顾会被普通写请求预算误杀 | 长任务显式声明 6 分钟预算，短状态请求 2.5–5 秒 | 已修；端点分级 |
| 15 | 使用者 | 服务异常时 Today 为空，用户不知道数据是否丢失 | 持久化 compact Today 的 last-good 快照，失败继续展示 | 已修；API/前端回归 |
| 16 | 美学 / 使用者 | 错误只在 console/toast，稍后看不到 | 增加克制的连续性状态条：连接中、缓存、降级、更新时间、重试 | 已修；响应式 UI 审查 |
| 17 | 功能 | 服务恢复但流水线指纹没变化，Today 不会更新 | 就绪状态从失败转正常时自动 reconcile；正常时每 60 秒校准 | 已修；状态机审查 |
| 18 | 速度 / Bug | 故障期间固定频率重试会再次制造请求风暴 | Today 失败采用 4–60 秒指数退避，且就绪前不发业务重试 | 已修；状态机审查 |
| 19 | Bug / 运维 | 生产以 `--reload` 运行，依赖终端且 watcher 本次未生效 | `run.sh` 改生产模式，热重载移到 `run-dev.sh` | 已修；shell/启动验证 |
| 20 | 运维 | 进程退出无人拉起 | 用户级 systemd `Restart=on-failure` + 3 秒退避 + start limit | 已修；SIGKILL 故障注入 |
| 21 | 运维 / Bug | 进程假活不会退出，普通 restart policy 无效 | 独立 watchdog 每 10 秒检查 live/ready，连续 3 次失败才重启 | 已修；SIGSTOP 故障注入 |
| 22 | 功能 / 速度 | 无法看到并发和慢请求，复盘只能猜 | runtime 诊断、Server-Timing、Prometheus 指标、最近 20 个慢请求 | 已修；API 测试 |
| 23 | 速度 | 没有自动判定的性能验收线 | 新增并发/老化脚本，强制 p95 与错误率阈值 | 已修；真实服务压测 |
| 24 | 扩容 / 运维 | 请求、FD、子进程和重启风暴没有边界 | Uvicorn concurrency cap、FD limit、KillMode control-group、journal 日志 | 已修；systemd verify |
| 25 | 功能 / 运维 | systemd 精简 PATH 与交互 shell 不同，首次启动找不到 Codex/Node | 显式声明 `.npm-global/bin`、`.local/bin` 与 Python 路径，不依赖 shell profile | 已修；隔离环境运行 `codex --version` |
| 26 | Bug / 运维 | 就绪请求超时会取消 future 并持续向专用 executor 排队，故障探测本身可能拥塞 | readiness 共用 shielded single-flight、1 秒结果缓存；SIGUSR1 可把全线程栈写入 journal | 已修；启动负载与信号诊断 |
| 27 | Bug / 速度 | 服务刚启动、初始同步写入且 Today 缓存为空时，并发首页可能与 writer 争用 | lifespan 在派发初始同步前预热 Today last-good；watchdog 发现 MainPID 换代即清零旧失败 | 已修；冷启动并发测试 |

## 实测结果

- 自动回归：`75 passed in 2.07s`；Python 编译、Node `--check`、bash `-n`、
  `systemd-analyze --user verify` 与 `git diff --check` 均通过。
- 首轮并发基线：240 请求零错误，但 Today 重复计算导致混合 p95 `3926.83 ms`，
  未通过 1500 ms 门槛。没有下调门槛，改为按数据库写入代次失效的 5 秒
  single-flight 缓存。
- 同步并发：主动触发 local sync 的同时运行 12 路 360 个混合请求，零错误，
  p95 `91.24 ms`，最大 `561.86 ms`，同步在 `1.76s` 完成。
- 冷启动并发：服务刚开始初始同步即运行 12 路 360 个混合请求，零错误，
  p95 `80.07 ms`，最大 `537.48 ms`，初始同步在 `1.35s` 完成。
- 持续老化：120 秒、8 路并发、3,702 个混合请求，零错误，p95 `26.36 ms`，
  p99 `46.82 ms`，最大 `225.77 ms`。
- 崩溃注入：SIGKILL 主进程后，systemd 将 PID `2964194` 替换为 `2965679`，
  live/ready 自动恢复。
- 假活注入：SIGSTOP PID `2965679` 后，watchdog 连续三次失败，于 48 秒内替换为
  PID `2967454`；恢复后 live/ready 均为 200。
- 搜索回归：查询“Qwen3.5 122b golden 显存”且筛选 Codex，任务级第一条为
  “修复 Qwen3.5 122B dump-golden 双卡显存爆炸”。
- 老化结束运行快照：3,734 请求、0 个 5xx、0 个慢请求、AnyIO `0/40` 借用、
  6 个 Python 线程；同步成功，Codex Provider 可用。

## 仍然保留的架构演进

当前单实例已经能隔离探活并自动恢复，但 API、同步器和 Codex worker 仍在同一进程。
数据与队列已经持久化，下一阶段可按 Dify 的做法把 `sync-worker`、`agent-worker` 与
`web` 拆成三个 systemd service；拆分前不能简单启动多个 Uvicorn workers，否则每个
worker 都会重复启动 scheduler 和 enrichment。这个边界已明确记录，不用“多 worker”
掩盖资源隔离问题。

## 验收命令

```bash
python3 -m pytest -q
sed -n '/<script>/{:a;n;/<\/script>/q;p;ba}' static/index.html | node --check
systemd-analyze --user verify deploy/*.service
python3 scripts/smoke_load.py --requests 240 --concurrency 12
python3 scripts/smoke_load.py --duration 120 --concurrency 8 --interval-ms 250
./scripts/service_status.sh
```

每次可靠性相关修改必须至少保留：单元回归、真实 HTTP 并发、故障注入、服务重启后
业务接口验证四类证据。仅有“进程还在”或“我刷新了一次页面”不再视为通过。
