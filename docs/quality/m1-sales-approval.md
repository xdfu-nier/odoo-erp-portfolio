# M1 销售审批：实现与验收记录

日期：2026-10-27（含安全审查回归）。模块版本 `18.0.1.1.0`；Odoo 18 固定源码
`98e48692891fc9a3aaa7f53001305b322f53cfa2`。关联开发任务 #4。

本记录覆盖销售审批子集。M1 主数据任务 #3 / UAT-001 尚未实现，M1 整体未完成。
采购、库存、发票、退货、贷项与接入不在本次交付范围。

## 实际结果

初版源码提交 `8f3dac8`，规格安全修复提交 `c254f82`；代码质量修复随本记录一并提交。
CI专用测试库对齐提交 `9585cd3`，并发测试与原生CI统一使用下述隔离库名。
测试仅写入 `odoo_portfolio_m1_test`，没有升级或写入 `odoo_portfolio`。
测试使用项目内 CPython 3.12.13 与 PostgreSQL 16.2。

- 质量修复后完整 Odoo 模块升级及测试：**32 个测试，0 failed，0 errors**。
  其中26个业务/权限测试、6个独立事务并发测试；包含状态、派生金额、
  `ir.default` 行归属、服务端审计时间，以及下述关联完整性回归。运行6.87秒，10,312次查询。
  日志的36 tests统计包含类级生命周期，业务测试数以结果行32为准。
- 日志：工作树 `.runtime/m1-quality-green-exclusive.log`，结果行时间 `2026-10-27 06:52:54 UTC`。
  由主代理独占测试库执行；真实序列化冲突回滚后以新事务重试，最终无失败。
- 运行配置/原生环境辅助测试：`unittest discover -s tests -v`，7个通过。
- `ruff check addons/portfolio_trade`：All checks passed。
- 继承标准销售表单、公司设置表单、拒绝向导及 ACL/规则在真实模块升级中加载成功。
  尚未执行浏览器人工点击/截图验收，不能将上述服务端测试表述为人工界面验收。

测试先行提交 `cfac4d6` 的真实红灯证据在 `.runtime/m1-red.log`：
`test_01_standard_confirmation_requires_approval` 预期 UserError，但标准确认直接成功。
随后补齐实现、修正早期测试夹具并运行绿灯；过期政策界面指示字段另有
`.runtime/m1-policy-ui-red.log` / `.runtime/m1-policy-ui-green.log` 回归记录。
安全审查回归先在原模型执行，`.runtime/m1-review-red-exclusive.log`记录22个测试、
9个失败、0个错误，复现派生金额、默认值和状态绕过；修复后的完整绿灯见上述专用日志。
独立规格复审已通过。代码质量审查指出的派生身份字段伪造、跨单关联级联删除、
嵌套移动锁顺序问题已修复并通过32项测试，独立代码质量复审待完成。

## 覆盖映射

| 验收 | 本次实际覆盖 | 结果 |
| --- | --- | --- |
| UAT-002 | 销售单/审批历史公司隔离、跨公司读写/操作拒绝；销售行公司/币种/客户/负责人/状态派生身份不可伪造；政策禁用公司的标准确认 | 通过（审批子集） |
| UAT-003、004 | 未税49,999.99/50,000；商品折扣10/15；文本备注不触发商品折扣政策 | 通过 |
| UAT-005 | 普通销售无审批权限；创建者、负责人、提交人禁止自审；直接字段/创建者/context默认值伪造被拒绝；经理不能代替负责人确认 | 通过 |
| UAT-006 | 客户、地址、公司、币种、付款/合同条款、交期、负责人；行商品/单位/数量/价格/折扣/税/描述/分析分配/包装/路线，增删与移动均失效；已确认行商业修改及跨单链接级联删除被拒绝；派生金额/税额不可直接写入 | 通过 |
| UAT-007、028 | 确认与行新增/修改/删除/移动、政策修改，以及审批与修改的双向竞争；重复审批仅一条通过结论 | 通过 |
| UAT-020 | 事件追加记录人、时间、原因、快照；销售/经理不能创建/修改/删除历史；拒绝原因与重提 | 通过 |
| UAT-001、008—019、021—027 | 本次审批以外的主数据、进销存、接入、完整发布/恢复及性能验收 | 未执行 |

并发测试使用不同 PostgreSQL 游标和事务，夹具明确提交在专用测试库。
通过 `pg_stat_activity.wait_event_type = 'Lock'` 验证第二事务确实等待第一事务；
遇到真实 repeatable-read `SerializationFailure` 后回滚并用新游标重试，最终再次检查状态和权限。
日志中的预期 SQL serialization error 与测试失败不同；最终结果无失败。
不可变审计夹具保留在测试库作为证据，测试账号在类清理时归档。

## 复现命令

在特性工作树运行；配置文件包含本地凭据，不要打印或提交它。
`--http-port=8076` 避开现有8069服务；Odoo测试模式会启动HTTP，`--no-http`不足以避免端口冲突。

