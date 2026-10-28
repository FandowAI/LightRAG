# 从部署服务器目录导入文档

文档管理的上传弹窗提供并排的“选择本地文件夹”和“选择服务器路径”按钮。前者打开电脑的目录选择器，后者打开独立的“从服务器目录导入”弹窗。用户可填写绝对路径，或从已开放的根目录浏览子目录，预览后将文件送入现有入库队列。服务器路径指向运行 LightRAG 后端的服务器；容器部署时指向容器内已挂载路径。

## 配置与启用

在 LightRAG `.env` 中配置可导入的根目录，值为 JSON 字符串数组：

```dotenv
LIGHTRAG_SERVER_IMPORT_ROOTS='["/data/company-documents", "/data/imports"]'
# Optional preview limits; add these lines if the defaults are too small.
LIGHTRAG_SERVER_IMPORT_MAX_FILES=10000
LIGHTRAG_SERVER_IMPORT_MAX_ENTRIES=50000
```

替换为实际存在、服务进程有读取权限的目录。未配置根目录时默认为空数组，前端会说明尚未开放目录。目录不能经过符号链接，请使用实际路径。两个可选数量字段未配置时分别为 5000 和 20000，需要时在 `.env` 新增；允许范围分别为 1–100000、1–1000000。修改 `.env` 后重启对应服务，页面中的“重新读取配置”不会让进程重新加载 `.env`。

修改后重启对应后端；部署新界面时构建前端：

```bash
cd /root/MasterRAG/LightRAG/lightrag_webui
bun install --frozen-lockfile
bun run build
cd /root/MasterRAG/LightRAG
bash scripts/restart_lightrag.sh
```

## 使用步骤

1. 进入 `/webui/` 的文档管理页面，点击上传。
2. 点击“选择服务器路径”，在打开的弹窗中选择根目录或填写其范围内的目录路径。
3. 点击“浏览目录”进入子目录；可以使用“上一级”返回，但不能超出配置范围。
4. 选择“文件类型”：所有支持类型、常用文档（MD/TXT/PDF/DOCX）、仅 Markdown；选择是否“包含子目录”，点击“预览文件”。
5. 确认预览后点击“导入这些文件”。所有有效文件按顺序提交，显示待提交、已提交、已存在、失败及跳过数量。
6. 需要暂停时点击“停止后续导入”，当前文件完成后停止，尚未提交的文件保留。可再次导入剩余文件或重试失败文件。
7. 在文档列表和原有流水线进度中查看解析、抽取与入库结果。

提交结束后，“导入这些文件”右侧显示红色小字“已成功添加 N 个文件，正在后台处理”。N 只统计服务器已接收的文件，不包含已存在或失败项；部分失败时仍需查看失败数量。它不是最终解析或入库成功通知。

未导入或当前提交完成后，可点击“返回上传”关闭服务器弹窗，回到原上传界面；在同一次上传弹窗会话内重新打开时保留路径及预览。导入提交期间不能关闭服务器弹窗，可先使用“停止后续导入”，等当前文件完成后返回。

已提交只表示已经进入处理队列，不能当作解析或入库成功。预览列表最多展示前 80 个文件，但导入包含本次预览的全部有效文件。刷新或关闭页面不会自动恢复前端的逐文件提交队列；已接收的文档仍按现有后台流程处理。再次预览导入时，已存在记录按原上传规则拒绝，不覆盖。

源目录只读取，原文件保留。后端先生成一致的临时磁盘副本，再复用 `/documents/upload` 的实现，将副本写到当前 workspace 的输入区。后续归档、解析及删除针对输入区副本，不移动或删除原目录文件。

## 格式、限制与路径规则

- 使用后端当前解析规则的实时格式支持判断，包含合法的文件名解析提示。
- 可选递归；跳过隐藏项目、`__parsed__` 目录、符号链接及非普通文件。
- 单文件沿用 `MAX_UPLOAD_SIZE`，预览时跳过超限文件，实际复制时再次检查。
- 每次预览默认最多检查 20000 个目录项目、接受 5000 个匹配文件、递归 64 层；数量可用上述环境变量调整。文件类型筛选在匹配文件计数之前执行，但所有遍历项目仍计入扫描上限。超限明确失败，不将部分预览默认为全目录。
- 浏览单层最多返回 1000 个子目录，目录过大时可直接填写更具体的路径。
- 文件在预览后或复制中变化会被拒绝，需重新预览。
- 不同相对路径下的同名文件沿用文件夹上传的路径哈希命名，保留来源路径映射。根目录身份也参与命名，同一根目录下换选子目录不会改变同一文件的导入身份。

导入接口继续执行认证，且不应用公共路径白名单来绕过认证。根目录由服务端配置，客户端不能扩大可读取范围。每个目录分量通过目录描述符和不跟随符号链接的打开方式校验，避免路径遍历和预览后替换父目录造成越界读取。

## 后端接口

沿用 LightRAG API 前缀和认证：

| 接口 | 请求 / 返回 |
| --- | --- |
| `GET /documents/server-import/config` | 返回 enabled、roots、configuration_variable、limits.max_files/max_entries |
| `POST /documents/server-import/browse` | 请求 path；返回 path、parent、directories |
| `POST /documents/server-import/preview` | 请求 path、recursive（默认 true）、可选 extensions（如 `[".md"]`）；返回 files、skipped、excluded（最多 80 项） |
| `POST /documents/server-import/file` | 请求单文件 path、version；返回原上传的 status、message、track_id |

预览的 files 每项包含 `path`、`relative_path`、`size`、`version`。version 是文件设备、inode、大小、纳秒 mtime 的摘要字符串，不是文件内容 SHA-256，也不需要浏览器自行计算。提交时使用预览返回的原值；不会通过浏览器传递文件正文。

文件数或扫描数超限时返回 HTTP 422，detail 包含 code、limit、observed、configuration_variable。observed 是中止时已观察的数量，不代表目录总数。界面显示对应的中文提示，可重新预览或一键“仅预览当前目录”；浏览失败提供重新浏览，只有配置读取失败才提供重新读取配置。

文件导入成功继续使用原上传的准入限制、同名拒绝、解析器检查、任务移交和 workspace 隔离。原文内容去重仍在解析后的原流程执行；来源文件数不等于最终可检索文档数。

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `lightrag/api/server_files.py` | 允许范围、目录描述符、浏览、预览、版本检查和临时副本 |
| `lightrag/api/routers/server_import_routes.py` | 认证、参数与复用上传回调 |
| `lightrag/api/routers/document_routes.py` | 注册新路由并注入原上传入口 |
| `lightrag_webui/src/api/serverImport.ts` | 前端 HTTP 客户端与类型 |
| `lightrag_webui/src/components/documents/ServerDirectoryImport.tsx` | 独立目录选择、预览和提交组件 |
| `lightrag_webui/src/components/documents/UploadDocumentsDialog.tsx` | 上传弹窗接入 |

模型调用和正式文档处理不属于该功能的离线测试；验证使用临时源目录和模拟入库回调，另验证原上传回调仍负责准入与后台任务移交。
