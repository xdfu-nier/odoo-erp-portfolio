# M3c 报价处理与人工重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 由专用最小权限处理账号把已提交pending任务转为一张原生草稿报价；失败持久留痕、业务写入完整回滚，修复主数据后可带原因人工重试。

**Architecture:** 在现有单向依赖和接收模型上扩展工作器及不可变尝试记录。使用原生PostgreSQL行锁与`SKIP LOCKED`有界领取，报价、任务与尝试在同一外层事务提交。每次报价创建使用保存点，业务失败只撤销保存点内业务写入；数据库瞬态和未知致命异常向外传播。

**Tech Stack:** 固定Odoo18、项目内Python/PostgreSQL、原生sale.order/line、ORM保存点、Decimal与普通角色测试；不引入外部队列或新依赖。

---

## 文件边界与前置

前置：接收与真实并发Task1/Task2的测试、规格与质量门禁通过。现有模块`18.0.1.1.0`，升级为`18.0.1.2.0`。

- 新建 `models/import_worker.py`：处理账号、来源和任务能力检查，有界领取、原生报价、失败及重试状态。
- 新建 `models/import_attempt.py`：只追加的处理/重试审计与公司/来源边界。
- 新建 `models/import_values.py`：有明确输入的映射解析、单位/商业值校验与安全业务错误；不负责调度或授权提权。
- 修改模型init、安全XML/CSV、manifest；必要时将已验证的价格精度校验提取为共享纯函数，保持接收错误路径与摘要不变。
- 新建 `tests/test_import_worker.py`、`tests/test_import_retry.py`；复用无测试方法的 `tests/common.py` 夹具，禁止继承既有测试用例制造重复计数。
- 这一计划交付服务端处理和重试；界面、定时任务启用、本地HTTP及工作器竞争/退出恢复在下一计划，不提前堆空菜单或用单事务代替恢复证据。

### Task 1: 工作器、不可变尝试与重试服务

- [ ] **Step 1: 写失败原生测试并冻结**

每个测试setUp先断言工作器入口/尝试模型存在，再建立真实公司、来源、角色和映射。只使用测试事务，暂不提交夹具。普通Integration先提交pending，普通Operator+Sales处理；映射由普通Settings维护，Sales负责人不同于Operator。

成功核心契约：

```python
received = ingress.submit_order(payload)
processor.env["portfolio.import.job"]._process_pending()
job = processor.env["portfolio.import.job"].browse(received["job_id"])
self.assertEqual(job.state, "done")
self.assertEqual(job.attempt_count, 1)
self.assertEqual(job.sale_order_id.state, "draft")
self.assertEqual(job.sale_order_id.user_id, salesperson)
self.assertEqual(job.sale_order_id.create_uid, processor.env.user)
```

实际断言公司、仓库、CNY价目表、客户、原生商品/基础单位、数量/价格/折扣、免税、外部引用和提交人原审计保持不变。100×100、折扣15%的报价由后续Sales确认时仍被M1审批拦截；处理器不提交审批、不确认、不预留、不采购、不生成库存/发票。重复处理done不新增报价或尝试；同载荷重新提交返回同任务及报价ID。

失败反例至少覆盖客户/SKU映射缺失、归档、目标异公司、商业客户失效、商品单位归档/数量不符合单位粒度、原生SKU/ref歧义、币种/价目表变化、原生销售规则拒绝。缺失映射不自动创建主数据。注入第二商品行创建后的真实业务异常，证明报价、行及关联业务写入都回滚，failed任务、错误路径、尝试次数/时间/执行人仍可在调用方活事务中读取。以标准unittest捕获异常/flush/invalidate做活库核对，不能让测试框架保存点替生产实现回滚。

人工重试测试：失败任务由所属Operator填写非空原因后回pending；Settings修复映射再处理成功，保留失败尝试、重试人/时间/原因和原始载荷，次数从1到2；禁止空原因、Integration/Sales/普通Settings重试、他人来源或异公司任务、pending/processing/done重复重试。审计public create/write/unlink/copy和原始任务字段伪造均拒绝。

所有新私有入口都测试实际UID能力，含保留sudo、外部来源记录集环境、当前/实际公司变化、非指定处理账号以及假context/default字段；不能仅依赖按钮或公开入口的调用顺序。Operator额外的Sales记录规则仅覆盖其本人创建的订单及对应行，不能据此读取同公司他人的普通报价；任务与载荷仍按当前来源分配隔离，不把业务报价读取与原始载荷授权混为一套规则。

