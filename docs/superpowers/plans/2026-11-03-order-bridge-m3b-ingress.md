# M3b 持久化接收与幂等 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通过认证 Odoo RPC 将获授权的外部订单持久化为不可改写的 pending 任务，并用真实独立事务证明重复提交只产生一个任务。

**Architecture:** 接收方法复用精确解析器和已授权来源，以来源技术修订写锁协调 REPEATABLE READ 快照，数据库唯一键最终兜底。接收事务只创建任务；来源和任务读取继续恢复实际 UID。处理器与原生报价创建属于下一项任务。

**Tech Stack:** 固定 Odoo 18、PostgreSQL REPEATABLE READ、ORM 保存点、标准库 Decimal、现有解析器和真实多游标测试。

---

## 前置与文件

来源/映射任务必须先通过原生测试和独立规格、质量审查。新增 `models/import_job.py`、`tests/test_import_ingress.py`、`tests/test_import_ingress_concurrency.py`；修改相应 init、安全 XML/CSV、manifest 版本为 `18.0.1.1.0`。已有解析器及 `portfolio_trade` 不改写。

### Task 1: 持久接收与不可变任务

- [ ] **Step 1: 写原生失败测试，根代理实际 RED**

测试类 `TestImportIngress` 标记 post_install/-at_install。每个测试开始先 assertTrue 任务模型存在，避免注册表巨量错误。来源、角色、CNY 与主数据完全处于测试事务；不提交开发数据。

```python
model = self.env["portfolio.import.job"].with_user(self.integration).with_context(
    allowed_company_ids=self.company.ids,
)
first = model.submit_order(payload)
second = model.submit_order(payload)
self.assertEqual(first["job_id"], second["job_id"])
self.assertFalse(first["reused"])
self.assertTrue(second["reused"])
self.assertEqual(first["state"], "pending")
self.assertFalse(first["sale_order_id"])
```

测试同键同摘要复用、行顺序/数值尾零/传输字段变化复用；价格/数量/折扣/客户变化冲突且原始任务不变；不同来源、公司、外部单号互不冲突。接收不产生报价/行/审批/库存/财务单据，不执行或提交事务。未知映射仍可接收，留待工作器处理失败。

反例覆盖无接入组、未授权来源、归档来源、假公司代码、跨公司 context、`.sudo()`、调用方 default_state/default_company_id/default_source_id/内部状态标记、撤销组/公司/来源授权后的旧记录集。载荷错误、非 CNY、与来源币种不一致、单价不能以币种粒度表示、折扣超过当前 Discount 精度均拒绝且无任务残留。

创建后禁止 public create/write/unlink/copy/copy_data（包括 Settings 和 `.sudo()`），原始 JSON/摘要/来源/公司/外部单号/提交人/状态/结果均不可伪造。普通 Sales 不能读取任务；Integration 只读授权来源，Operator 只读分配来源；查询、分组、导出与 id-only web_read 以实际 UID 执行。Settings 维护来源不等于获得载荷权限。

根代理采用已有 native 命令，`-u portfolio_order_bridge --test-tags /portfolio_order_bridge:TestImportIngress`，实际确认缺失任务模型或公开入口失败后解冻实现。连接、安装或发现错误不算 RED。

- [ ] **Step 2: 接收模型及服务端路径**

模型 `portfolio.import.job`：source_id、company_id、external_order_id、payload_hash、raw_payload、canonical_payload、submitted_by_id、received_at、state、attempt_count、last_attempt_at、error_code、error_detail、sale_order_id。所有字段只读；state 为 pending/processing/done/failed；结果关联 sale.order，check_company=True。`(company_id, source_id, external_order_id)` 是数据库唯一约束。载荷和内部错误字段仅 Integration/Operator 组可见，任务整体没有 Sales ACL。

