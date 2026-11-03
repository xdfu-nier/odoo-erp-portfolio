# M2c 本地CNY会计准备：进行记录

目标为已标记的虚构工业配件验收公司准备最小会计配置，接续只读追踪任务。
这不是法定中国科目表或税务本地化；实际业务动作继续通过原生角色与单据执行。
设计及门禁见[实现计划](../superpowers/plans/2026-10-30-m2c-fulfillment-trace.md)。

## 配置范围与不变量

`portfolio_trade_finance` 使用独立命名空间保存6个身份：收入、费用、应收、应付账户，销售日记账和CNY价目表。
只为原验收客户的应收、供应商的应付及3个验收商品的收入/费用补充PARTS-A公司属性。
原 `portfolio_trade_acceptance` 的19个身份和原生期初盘点40件必须完整保留；当前库存可以随后续合法业务变化。
PARTS-B的策略和属性、角色、币种开关、共享分类和全局默认值不得因准备命令改变。

命令和API均验证实际本地目标；原始调用者必须具备Settings权限和PARTS-A实际/活动公司范围。
默认准备不启用履约；`--enable-fulfillment` 仅在全部预检和会计配置后显式启用PARTS-A。
`--check` 只返回安全摘要，不提交数据，与启用参数互斥。已有未知或损坏配置拒绝覆盖、认领或替换。
整个准备操作由保存点和命名空间锁保护，失败后检查实时数据恢复，helper本身不提交事务。

## 测试先行证据

新增23项 `TestLocalFinance`，使用真实M1验收数据及原生账户模型，不加载generic chart。
夹具先完成原19身份和40件原生盘点，再断言准备API缺失。
主会话RED为 **23 failed，0 errors / 23 tests**：`2026-10-30 09:30:55 UTC`，18.84秒、41,742次查询；
`.worktrees/fulfillment-trace-acceptance/.runtime/m2c-local-finance-red.log`。

命令行新增9项测试，非法配置/目标/参数在导入Odoo前拒绝，检查模式不提交，失败不提交且不打印配置秘密。
RED为 **9 failures，0 errors**，全部为缺少显式准备命令；
`.worktrees/fulfillment-trace-acceptance/.runtime/m2c-local-finance-cli-red.log`。

最小实现后22项辅助测试、Ruff、编译及diff检查通过。首轮原生23项为0 failed、4 errors，
`2026-10-30 09:35:54 UTC`，29.04秒、60,463次查询；错误均在测试夹具或实时快照中：
不存在的仓库字段、删标识后错误解引用，以及新增账户公司前未准备原生编码映射。
仅修正测试后重跑为0 failed、1 error，`2026-10-30 09:40:45 UTC`，25.67秒、58,122次查询；
剩余错误来自原生Odoo禁止修改已完成移动的来源位置，测试无法进入helper。
改为在清理保存点内修改原库存调整来源位置的usage，保留DONE移动和明细不变，验证同一预检拒绝条件。
以上调整未改生产控制或移除用例。最终专项 **23项，0 failed，0 errors**：
`2026-10-30 09:44:21 UTC`，26.19秒、58,267次查询；`.runtime/m2c-local-finance-final-focused-green.log`。
完整回归 **390项，0 failed，0 errors**：`2026-10-30 09:47:28 UTC`，141.80秒、288,989次查询；
`.runtime/m2c-local-finance-regression-green.log`，包含原367项及23项会计准备。
22项辅助测试、Ruff、编译及diff检查通过；独立规格和代码质量审查尚待完成。

## 规格审查：实际安装状态

规格审查指出Python模型/方法已加载并不能证明数据库已升级，原生 `installed_version` 又代表磁盘最新版本，
实际持久化安装版本保存在 `latest_version`。旧M2b开发库可在加载新Python后缺少追踪表及表单。
新增两项原生回归：只移除已安装追踪表单XMLID，或只将持久化版本设为较旧版本，同时保留新Python API。
主会话RED为 **2项，6 failed，0 errors**：`2026-10-31 01:34:06 UTC`，2.75秒、6,079次查询；
`.runtime/m2c-local-finance-readiness-red.log`。预检、准备和显式启用三种模式均实际复现未拒绝。
最小修复增加实际已安装版本与源码版本匹配、原生模型元数据/表单及两张追踪表的只读技术目录检查，
所有模式在写业务数据前完成验证；缺项明确要求原生模块升级。
专项 **25项，0 failed，0 errors**：`2026-10-31 05:33:17 UTC`，30.74秒、64,800次查询；
`.runtime/m2c-local-finance-readiness-focused-green.log`。22项辅助测试、Ruff、编译及diff检查通过。
完整回归 **392项，0 failed，0 errors**：`2026-10-31 07:19:02 UTC`，155.82秒、296,118次查询；
`.runtime/m2c-local-finance-readiness-green.log`，包括原367项和25项会计准备。
修复提交 `faca691bef3a17478732780dd38a01d595c34614` 已通过规格复审及新的独立代码质量审查，无待修复Critical或Important项。
开发库的实际检查、模块升级和显式会计准备安排在本阶段集成验证，角色流程先在独立原生测试中完成。
开发库尚未执行本阶段的会计准备或启用，仍保留M2b的19身份、40件当前库存与关闭的履约策略。