- [ ] **Step 2: 根代理实际RED**

根代理沿用固定源码、根配置、当前worktree addons和严格测试库，`-u portfolio_order_bridge --test-tags /portfolio_order_bridge:TestImportWorker,/portfolio_order_bridge:TestImportRetry`，日志`.runtime/m3c-worker-red.log`。模型/入口缺失应为真实测试断言失败；安装、发现或连接失败须先查环境，不能当RED。根代理确认后解冻实现。

- [ ] **Step 3: 最小权限处理、映射与报价**

入口 `_process_pending()` 为私有model方法，固定批次5；使用实际有效内部Operator+基础Sales，单公司且当前范围恰为其公司，拒绝Settings/访问管理/销售经理/采购/仓库/Billing权限组合。按来源指定processor再次检查，不通过sudo建立订单。来源和映射恢复实际用户环境检查，绑定同公司或合法共享对象，未知数据不自动创建。

候选来源仅本人负责且有效的来源。参数化SQL从这些来源/当前公司pending任务中按ID领取，`FOR UPDATE SKIP LOCKED LIMIT 5`；不能先取前5个ID再跳过锁而遗漏后面的可处理任务。每个任务处理入口自身再次检查实际身份、来源、公司、状态并持有任务锁，避免内部调用依赖可伪造context能力标志。

客户/商品映射必须唯一、有效；记录本次解析的映射ID、原生目标ID、商品基础单位，以及来源仓库/价目表/负责人等快照。数量必须精确符合该单位舍入粒度；价格与Discount精度按当前配置再次验证。用固定允许字段构造原生报价及`Command.create`商品行，显式清空首版免税验收路径的税，不接受任意模型ID或ORM命令。数据由已验证持久快照解析，不继续引用调用方可变对象。

必要的Sales记录规则仅向Operator开放其本人创建的订单/行，用于创建后原生处理和查询；全局公司规则继续生效，不授予all-leads或经理组。若普通Sales owner规则阻止原生create后写入，用针对本人创建记录的组规则解决；禁止sudo或授予全公司写权限。

原生报价的客户/商品/单位和实际商业值必须验证；使用高精度Decimal计算期望免税行金额，按CNY舍入对平原生行/整单，避免大数或精度损失悄悄改变输入。原生M1政策按当前公司继续生效；工作器不修改审批政策或自行确认报价。

- [ ] **Step 4: 保存点、错误与不可变尝试**

任务从pending到processing并增加attempt_count/时间；在业务保存点内创建并验证原生报价。成功时设置done和sale_order_id，追加成功尝试。受控业务错误回滚业务保存点，保存点外设置failed并追加失败尝试。只捕获明确业务异常；SerializationFailure、DeadlockDetected等瞬态错误以及未知异常必须向外传播，使外层事务回滚且仍可恢复pending。

错误使用稳定代码和可定位路径，不持久化任意异常traceback、配置或凭据。映射缺失应定位customer_ref或具体商品行。审计模型`portfolio.import.attempt`有job_id、来源派生company_id、attempt_no、event_type（done/failed/retry）、actor_id、开始/完成时间、安全错误及重试原因、映射/来源默认快照；只追加、不可复制/删除/改写。普通Integration和Operator仅访问各自获授权来源，Sales只看原生业务结果。

任务状态、尝试和报价由调用方/原生cron外层事务一起提交；工作器不手动commit。后续独立事务测试会在创建报价后、提交前退出，验证全量回滚。当前不得用一次单事务成功宣称恢复已通过。

公开 `action_retry(reason)` 只允许所属Operator对failed任务操作，先授权再锁、锁内重验状态，保留原始载荷与失败尝试，追加操作者/时间/原因后回pending。done不可重试；不引入自动重试或后台线程。

- [ ] **Step 5: 根代理GREEN、完整回归与双重审查**

根代理先聚焦工作器/重试，再完整 `/portfolio_trade,/portfolio_order_bridge`；同时运行83项纯Python、27项Node和Ruff。看真实post-tests统计及活库业务结果，先独立规格、再独立质量审查。发现问题先补反例并实际RED，修复后复验；通过后提交自有文件，更新实际计数与证据。

此任务完成后才能把UAT-014/017中单任务原生报价、失败修复重试部分记为自动化通过。多处理器领取竞争、原生提交前退出恢复、真实角色界面/HTTP、M4备份恢复和性能仍分别保留门禁。
