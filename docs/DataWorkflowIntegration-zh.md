# 检索结果转交与蒸馏任务接口

本功能只增加后端接口。一个启动操作触发扫描目录、读取 JSON、转换输入、落盘和启动 `data-work-flow` 的完整流程。进度展示独立通过任务列表、详情和日志接口查询；当前不提供前端按钮或进度页面。

不修改检索流程、提示词或 `/query/data` 的响应，也不修改 `data-work-flow` 的 workflow。调用方应先完成原始结果的写入，再提交任务。

## 配置位置

### 桥接配置：`config/data_workflow.yaml`

| 字段 | 配置内容 |
| --- | --- |
| `input.paths` | 待读取的 JSON/JSONL 文件、目录或 glob；目录扫描为一层，按文件名排序，只取 `.json`、`.jsonl` |
| `input.records_path` | 原始接口有额外包装时的字段路径；标准 `/query/data` 保持 `[]`，不要填 `[data]` |
| `workflow_config` | 本次使用的 QA 配方；默认 `data_workflow_job.yaml` |
| `project_directory` | `data-work-flow` 项目目录，默认 `/root/data-work-flow` |
| `python` | 该项目的虚拟环境解释器，保留 `.venv-api/bin/python` 路径，不替换为符号链接的目标 |
| `credentials_file` | 凭据专用 `.env` 文件；默认 `null`，使用继承的进程凭据 |
| `jobs_directory` | 本功能的任务资料目录，默认 `runs/data-workflow`；同一任务生命周期中不要改变此路径 |
| `limits.max_active_jobs` | 每个 workspace 的最大活动任务数，默认 1；每个任务内部仍使用配方中的批次及并发限制 |
| `limits.max_input_bytes` | 一次扫描累计输入字节上限，默认 64 MiB |
| `limits.max_records` | 一次扫描最大记录数，默认 10000 |
| `limits.timeout_seconds` | 实际 CLI 的执行超时，默认 `null`；不包含前面的扫描准备时间 |

桥接配置中所有相对路径以本文件所在目录为基准。例如：

```yaml
input:
  paths:
    - ../incoming/query-results
  records_path: []
```

如果源文件是 `{"payload":{"results":[第一份完整响应,第二份完整响应]}}`，配置 `records_path: [payload, results]`。

配置文件位置可通过进程环境变量 `LIGHTRAG_DATA_WORKFLOW_CONFIG=/绝对路径/bridge.yaml` 覆盖。配置读取是惰性的；未填写输入不会影响原来的 LightRAG 服务启动。

### QA 配方：`config/data_workflow_job.yaml`

这份文件复制自 `/root/data-work-flow/examples/config/review_classifition.yaml`，供独立维护，不修改对方项目。启动前填写：

- `models.generation`：生成及修订模型地址、名称、采样参数、并发等。
- `models.classifier`：分类模型配置。
- `models.judge`：专家评价模型配置。
- `runtime.batch_size`、`runtime.max_inflight_requests`：批次大小及本作业内并发上限。
- `workflow.parameters`：问题数量、场景要求、分类要求、专家、量表及修订参数。
- 各任务的 `prompt_file`：需要替换指令时指定 Markdown；相对路径以这份配方文件所在目录为基准。

当前模型地址及名称为示例占位值，场景要求限定采购审批。这些值需要按实际材料填写；真实执行遇到示例模型会直接失败，不发送请求。

凭据按配方的变量名提供：`QA_API_KEY`、`CLASSIFIER_API_KEY`、`JUDGE_API_KEY`。不将密钥值写入任务配置或 API 返回体。沿用对方 CLI 的规则：已有进程环境变量优先于凭据文件。

每次提交会生成独立的 `job.yaml`，自动替换输入、输出及正文路径映射，保留其他 workflow 参数。不会将原配方的输入文件误用成本次数据。各任务的 `prompt_file` 转为绝对路径，保留配方原来的资源基准。已有任务使用提交时的快照；修改配置后用新的 `request_id` 提交新任务。

## 输入与转换

