# M2c 本地真实角色履约：进行记录

本任务在已审查的只读追踪及本地CNY会计准备上，使用原M1普通角色执行一条可重复核对的虚构业务链路。
业务动作通过原生ORM执行，HTTP与浏览器页面核对属于随后独立验收，不将原生测试写成已完成人工UAT。
目标链路为报价100件/折扣15%→经理审批→采购60→收30交70并开票5950→再收30交30并开票2550→退10贷850。
最终预期库存10、毛交付100、净交付90、实际商品分录净过账7650 CNY；预期值在通过前不作实际结果引用。

## 测试先行

新增14项 `TestLocalFulfillment`，夹具先执行真实M1验收准备和真实最小CNY会计准备、显式启用A，再断言流程API缺失。
主会话RED为 **14 failed，0 errors / 14 tests**：`2026-10-31 07:40:21 UTC`，14.44秒、32,323次查询；
`.runtime/m2c-local-flow-red.log`。纯命令行8项测试RED均为缺少显式流程命令。

初版原生专项为 **0 failed，7 errors / 14 tests**：`2026-10-31 07:46:49 UTC`，20.88秒、41,657次查询；
`.runtime/m2c-local-flow-focused-green.log`。其中6项同因：原生分批完成库存移动时写内部日志，真实普通操作者没有发件邮箱而被拒绝；
另1项为B角色的分组检查继承了A活动公司上下文，冷缓存读取时触发原生公司权限拒绝。

## 显式本地发件身份准备

采用独立 `prepare_local_fulfillment_senders` API及 `--prepare-senders` 命令步骤，为六个已标记的A角色补齐缺失邮箱，
使用 `portfolio.acceptance.<role>@portfolio.invalid` 保留域名地址。已有合法单一 `.invalid` 地址保留，未知非空地址或非站内通知配置拒绝覆盖。
这一步可在履约开关关闭时执行，默认业务运行只检查配置，不迁移用户字段。用户权限、公司范围及B角色保持原状，原生内部日志继续执行。

新增2项原生回归及1项命令行回归。原生RED为 **2 failed，0 errors / 2 tests**：
`2026-10-31 08:36:20 UTC`，2.32秒、4,679次查询；`.runtime/m2c-local-flow-senders-red.log`。
命令行将缺少 `--prepare-senders` 的解析退出明确转为断言失败，9项中8通过、1失败、0错误；
`.runtime/m2c-local-flow-senders-cli-clean-red.log`。

显式发件准备及公司上下文修复后，31项辅助测试、Ruff、编译及diff检查通过。
原生专项 **0 failed，6 errors / 16 tests**：`2026-10-31 08:41:10 UTC`，35.69秒、68,219次查询；
`.runtime/m2c-local-flow-senders-focused-green.log`。6项均已执行至完整单据生成后的库存单据核对，
错误为首张收货单创建者预期不符；原生 `purchase_stock._create_picking` 明确以 `SUPERUSER_ID` 创建自动收货单，
实际采购确认仍由普通采购账号执行。仅修正首张收货单的精确原生创建者预期，保留其他角色、来源、数量和公开动作操作者检查。
最终专项 **16项，0 failed，0 errors**：`2026-10-31 08:45:24 UTC`，37.22秒、72,566次查询；
`.runtime/m2c-local-flow-final-focused-green.log`。普通角色分派、原生单据及过账金额、字节一致重跑和末步失败整体回滚均通过。
实际最终库存10、毛交付100/退10/净90，已过账发票数量100/贷项数量10，实际商品分录净额7650 CNY。
31项辅助测试、Ruff、编译和diff检查通过；408项完整回归正在执行，独立规格及质量审查尚未完成。

完整回归 **408项，0 failed，0 errors**：`2026-10-31 08:51:44 UTC`，189.62秒、370,901次查询；
`.runtime/m2c-local-flow-regression-green.log`。实现提交 `1645e95f9872eff77e8bde1fd2e84b36cb0a48cc` 进入独立规格审查。

## 规格审查：调用者库存筛选上下文

规格审查发现清理调用环境后，原验收商品记录仍保留调用者的库位/批次/日期筛选，
导致数量读取可误用客户库位库存，进而拒绝实际正确的首跑或已完成流程。
新增一项原生回归覆盖带客户库位筛选的首跑与重跑，分别核对实际内部40/10及展示过滤后的0/90。
项目数据库服务停止导致首个尝试未进入测试；启动原项目PostgreSQL16数据目录后取得干净RED：
**1 failed，0 errors / 1 test**，`2026-11-02 02:48:07 UTC`，1.47秒、2,704次查询；
`.runtime/m2c-local-flow-context-clean-red.log`。

修复用已验证的替换上下文重新绑定验收记录，保留原UID及受控元数据读取权限，普通角色业务分派不变。
专项 **17项，0 failed，0 errors**：`2026-11-02 02:55:06 UTC`，44.67秒、80,325次查询；
`.runtime/m2c-local-flow-context-focused-green.log`。31项辅助测试、Ruff、编译和diff检查通过。
完整回归 **409项，0 failed，0 errors**：`2026-11-02 03:03:58 UTC`，198.71秒、380,588次查询；
`.runtime/m2c-local-flow-context-regression-green.log`。最终提交 `91d2fe6eabca5d8b0c96091fa032faf0ead26288`
已通过规格复审及新的独立代码质量审查，无待修复Critical、Important或Minor项。
开发库尚未执行本阶段发件身份准备或业务流程；原19个标识及期初40件证据继续保留。
