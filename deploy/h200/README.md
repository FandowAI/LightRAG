# H200 部署与资料包交付

本目录提供 Linux 源码部署配置；此次代码整合不自动操作 H200。模型仍使用 API，LightRAG 本身无需占用 GPU。向量模型服务若部署在同一服务器，直接连接服务器本机端口，无需 Windows 隧道。

## 1. 准备目录与依赖

服务器需已有 Python 3.12、uv、Bun，以及可连接的 Neo4j、Milvus、Redis。Redis 必须开启持久化；数据库使用持久卷并独立备份。不要把本地 JSON/NetworkX 文件目录直接当作生产数据库，也不要对现有数据库执行清空。

建议由管理员准备：

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin lightrag
sudo install -d -o lightrag -g lightrag /opt/fando /data/fando/lightrag
sudo install -d -o lightrag -g lightrag /data/fando/lightrag/{inputs,rag_storage,exports,logs}
sudo -u lightrag git clone --branch feat/enterprise-knowledge-migration https://github.com/FandowAI/LightRAG.git /opt/fando/lightrag
cd /opt/fando/lightrag
```

若账户、目录或仓库已经存在，沿用并核对其属主。固定部署经验证的 commit，不直接跟随移动的分支头。

以有目录写权限的部署账户构建：

```bash
uv sync --frozen --extra api --extra offline-storage
cd lightrag_webui
bun install --frozen-lockfile
bun test
bun run node_modules/typescript/bin/tsc --noEmit
bun run lint
bun run build
cd ..
```

前端构建输出由原服务提供，不另起工作台。`.venv` 和构建产物需允许 `lightrag` 账户读取、执行；`uv` 安装的依赖缓存也须对服务账户可读。可直接以该账户构建以避免属主问题。

## 2. 填写配置并启动

```bash
sudo install -m 600 deploy/h200/production.env.example /etc/fando-lightrag.env
sudoedit /etc/fando-lightrag.env
sudo install -m 644 deploy/h200/fando-lightrag.service /etc/systemd/system/fando-lightrag.service
sudo systemctl daemon-reload
sudo systemctl enable --now fando-lightrag
sudo systemctl status fando-lightrag
sudo journalctl -u fando-lightrag -n 100 --no-pager
```

启动前替换全部 `REPLACE_*` 配置，核对数据库地址、工作区、模型名、向量维度及鉴权。样例端口 9622 是待核实的向量服务地址，不会创建或启动模型服务。维度必须与向量模型实际输出一致；更换模型/维度应使用新集合和新工作区，不覆盖现有索引。

后台保持单实例、单 worker；SQLite 任务账本和身份索引使用本机持久磁盘。水平扩容前需把任务执行与账本迁移到集中式队列/数据库。生产鉴权沿用原项目，批次为当前工作区共享资源，并非按用户独立授权。

新批次直接执行“召回 → 包内及跨包去重 → 保存”，不做大模型相关性筛选，也没有原文待复核。仅极高相似的疑似重复才调用模型确认，整批最多 2 次（`LIGHTRAG_DEDUP_MAX_LLM_CHECKS` 可设为 0 禁用）；无法确认则两份都保留。

首次验收用少量已授权资料：上传文件夹 → 确认文档处理完成 → 批量检索 → 选择少量中心 → 开始。默认深度 2、向量候选 10。图遍历复用同事的算法；普通测试接口保持 100 实体 / 200 关系上限。批量任务单独使用 `LIGHTRAG_BATCH_MAX_GRAPH_NODES=10000`、`LIGHTRAG_BATCH_MAX_GRAPH_RELATIONS=20000`，也可下调。资料包触及上限会明确失败，应降低深度或评估后调整批量上限，避免把截断结果误当成完整资料。

## 3. 资料包实际保存位置

配置样例 `WORKSPACE=enterprise` 时：

```text
/data/fando/lightrag/exports/enterprise/
├── jobs.sqlite3
└── <batch_id>/
    ├── packages.jsonl          # 每行一个中心资料包
    ├── sources.jsonl           # 本批次完整原文，每个 chunk_id 只存一次
    ├── manifest.json           # 文件行数、SHA-256、格式版本；交付完成标记
    ├── entity-0000.json        # 各中心结果与去重引用
    ├── candidate-0000.json     # 召回快照
    └── candidate-0001.json     # 后续中心的召回快照