支持单份完整 `/query/data` JSON、此类对象组成的 JSON 数组，以及每行一份对象的 JSONL。重复匹配到的文件只读取一次。

每份成功响应被投影为以下正文，保留材料顺序和原文内容：

```json
{
  "entities": [{"entity": "费用报销", "type": "PROCESS", "description": "报销流程"}],
  "relationships": [{"entity1": "费用报销", "entity2": "发票", "description": "必须提交"}],
  "text_chunks": ["申请报销时必须提交合法发票。"]
}
```

最终 JSONL 的每行只有 `text`，其值是上述对象的 JSON 字符串。无 `meta`、引用列表、来源文件名、chunk ID 或回答指令。实体、关系投影沿用查询上下文的字段命名；原文来自 `data.chunks[].content`，不做摘要或模板渲染。Naive 可以没有实体、关系；所有模式都至少需要一段非空原文。

单条检索失败、格式错误或缺少原文会记入 `rejected.jsonl`，有效记录继续准备。输入限额、配置、文件读取等任务级失败会阻止启动。没有任何有效材料时任务失败。

输入写完后由 `.part` 发布为固定 `input.jsonl`，启动后不追加。对方现有入口缺少 `meta` 时可能记录警告，这是预期行为。

## 启动按钮的接口

所有路径沿用 LightRAG 的 API 前缀和认证。不会因为白名单而绕过已有认证；客户端不能通过请求选择程序、输入路径或凭据文件。

```http
POST /integrations/data-work-flow/submit
Content-Type: application/json

{"request_id":"由前端生成并保留的本次操作标识","start":true}
```

返回 HTTP 202 和任务对象，不等待扫描完成。`start` 默认为 `true`。按钮只需调用一次，之后交由独立任务展示模块查询。网络重试复用同一个 `request_id`，返回原任务，不重复执行；要有意重新扫描则生成新的标识。

设置 `start:false` 可以只扫描转换、检查落盘结果，停在 `ready`。之后调用 `POST /integrations/data-work-flow/jobs/{job_id}/start` 执行这份固定快照。

## 独立进度模块的接口

| 接口 | 用途 |
| --- | --- |
| `GET /integrations/data-work-flow/config` | 查看当前路径和限额，不返回密钥值 |
| `GET /integrations/data-work-flow/jobs?limit=20&offset=0` | 分页任务列表；新进入进度页面时恢复任务 |
| `GET /integrations/data-work-flow/jobs/{job_id}` | 查询一个任务 |
| `GET /integrations/data-work-flow/jobs/{job_id}/logs?stream=stderr&offset=0&limit=8192` | 按字节游标读取日志；`stream` 可选 `stdout`、`stderr` |

任务对象使用版本化的 `JobResponse`，OpenAPI 包含完整字段类型。示意：

```json
{
  "schema_version": 1,
  "job_id": "distill-…",
  "request_id": "click-…",
  "status": "running",
  "phase": "processing",
  "created_at": "2026-10-22T00:00:00+00:00",
  "updated_at": "2026-10-22T00:00:01+00:00",
  "started_at": "2026-10-22T00:00:01+00:00",
  "finished_at": null,
  "error": null,
  "terminal": false,
  "can_start": false,
  "poll_after_ms": 2000,
  "progress": {
    "unit": "sources",
    "percent": 50,
    "seen_sources": 11,
    "accepted_sources": 10,
    "rejected_sources": 1,
    "processed_sources": 5,
    "committed_batch": 1
  },
  "result": null,
  "artifacts": {"directory":"…","input":"…","config":"…","rejected":"…","stdout":"…","stderr":"…"},
  "links": {"status":"…","start":"…","logs":"…"}
}
```

展示逻辑：

