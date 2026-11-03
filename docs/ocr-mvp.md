# 文档解析（OCR）MVP：使用与部署

把客户技术文件、招标文件、工程量清单上传上来，得到**每个字段都带原文页码和片段**的结构化结果，人工核对后导出 Excel。对应方案 4.2 第 1–4 步、4.3 第 2–3 步。

## 1. 能做什么

| 步骤 | 说明 |
|---|---|
| 上传 | 网页拖拽上传（可多份），或 `POST /v1/documents`。PDF、PNG/JPEG/TIFF、Word（.docx）、Excel（.xlsx），单份 ≤ 50 MB、≤ 300 页（可配） |
| 解析 | 有文字层的 PDF 直接取字；扫描页和图片逐页 OCR（多页并发，进度实时显示）；Word / Excel 直接读。每页记录解析方式 |
| 字段抽取 | 按文件类别选 Schema：**技术文件**（31 个字段：项目、炉型部位、工况、衬里、标准、工程量、商务）、**招标文件**（19 个：编号、限价、截止/开标时间、保证金、资格、评标办法、★▲实质性条款、废标条款…）。每个字段给出值、单位、页码、原文片段、可信度；找不到的标"缺失"，不同页给出不同值的标"冲突" |
| 核对 | 左边原文（识别文本 / 原文版面），右边字段表。点"第 N 页"跳到原文并高亮；可修改、确认、还原、采用其他候选值，每次操作记录人和时间 |
| 导出 | Excel（字段表 + 逐页原文 + 说明，缺失与冲突行标红）或 JSON。**模拟数据不能导出 Excel** |

抽取分两层：

- **规则抽取**（总是运行）：按 Schema 里的"叫法"、取值和正则在原文里找，确定、可解释，不需要任何模型服务。
- **模型抽取**（`FOS_LLM_MODE=litellm` 时自动开启）：DeepSeek / Qwen 读原文补充规则漏掉的写法。模型给的原文片段必须能在那一页逐字找到、值必须出现在片段里，否则丢弃；规则和模型都找到的同一个值可信度更高。模型只读不算（规则 3）。

## 2. 本机试用（5 分钟，不需要任何模型）

```bash
uv venv && uv pip install -e .
export FOS_DATABASE_URL=sqlite+aiosqlite:///var/fos.db    # 本地试用用 SQLite
.venv/bin/fos quickstart          # 建表 + 建管理员 + 打印 API Key（只显示一次）
.venv/bin/fos samples ./samples   # 生成三份样例：技术要求、招标文件、工程量清单
.venv/bin/fos serve               # 打开 http://127.0.0.1:8000/app/ ，用上面的 Key 登录
```

把 `samples/` 里的文件拖进网页，类别分别选"技术文件""招标/询价文件""工程量清单"。Word / Excel 和有文字层的 PDF 在模拟模式下也是真实结果，可以导出；扫描件会显示"模拟数据"横幅。

## 3. 接真实 OCR

三种模式由 `FOS_OCR_MODE` 切换，切换只改环境变量，不改代码，重启生效。

### 3.1 百炼 Qwen-VL-OCR（没有 GPU 时先用这个）

```bash
FOS_OCR_MODE=qwen_vl
FOS_OCR_API_KEY=<百炼 API Key>          # 只放环境变量 / KMS，不进代码和租户配置
# 可选：FOS_OCR_MODEL=qwen-vl-ocr-latest（默认）  FOS_OCR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1（默认）
```

### 3.2 自建 Xiaomi-OCR-0（0.8B，Apache-2.0）

任选一种推理服务，都是 OpenAI 兼容接口：

```bash
# NVIDIA GPU（DGX、阿里云 GPU ECS）：vLLM 或 SGLang
vllm serve SeerRay-Lab/Xiaomi-OCR-0 --served-model-name xiaomi-ocr-0 --port 8001 --max-model-len 16384
# 或者 docker compose --profile gpu up -d（见 deploy/docker-compose.yml 的 ocr 服务）

# Mac Studio：Ollama（社区 GGUF，官方标注为实验性，上线前先用样本核对效果）
ollama pull longwayxu/xiaomi-ocr-0:bf16
```

```bash
FOS_OCR_MODE=xiaomi
FOS_OCR_BASE_URL=http://<推理服务地址>:8001/v1   # Ollama 为 http://<地址>:11434/v1
FOS_OCR_MODEL=xiaomi-ocr-0                         # Ollama 为 longwayxu/xiaomi-ocr-0:bf16
```

提示词和解码参数按官方 demo（关闭思考模式、`top_p=0.8`）；模型输出的 OTSL 表格在这里转成带合并单元格的 HTML 表格。整页模式适合扫描件、手写和不规则版面；官方的版面区域模式（PP-DocLayoutV3）留到后续。