`submit_order(payload)` 在任何业务读取前检查实际内部接入身份。调用解析器，查找唯一实际且当前公司可见、确实授权的来源；查不到或未授权使用同一安全访问错误。来源内部配置使用先授权、后只读该单个来源的有界技术读取，不赋予接入用户 Settings/Sales 权限，不用全局 sudo 运行接收。

在方法自身保存点内：

1. 对已授权来源执行技术 `ingress_revision = ingress_revision + 1` 的参数化 SQL UPDATE；该字段不允许公开 create/write/default 注入。
2. 失效来源配置缓存，在锁内重新核对来源状态、实际用户授权与当前配置；来源普通配置写入与该行锁自然互斥。
3. 使用精确 Decimal 校验币种单价粒度及 Discount 精度；拒绝无法精确表示的值，不舍入。币种首版 CNY，必须等于来源价目表和公司本位币。
4. 按唯一键查任务，摘要相同返回既有任务，摘要不同抛出明确安全冲突。新任务通过私有方法调用原生 create，全部身份与字段由服务端设置，源参数来自已验证配置，不接受任意模型/命令。
5. 返回仅包含 `job_id`、`state`、`sale_order_id`、`reused` 的普通字典。不得手动 commit；调用方事务或原生 RPC 成功提交接收结果。

来源 UPDATE 造成的真正 PostgreSQL SerializationFailure 必须传播到 Odoo retrying，使等待方在新事务读取已提交任务。不能在失效快照中捕获后反复查询；不能把数据库瞬态竞争记录为业务失败。SQL 只处理技术修订/锁与唯一性，客户/商品/销售数据不使用 SQL 写入。

来源创建与公开维护拒绝 ingress_revision；私有锁先校验来源/身份。Integration ACL 可为私有 ORM create 提供模型 create 位，但公开 override 始终拒绝直接创建。所有私有路径重新验证能力，不使用可伪造 context 标记。失败捕获后 flush/invalidate 的活库比较证明无半个任务或摘要覆盖。

- [ ] **Step 3: 根代理聚焦 GREEN**

根代理运行配置与接收两类原生测试，辅助 Python 全量与 Ruff；确认接收反例不影响既有来源配置，并冻结本任务源文件后进入独立事务测试。

### Task 2: 真正并发幂等

- [ ] **Step 1: 先写独立事务测试，根代理运行**

`TestImportIngressConcurrency` 只允许数据库严格等于 `odoo_portfolio_m1_test`。借鉴已有进销存并发测试：单独提交 UUID 命名公司、角色、来源等隔离 fixture；共享已加载 registry 给线程的全新 cursor，避免测试 runner registry 锁死。禁止修改全局价目表/用户默认组；若启用 CNY 触发原生隐含组变化，先保存并恢复原有组与成员再提交 fixture。

第一事务调用 submit 尚未提交；第二独立事务先预读来源建立旧快照，然后提交同键订单。第三连接用 pg_stat_activity 确认真实 Lock 等待，再提交第一事务。第二遇到 SerializationFailure 后必须 rollback 并新建游标重试。核对相同摘要返回同一任务、至少一次真实序列化重试、SQL 活库唯一任务；不同摘要最终为明确冲突且无第二任务。

另测不同公司或来源的相同外部单号可分别接收；直接 SQL INSERT 违反复合唯一键，证明数据库约束确实存在。SQL 反例只在测试清理保存点内执行，不创建有效业务对象。线程必须限时 join，异常必须传播到测试；不用固定 sleep 假装并发证据。

- [ ] **Step 2: 全量回归、审查和提交**

根代理执行 `/portfolio_trade,/portfolio_order_bridge` 完整原生回归、全部纯 Python、Node27 和 Ruff。先规格、后质量独立审查；任何修复都保留可复现反例。更新 `docs/quality/m3-order-bridge.md` 的实际统计，提交 `feat: persist authorized idempotent order submissions`。

本任务结束只证明任务接收的幂等与隔离；只有 M3c 工作器完成后，才可以核对 UAT-014/016 的“一张销售单”结果，不提前将整个 UAT 标记通过。
