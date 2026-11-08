# 工程方案报价（W10–W12，里程碑 M2）

对应开发方案第 4 章应用 A：客户技术文件 → 工况 → 选材 → 传热与用量 → 方案 / BOM / 报价 → 审核 → 发布。

## 一条完整的流程

```
文档解析页：上传技术要求 → 核对字段（每个字段带页码原文）
        │  关键工况（温度、气氛、外壁温度、面积、厚度）必须人工确认或改正
        ▼
POST /v1/eng/projects            eng.project_created 事件（证据 = 每个字段的页码与原文）
        ▼
POST /v1/eng/projects/{id}/match 衬里模板 + 硬约束 → 方案 A/B/C（+ 按客户文件写明的“客户方案”对照）
        ▼
PATCH /v1/eng/designs/{id}       改厚度 / 换产品 / 改折扣运费 → 全部重算，生成新版本
PATCH .../review/{条目}          审核清单逐条标 已处理 / 接受风险 / 不适用
        ▼
POST /v1/eng/designs/{id}/publish eng.quote_issued 事件 → 命中 policy.yaml → 审批单（销售负责人，低毛利加总经理）
        ▼                         审批通过后以申请人身份入账，项目状态 quoted
GET  /v1/eng/designs/{id}/export?format=xlsx|docx|json
```

网页：`/app/eng.html`（和文档解析页共用 API Key）。

## 谁算数字

全部是 `fos/engineering/` 里的确定性代码（CLAUDE.md 规则 3），结果和公式一起保存、一起展示：

| 计算 | 方法 |
|---|---|
| 传热 `heat.py` | 多层平壁 / 圆筒一维稳态；k(T) = a + b·T 逐层积分求精确解；外表面自然对流 C·ΔT^(1/3) + 辐射 εσ(Ts⁴−Ta⁴)；对热流二分求平衡。正常工况算外壁温度，最高温度工况算各层热面温度 |
| 硬约束 `design.py` | 工作层分类温度 ≥ 最高温度 + 余量（默认 100℃）；后续各层分类温度 ≥ 热面温度 + 余量（默认 50℃）；外壁温度 ≤ 要求；文件写了“不小于 / 不大于”的总厚度；气氛相容 |
| 用量 | 面积 × 厚度 × (1 + 损耗率)，按计价单位换算成 t / m³ / 块（单件体积）；砖砌体按 0.12 t/m³ 配泥浆 |
| 报价 | Σ 数量 × 单价（每行记价格对象和版本）→ 折扣 → 运费（总重 × 元/t）→ 施工费（面积 × 元/m²）→ 税；材料毛利率 = (材料净价 − Σ 数量 × 成本价) / 材料净价 |
| 匹配 `match.py` | 按炉型 / 部位 / 温度选模板，每层取成本最低 3 个 + 导热最低 2 个候选，枚举模板里的可选厚度；满足全部硬约束的组合里挑“成本最低 / 衬里最薄 / 热损失最小” |

模型目前不参与：选材理由、技术方案正文由模板拼装。以后接 DeepSeek 润色时只改文字，不改数字。

## 知识库

| 内容 | 放在哪 | 怎么改 |
|---|---|---|
| 产品热工参数 | 账本对象 `product`（行业包追加 `tc_a`、`tc_b`、`piece_volume`、`loss_rate`、`aliases`、`unsuitable_atmospheres`） | `kb.product_registered` / `kb.product_specified` 事件（`kb.product:write`） |
| 价格 | 账本对象 `price_item`（单价、计价单位、成本价、客户类别、生效 / 有效期） | `kb.price_set` / `kb.price_changed`（`kb.price:write`）；版本 = 对象版本号，报价里记 `price_item/<id>@v<版本>` |
| 衬里模板与选材参数 | `packs/refractory/engineering/linings.yaml`；租户可用 `tenants/<id>/engineering/linings.yaml` 整体替换 | 资深工程师改 YAML，启动时对照本体校验 |

本体拆在 `packs/refractory/ontology/engineering.yaml`（行业包可以有多个本体文件，按文件名顺序加载）。

`aliases` 用来对上客户文件里的叫法：文件写“JM-26 莫来石轻质砖 230 mm”，知识库 LN-ML26 的别名有 JM-26，就能组出“客户方案”做对照校核。

## 方案草稿与版本

方案是 L1 草稿，存在 `eng_designs` 表（迁移 0008），不是业务事实：
- 每次修改生成新版本（`parent_id` 指向上一版，旧版标 `superseded`），所有版本保留；
- 工况改了（`eng.project_updated`）之后，已有方案标“工况已变”，发布前必须重算；
- 审核条目的处理结果在重算后按条目保留（同一问题不用重复处理）。

## 审核清单

| 类型 | 例子 | 能否标记放过 |
|---|---|---|
| 缺失 | 文件没给最高温度 / 气氛 / 外壁温度 / 执行标准；按默认值做的假设 | 可以 |
| 冲突 | 字段在不同页给了不同的值（带页码） | 可以 |
| 未核对 | 必填字段还是自动识别结果 | 可以 |
| 选材 / 传热 | 余量不足、外壁超温、厚度不符、气氛不适用 | 可以（接受风险） |
| 商务 | 价格过期、低毛利、只供料不含施工 | 可以 |
| 必须修正 | 模拟数据、缺价格、缺热工参数、缺成本价、低于成本 | 不能，修正数据后重算才会消失 |

全部处理完才能发布。

## 审批（tenants/luneng/policy.yaml，阈值为占位值，待试点确认）

- 工程报价一律要销售负责人确认（方案 4.2 第 8 步）；
- 材料毛利率 < 15% 再加总经理审批；
- 毛利率 < 0：红线，直接拒绝。

## 模拟数据

`fos eng-demo` 把 `packs/refractory/engineering/sample_kb.yaml`（示例牌号与价格）以模拟数据写入。
用模拟知识库或模拟 OCR 结果算出的方案标 `data_mode=mock`：可以演示匹配、改厚度、审核，不能发布报价、不能导出 Excel / Word。

本地试用：

```bash
export FOS_DATABASE_URL=sqlite+aiosqlite:///var/fos.db
.venv/bin/fos quickstart && .venv/bin/fos eng-demo && .venv/bin/fos samples ./samples && .venv/bin/fos serve
# /app/ 上传 samples/样例-技术要求-2#辊道窑.docx，确认关键字段 → /app/eng.html 建项目、匹配方案
```

## 还没做（后续 PR）

- 真实知识库：六安绿能的牌号、导热系数、价格、成本价需要现场收集；`linings.yaml` 的模板和余量需要资深工程师确认；
- 知识库 Excel 批量导入、检测报告 OCR 提议更新参数（L2 卡片）；
- 模型润色技术方案正文、DeepSeek-R1 解释权衡；
- 圆筒（回转窑）在匹配里的使用：`heat.py` 已支持，工况里还没有内半径字段；
- IM 侧：发文件给工厂助手直接建项目、报价审批推送已有（审批卡片），项目摘要回复还没做；
- 应用 B 招标拆解：已做（W18，见 `docs/tender.md`），招标文件的技术规格可以直接转成工程项目。