```sh
mkdir -p .runtime .cache
.venv/bin/python -X pycache_prefix=.cache/pycache .upstream/odoo/odoo-bin \
  -c /Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.runtime/odoo.conf \
  --addons-path=/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.upstream/odoo/addons,/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.worktrees/order-approval/addons \
  -d odoo_portfolio_m1_test -u portfolio_trade --without-demo=all \
  --test-enable --test-tags /portfolio_trade --stop-after-init --http-port=8076 \
  --logfile=/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.worktrees/order-approval/.runtime/m1-quality-green-exclusive.log
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -v
.venv/bin/ruff check addons/portfolio_trade
```

首次红灯安装使用同一命令，将 `-u` 换成 `-i`，端口8071，日志 `m1-red.log`。
首次数据库初始化报告本地缺失可选 `pg_trgm`；缺少PDF工具的提示也存在，均未阻塞销售审批测试。
测试库名保护是有意约束：完整并发测试在其他数据库名下拒绝创建已提交夹具。

## 操作说明

1. 系统设置管理员进入 **Settings → Users & Companies → Companies**，打开目标公司，
   在 **Sales Approval Policy** 页启用政策。默认关闭；初始阈值未税50,000或任一商品行折扣>10%。
   金额只支持公司本位币。金额须有限非负，折扣须0—100。
2. 建立独立账号：销售使用Sales的 **User: Own Documents Only**；审批人使用Sales的
   **Administrator**（销售经理）。经理无需系统Settings权限；普通销售和经理都不能更改公司政策。
   限定各自允许公司，另设第二经理以演示负责人/提交人自审限制。
3. 以销售建立50,000本位币未税报价，或100元、折扣15%的报价。通过标准Sales菜单和API创建都会受政策约束。
   在审批前点击标准Confirm会被服务端拒绝。点击 **Submit for Approval**。
4. 切换到另一名经理，点击 **Approve**；回到销售账号点击标准 **Confirm**。
   创建者、负责人或提交人即使有经理权限，也不能审批自己的单据。
   审批经理不能代替负责人确认，启用公司须由报价负责人执行Confirm。
   管理员创建的报价应交给另一位经理审批；不要用管理员同一账号演示全部步骤。
5. 另建报价，提交并通过后先不确认，修改数量/价格或其他商业字段，审批立即回到Draft，
   历史追加Invalidated。重新提交，由其他有权经理再次审批，再确认。
6. **Reject** 打开原因向导，拒绝必须填写原因；销售修改后重新提交。
   系统管理员改变政策后，旧审批不能放行；Sales Approval页显示提示并提供再次提交入口。

政策启用公司的全部报价受约束，没有逐单退出开关。取消/确认等标准销售状态仍由Odoo管理。
已确认单据在政策启用期间不能直接修改关键商业内容。
直接ORM写入确认/取消或将已确认订单改回Sent/Draft会被拒绝，必须调用标准业务动作。
未执行订单可通过标准取消及重置报价流程重新提交；已有完成交货或已过账发票时禁止重开。

## 设计边界与后续验收

审批数据字段、派生财务金额以及销售行的公司/币种/客户/负责人/状态/仓库派生身份字段禁止公共create/write及context/`ir.default`伪造；正常声明的字段默认值保留。
标准确认/取消/重置动作在私有Python执行上下文中获得绑定游标、用户、订单ID及目标状态的权限，
没有可由调用方伪造的Odoo context授权标志，标准业务副作用仍由父类动作执行。
仅创建审计事件时窄范围提权，业务操作不整体sudo。审计时间由服务端明确写入，不使用调用方默认值。
历史独立保留事件发生时公司，商业改单不会搬移旧公司的审计内容。
销售行的跨订单 `linked_line_id` 关系只允许同一报价；跨单链接、逆向关联和通过报价嵌套命令移动行均被拒绝，避免级联删除已确认行。
分析分配、客户可见描述、包装和库存路线属于本次报价快照/失效边界；更广泛的分析会计配置及进销存自动规划仍属后续范围。

统一锁顺序是公司ID、父订单ID，升序加锁；访问权限在SQL前后检查，加锁后清除ORM旧缓存。
所有商业行变化更新父订单版本以产生repeatable-read冲突；政策修改增加公司政策版本。
公司行锁会串行化同公司敏感操作，这是首版正确性取舍，尚未做吞吐量或性能承诺。

独立规格复审与代码质量复审均已通过；修复了审查发现的状态/金额/默认值绕过、相关身份字段篡改、跨订单关联级联删除和嵌套移动锁序问题。
源码已发布至[PR #8](https://github.com/xdfu-nier/odoo-erp-portfolio/pull/8)，远端CI状态以该PR检查为准。
浏览器人工验收、真实分销商品主数据和后续业务闭环验收尚未完成；未部署生产数据库。