其他参数：`FOS_OCR_CONCURRENCY`（同一份文档并发识别页数，默认 4）、`FOS_OCR_TIMEOUT_S`（默认 120）、`FOS_OCR_RETRIES`（超时 / 5xx / 429 重试次数，默认 2）、`FOS_OCR_RENDER_SCALE`（扫描页渲染倍率，默认 2.0 ≈ 144 dpi）。

OCR 服务暂时不可用时文档记为"失败"并显示原因，修好后在网页上点"重新解析"即可。

### 3.3 开启模型抽取

`FOS_LLM_MODE=litellm`，并按 `deploy/litellm/config.yaml` 配好百炼 / DeepSeek 的 Key。网页右上角显示"模型抽取：开"。

## 4. 部署（阿里云 ECS + Docker Compose）

```bash
cp deploy/.env.example deploy/.env        # 填数据库密码、LiteLLM 主密钥、OCR 模式与 Key
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
docker compose -f deploy/docker-compose.yml exec api fos quickstart --login jayq --display-name JayQ
# 网页：http://<服务器>:8000/app/   接口文档：http://<服务器>:8000/docs
```

正式环境前面加 HTTPS（SLB 或 Nginx）；原件目前存在 `blobs` 卷里，后续换 OSS。给同事开账号：

```bash
docker compose -f deploy/docker-compose.yml exec api fos create-user luneng zhangsan 张三 --roles sales
docker compose -f deploy/docker-compose.yml exec api fos issue-key luneng zhangsan
```

权限：上传 `doc.document:write`，查看与导出 `doc.document:read`，修改 / 确认字段 `doc.field:write`（luneng 的销售、销售负责人、工程技术、管理员有）。

## 5. 维护字段 Schema（不用改代码）

Schema 在 `packs/refractory/extraction/{tech,tender}.yaml`，租户可以在 `tenants/<id>/extraction/` 放同名文件覆盖。拿到真实文件后，最常做的是**给字段补叫法**：

```yaml
- key: normal_temp
  name: 正常工作温度
  group: 工况
  type: temperature          # text / number / temperature / date / money / enum / list
  unit: ℃
  labels: [正常工作温度, 工作温度, 使用温度]   # 越靠前越可信
  exclude_before: [最高, 最低, 外壁]          # 叫法前面紧挨这些词时不算
  required: true             # 缺失时标红
```

改完重启服务，在文档页点"重新抽取"；人工修改和确认过的字段不会被覆盖。

## 6. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/documents?wait=false` | 上传（表单字段 `file`、`kind`=tech/tender/drawing/boq/other），返回 202，后台解析；`wait=true` 解析完再返回 |
| GET | `/v1/documents` | 列表（带缺失、冲突、已确认数） |
| GET | `/v1/documents/{id}` | 状态、进度（`progress`/`page_count`）、每页解析方式 |
| POST | `/v1/documents/{id}/retry` | 失败的文档重新解析 |
| GET | `/v1/documents/{id}/pages/{n}` | 第 n 页识别结果（Markdown，表格为 HTML） |
| GET | `/v1/documents/{id}/pages/{n}/image` | 第 n 页版面预览（PDF / 图片） |
| GET | `/v1/documents/{id}/original` | 下载原件 |
| GET | `/v1/documents/{id}/fields` | 字段（值、单位、来源页码与片段、候选值、修改记录） |
| PATCH | `/v1/documents/{id}/fields/{key}` | `{"action": "correct", "value", "unit", "note"}` / `{"action": "confirm"}` / `{"action": "reset"}` |
| POST | `/v1/documents/{id}/extract` | 重新抽取，可带 `{"schema_id": "tender"}` 换 Schema |
| GET | `/v1/documents/{id}/export?format=xlsx\|json` | 导出 |
| GET | `/v1/extraction/schemas` | 本租户可用的 Schema |

## 7. 已知限制与下一步

- 高亮定位到识别文本里的原文片段，不在原图上画框（需要版面区域模式的坐标）。
- 规则抽取对"叫法 + 值"写法最准；大段叙述里的工况要靠模型抽取补充。上线前用 10–20 份真实脱敏文件跑一遍，按漏抽的写法补叫法。
- 后台任务在 API 进程内运行，适合单机；并发量大时换成独立 Worker 进程。
- IM 收到的文件目前只解析、不抽字段。
- 下一步：字段确认后生成工程项目（经 `Ledger.append()` 记账）→ 知识库匹配 → 方案 / BOM / 报价（方案 4.2 第 5–8 步）；招标需求矩阵（4.3 第 3–6 步）。
