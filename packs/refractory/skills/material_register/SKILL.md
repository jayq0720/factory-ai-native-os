# 新增物料

1. 从任务里整理：名称（必填）、规格、类别（raw 原料 / auxiliary 辅料 / packaging 包材 / spare_part 备件，必填）、
   计量单位（必填）、安全库存。缺必填项就问用户，不要猜。
2. 用 objects.list（where name=名称）查是否已有同名物料；已有就告诉用户，不重复登记。
3. 用 ledger.propose 提议 warehouse.material_defined，title 写“新增物料：名称”。
   evidence 用 {"kind": "im_message", "ref": "当前消息"}。
4. 回复“已整理好，请确认卡片”，不要说“已登记”——确认之前没有入账。