```

接口返回的 `export_path` 是服务器上的绝对路径，页面也会展示。修改 `LIGHTRAG_BATCH_OUTPUT_DIR` 可调整目录；不要把目录放到 Git 工作树或临时目录。交接整个批次目录中的两个 JSONL 和 `manifest.json`，不能只拿走资料包而丢弃来源文件。

`packages.jsonl` 保留原蒸馏输入的 `text / metadata / created_at / updated_at`，另有明确结构：

| 字段 | 内容 |
| --- | --- |
| `schema_version` | `enterprise-evidence/1.0` |
| `batch_id / package_id / central_entity` | 批次、资料包、中心实体 |
| `entities` | 实体名、类型、描述、图路径和来源块 ID |
| `relationships` | 两端实体、关系描述、关键词、权重和来源块 ID |
| `original_texts` | 召回原文、来源及去重引用；原样保留条件、否定和决策依据 |
| `shared_evidence` | 跨包重复内容的所有者引用，不重复塞入原文 |
| `source_archive` | 同目录下的 `sources.jsonl` |

关系和实体描述是图谱索引，不能直接当作事实标签。关系引用的是完整原文块；核验关系时须读 `sources.jsonl` 的完整原文。没有可证实关系的资料包允许 `relationships=[]`，禁止凭空补关系。

`sources.jsonl` 每行包含 `chunk_id / text / content_sha256 / provenance`。导出前验证片段字符区间可以还原原文、关系端点存在、来源块可找到。完整来源按块集中存储，避免多个包反复复制。不同块之间仍可能有原始切块重叠；实际蒸馏输入使用已经去重的原文。

消费者只处理已有 `manifest.json` 且两份文件哈希和行数匹配的批次。`completed_with_errors` 只说明成功部分已交付，技术性检索失败仍留在记录中；无内容审核或待复核环节，召回原文经去重后直接进入资料包。停止或进程中断不自动重发付费模型请求。

## 4. 对接蒸馏系统

原有测试检索及同事的 `/query/data`、`/query/data/batch` 接口保留。新增批次接口见 [BatchRetrieval.md](../../docs/design/BatchRetrieval.md)。

页面“批量进入蒸馏系统”调用 `/integrations/data-work-flow/submit` 并传入批次 ID。后端自动定位本 workspace 的 `packages.jsonl`，规范实体、关系和原文后交给本地 workflow；独立模块展示实际蒸馏进度。配置 `config/data_workflow.yaml` 和 `config/data_workflow_job.yaml`，批次模式无需填写 `input.paths`。详见 [蒸馏对接配置](../../docs/DataWorkflowIntegration-zh.md)。

当前链路不再向外部 HTTP 接收端转交。统一服务按 workspace 和 `batch:<batch_id>` 持久化幂等关联，启动目标项目的独立 CLI，并通过任务状态及实际完成回执确认结果。API、worker 和目标项目需要共享本机文件系统。大资料包由工作流按上下文容量调度，LightRAG 不静默截断。

## 5. 备份与更新

同时备份 Redis、Neo4j、Milvus，以及 `/data/fando/lightrag` 下的输入、任务账本、资料包、提示词覆盖配置和身份索引。它们没有跨数据库事务；还原时应使用同一维护窗口的快照。

更新前停止服务并备份，检出固定 commit 后重新执行锁文件安装与前端构建，再启动服务。中断的批次保留快照，显式恢复或重建；不自动重新跑全量资料。回退代码不会撤回已提交给蒸馏系统的数据。
