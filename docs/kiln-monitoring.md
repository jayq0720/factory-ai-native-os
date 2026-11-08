# 窑炉监控（W19，里程碑 M4 的第一段）

对应开发方案 3.2 “窑炉与设备接入：三条路径”、2.4.6 Watchdog 和 10.6 验收指标“窑炉”：

- 三条接入路径（A 直连仪表 / PLC、B 旁路加装采集、C 人工抄表或拍照读数）的读数进**同一张时序表** `point_readings`，只是 `source` 和 `quality` 不同；
- 测点是本体对象（`monitor_point`，主数据，可用 Excel“测点”表导入），fos-edge 按 `crates/fos-protocol` 的 `UplinkBatch` 上行，云端按 `(edge_id, seq)` 去重，断网补传、重发都不会重复入库；
- 在烧窑次的温度点和**烧成制度**比对，超差、超上下限、网关断网、设备故障都由代码判定（零模型成本），推到班组长 IM；
- 不能联网的点位按间隔**提醒抄表**，工人回复“抄表 测点 数值”即可；
- 结束窑次时，**最高温度、超差时长、耗电 / 耗气由读数算出**，和“窑次结束”一起以操作人身份入账，证据指向读数区间。

读数是高频时序数据，不是业务事实，不走账本（规则 5 说的是业务事实）；告警是从读数派生的运行状态，也不走账本。进账本的是测点主数据和窑次结果。数字都由确定性代码算（规则 3）。

这里是云端部分；厂端 fos-edge v0.1（Modbus 采集、本地缓存 ≥ 7 天、签名的只读命令、仪表模拟器）见 `docs/fos-edge.md`。

## 一条完整的流程

```
主数据：窑炉 production.kiln_registered + production.kiln_curve_set（默认烧成制度）
        测点 production.point_registered（编号 = fos-edge 点位 id；类型、单位、接入路径、采集网关、间隔、上下限、是否参与曲线比对）
        配方 rnd.recipe_released 的 firing_curve（烧成制度编号）
                 │
production.kiln_run_started          班组长开窑（录单或 /v1/events）
POST /v1/edge/uplink                 fos-edge 上行一批读数（API Key 的用户角色 edge，只有 production.reading:write）
POST /v1/kiln/readings               路径 C：人工抄表 / 照片读数（数值由抄表人确认）；IM 里发“抄表 测点 数值”
        │  `fos channels run` 里每 30 秒判定一轮（fos/monitoring/watch.py）
        ├─ 超上 / 下限持续 1 分钟                → 告警 high / low
        ├─ 偏离烧成制度超过允许偏差持续 5 分钟  → 告警 deviation
        ├─ 网关 5 分钟没上行                     → 告警 edge_offline（一台网关一条，不逐个点位报故障）
        ├─ 网关在线、点位 3 个间隔没读数 / 质量差 → 告警 point_fault
        └─ 抄表点位到点 15 分钟没报数            → 抄表提醒（每次到点只提醒一回）
GET  /v1/kiln/board                  窑况：在烧窑次、各测点最新读数、目标温度、偏差、未关闭告警
GET  /v1/kiln/runs/{id}/curve        窑次曲线：实测 + 目标曲线 + 允许偏差带 + 超差区间 + 能耗
POST /v1/kiln/runs/{id}/finish       结束窑次：production.kiln_run_finished（结束时间、最高温度、是否偏差）
                                     + production.kiln_run_summarized（烧成制度、超差分钟、耗电 kWh、耗气 m³）
GET  /v1/kiln/energy                 区间能耗（按窑炉；没挂窑炉的表算“全厂 / 公用”）
GET  /v1/kiln/alarms                 告警（open / closed / all）；POST .../{id}/ack “已知晓”停止重复提醒
```

## 烧成制度怎么选

窑次上已定（`firing_curve`）→ 窑次里批次配方的烧成制度（多数）→ 窑炉默认烧成制度 → 都没有就只看上下限。制度定义在 `packs/refractory/monitoring.yaml` 的 `curves`：从装窑开始的小时数 → 目标温度，中间线性插值，`tolerance` 是允许偏差（±℃）。**现在的两条制度和偏差都是占位值**，上线前按工艺卡改；租户可以在 `tenants/<id>/monitoring.yaml` 整段覆盖，不用改代码。

一窑可以有多个参与比对的温度点（`curve_role`），没标的话该窑所有温度点都参与。

## 告警与推送

- 同一 key（类型 + 测点 / 网关）同时只有一条未关闭的告警；条件消失自动关闭；
- 新告警立即推给 `notify.alarm_roles`（默认班组长），未“已知晓”的每 `repeat_minutes`（默认 60）再推；
- 抄表提醒推给 `notify.manual_roles`（默认班组长、工人），Trigger 唯一约束保证每次到点只推一回；
- 先提交再发送（规则 6），推送只读不写账本；
- 只有窑炉在烧时才提醒抄窑温；电表 / 燃气表、不挂窑炉的点位一直按间隔提醒。

判定时长、断网时长、推送角色都在 `monitoring.yaml` 的 `alarms`、`notify` 里（占位值）。

## IM 指令（Fast Path，零模型成本）

| 发什么 | 回什么 | 权限 |
|---|---|---|
| 窑况 | 所有窑炉：在烧窑次、最高温度点的读数和偏差、告警数 | production.kiln:read |
| 窑况 1号梭式窑 | 这座窑每个测点的最新读数、目标、偏差 | production.kiln:read |
| 抄表 k1.panel 1120 | 记录读数（测点编号或名称都认）；超上下限时提示 | production.reading:write |

“抄表”是报数人本人输入的数值，就是本人确认，不再出确认卡片；照片读数（Qwen-VL / OCR）要先在录单卡片里确认数值后再走 `POST /v1/kiln/readings`。

## 网页

`/app/kiln.html`：窑况卡片（自动刷新）、告警“已知晓”、窑次曲线（SVG，红色区间 = 超差）、结束窑次并入账、人工抄表、24 小时能耗。“加载演示数据”写入一座演示梭式窑、4 个测点、一个在烧窑次和 30 小时模拟读数（含一段超差），全部 `data_mode=mock`。

## 权限（luneng）

- `edge` 角色：只有 `production.reading:write`，给 fos-edge 用（一台网关一个用户、一把 API Key）；
- 工人：多了 `production.reading:write`（抄表）；
- 班组长：`production.*:write`，可以登记测点、结束窑次、确认告警。

## 数据表（迁移 0010）

| 表 | 内容 |
|---|---|
| point_readings | 读数：测点、时间、值、单位、来源、质量、置信度、证据（照片对象键）、网关、抄表人、data_mode |
| edge_batches | 上行批次：(tenant, edge_id, seq) 唯一，兼作心跳 |
| kiln_alarms | 告警：类型、测点 / 窑 / 窑次 / 网关、开始 / 最后出现 / 关闭时间、推送时间、已知晓 |

## 待现场确认（占位值）

- 烧成制度曲线和允许偏差（按产品工艺卡）；
- 超差 / 超限持续多久才告警，告警推给谁（班组长？生产负责人？）；
- 每座窑有哪些测点、走哪条接入路径、抄表间隔（W1 现场调研结果整理成“测点”表导入）；
- 电表 / 燃气表是否已有带通讯的表。

## 下一步

- ~~fos-edge v0.1~~ 已完成（`docs/fos-edge.md`）；云端签发命令、存厂端状态；
- 摄像头定时拍仪表盘 → 视觉读数（路径 C ①）；
- 质量体系、另外两种 IM（W20–W22）。
