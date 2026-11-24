# 精密铸造与热处理行业包（`packs/foundry`）

继耐火材料（`packs/refractory`）与工业陶瓷（`packs/ceramics`）之后的第三个内置行业包，验证 Factory AI Native OS 零代码跨行业扩展至中频感应电炉熔炼、砂型/熔模精铸与热处理场景。

## 行业专属扩展

- **本体扩展（`ontology.yaml`）**：新增 `furnace`（熔炼/热处理炉）、`heat_lot`（熔炼炉次）、`mold_pattern`（铸造模具），并为 `product`、`production_batch`、`inspection` 扩展合金牌号、浇注工艺、抗拉强度（MPa）、布氏硬度（HBW）、球化率（%）与金相评级属性。
- **EHS 限值（`ehs.yaml`）**：针对抛丸清理与熔炼烟气收紧粉尘限值至 `12.0 mg/m³`、二氧化硫 `35.0 mg/m³`、氮氧化物 `100.0 mg/m³`。
- **岗位 Skill**：`foundry_metallurgist`（铸造熔炼与金相工艺师）、`ehs_officer`（铸造安环专员）。
- **黄金评测集（`eval/golden_suite.yaml`）**：12 条零模型成本确定性回归用例。
