# 质量体系（W22）

面向隔热耐火材料工厂的 AI 原生质量体系：所有业务事实经事件账本留痕，理化判定与 KPI 统计全部由确定性代码计算（不交由模型），处置与让步放行受 Policy 审批约束。

## 核心能力

1. **待检任务队列**（`GET /v1/quality/pending`）
   - 直接从账本投影派生：待检来料批次（`material_lot.status == pending_inspection`）+ 已完工/入库但尚未出厂检验的生产批次（`production_batch`），无冗余任务表。
2. **理化检验确定性判定与自动 NCR**（`POST /v1/quality/inspections`）
   - 对照 `packs/refractory/quality.yaml`（及产品台账的标称体积密度容差）逐项判定体积密度、常温耐压强度、导热系数、加热永久线变化、Al₂O₃ / Fe₂O₃ 含量。
   - 指标超标时拒绝强行记为合格；判定不合格自动创建 `ncr` 对象（`quality.ncr_opened`）并为 `qc` 角色生成处置待办。
3. **NCR 与 8D 整改闭环**
   - `GET /v1/quality/ncrs/{id}/8d-draft`：调用批次追溯图（`trace_report`）自动汇总上游原料、窑次超差等 D1–D4 证据线索（L1 草稿）。
   - `POST /v1/quality/ncrs/{id}/disposition`：判定返工 / 报废 / 退供 / 让步放行；**让步放行（`concession`）命中 `policy.yaml` 需总经理审批（返回 202）**。
   - `PATCH /v1/quality/ncrs/{id}/8d` & `POST /v1/quality/ncrs/{id}/close`：账本守卫 `NcrGuard` 强制校验——未处置不能关闭，重大/严重不合格品须完成 8D（含根本原因 D4 与纠正措施 D5/D6）方可关闭。
4. **批次质保书（CoA）**（`GET /v1/quality/batches/{batch_id}/certificate[/export?format=docx|xlsx]`）
   - 仅允许真实数据（`data_mode == "real"`）且最新出厂检验合格的批次签发；导出 Word / Excel 均经 `fos/excel_safe.py` 防公式注入与非法 XML 字符清洗。
5. **计量器具校准台账与到期提醒**
   - `POST /v1/quality/instruments` / `POST /v1/quality/instruments/{id}/calibrate`（`CalibrationGuard` 校验下次到期日必须晚于本次校准日期）。
   - `POST /v1/quality/instruments/remind`：为即将到期或已超期器具生成去重待办。
6. **ISO 9001 审核证据包**（`GET /v1/quality/iso-audit[/export]`）
   - 按日期区间确定性汇总来料/出厂合格率、NCR 关闭率、8D 完成数、按期校准率及账本事件序号索引，一键导出 4 表 Excel。
7. **IM Fast Path 与质检员 Skill**
   - IM 发送「待检」「不合格」「校准」「质保书 <批号>」走零模型 Fast Path；复杂问答由 `quality_inspector` Skill 调用只读工具回答。
   - 网页控制台：`/app/quality.html`。