- `status=preparing`：`phase=scanning/converting`，显示正在扫描或整理，以及已读取、有效、拒绝数量。转换完成之前可能未知总数，不显示模拟百分比。
- `status=ready`：只准备完成，`can_start=true`，停止轮询；由使用方决定何时启动。
- `status=starting/running`：按 `poll_after_ms` 轮询。`phase=processing` 表示对方 workflow 正在执行，不声称能观察到内部每次模型调用的阶段。
- `progress.percent` 来自 `run.json` 已提交的 `summary.sources / accepted_sources`；不可观测时为 `null`。它不是 QA 数量、合格率或模型 token 进度。
- 一个大批次执行期间可能长时间停在同一比例，提交批次后才更新。100% 表示来源已提交，仍以终态判断整个运行是否结束。
- `terminal=true`：停止轮询。`completed_with_errors` 要与 `completed` 区分；`failed/interrupted` 显示错误及日志入口。
- `result` 是对方 CLI 的实际完成回执，包含真实 `run_id/directory/status/summary`；`successful_units` 可能按 QA 工作项计数，不等于来源数。
- 日志查询使用返回的 `next_offset` 继续读取，不重新拉取全部文件；`limit` 范围为 4–65536 字节，游标保留完整 UTF-8 字符边界。页面卸载停止本地轮询，不取消后台任务。任务列表可在重新打开页面后恢复。
- `artifacts` 是服务端路径，供后续文件查看功能对接，不是浏览器下载 URL。

不提供自动重跑或取消接口，避免对已部分执行的模型调用进行隐式重试。要重新执行，检查现有产物后以新标识提交。

## 持久化与进程边界

任务按 workspace 隔离。`jobs_directory/<workspace-hash>/jobs.sqlite3` 是任务状态与幂等标识的持久化记录；SQLite 事务控制重复提交与活动任务数量。每个任务有独立目录，保存 `settings.json`、`job.yaml`、`input.jsonl`、`rejected.jsonl`、日志及 `outputs/`。

后台 worker 与 HTTP 请求分离，服务正常重启不主动终止已启动的 worker。任务查询会识别已停止的 worker，标记 `interrupted`；不自动重启，保留不确定产物。此本机适配器使用 Linux 进程身份和进程组，需要 API 服务、worker 和目标项目共享本机文件系统；不作为跨主机调度器。

目标 CLI 的实际退出码、回执和发布清单必须一致才接受完成：0 为 `completed`，1 为 `completed_with_errors`，2 为 `failed`。超时会终止本次目标进程组。对方 writer 或模型的失败不会被报告成成功。

首次部署新路由需重启 LightRAG 服务。后续修改桥接输入和 QA 配方对新提交生效，不会改变已准备或运行的任务。

## 代码边界

- `lightrag/integrations/data_workflow/config.py`：配置与配方快照。
- `inputs.py`：读取输入文件。
- `conversion.py`：纯字段投影。
- `storage.py`：任务持久化、幂等与状态认领。
- `service.py`：启动操作编排。
- `worker.py`：后台扫描、转换和运行。
- `execution.py`：对方 CLI 适配。
- `progress.py`、`contracts.py`：可观测进度和公开返回结构。
- `lightrag/api/routers/data_workflow_routes.py`：API 参数、认证及 HTTP 错误映射。

核心检索、现有 workflow 与前端保持独立；服务器仅注册新增路由。

## 本次验证与待配置项

- 新增集成与路由测试 47 项通过；连同项目文档字符串规范检查共 63 项通过，Ruff 检查通过。
- 使用真实 `/root/data-work-flow` 的 `source_review` CLI 做无模型验证：扫描 4 份响应，隔离 1 份失败记录，导入 3 条正文并完成 3 个已提交批次，进度为 100%，实际运行回执正常。
- 扩大 API 回归时，669 项通过、4 项跳过，停止于 3 项既有测试夹具错误；在修改前的服务器源码中复现同样 3 项失败，涉及 PromptAdmin 的 MagicMock 序列化及登录测试 FakeLightRAG 缺少向量存储属性。
- 尚未执行真实模型蒸馏：原始输入路径、模型及模板参数、凭据待配置。正式执行还要求 `python` 指向能正常加载 QA workflow 的环境。当前默认 `.venv-api` 在 QA 预检查时报告 `dataflow.get_logger` 导入失败，需要修复该目标环境或配置可用解释器；此问题不影响已经验证的无模型文件流转。

验证期间未更改对方项目代码或安装依赖，也未重启当前 LightRAG 服务。
