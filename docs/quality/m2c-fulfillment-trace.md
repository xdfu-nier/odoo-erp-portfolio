# M2c 履约追踪与本地验收：进行记录

范围已经确认：只读履约追踪、隔离的本地CNY会计准备、真实角色页面流程核对。
本阶段在M2b合并提交 `1444c44ad0eb6698595e96b9ae51452c192ec9c0` 上开发。
固定Odoo18源码 `98e48692891fc9a3aaa7f53001305b322f53cfa2`，原337项Odoo测试与13项项目辅助测试为回归基线。
设计及逐项门禁见[实现计划](../superpowers/plans/2026-10-30-m2c-fulfillment-trace.md)。

## 只读追踪

新增24项 `TestFulfillmentTrace`，覆盖销售来源、采购及收发货/backorder、退货来源、
实际交付和账务行汇总、草稿与过账分离、跨公司/原始调用者权限、单据规则变更、
不可写快照、多个销售来源及不同单位。财务数据限真实Billing角色，金额按关联已过账商品分录的实际余额计入。

首轮测试夹具先退货再新增正常发票，违反M2b已有额度约束；仅调整夹具顺序为
原70件发票过账→准备30件草稿→实物退10→原单贷项10过账。
干净RED：**24项，30 failed，0 errors**，`2026-10-30 06:33:17 UTC`，6.63秒、11,886次查询，
全部失败在缺失追踪模型或销售入口的明确断言；`.runtime/m2c-trace-red-clean.log`。

初版实现的原生专项结果：**24项，1 failed，2 errors**，`2026-10-30 06:49:06 UTC`，8.46秒、16,189次查询；
`.runtime/m2c-trace-first-green.log`。其中两个错误来自测试正向角色携带未授权公司上下文、
Odoo断言辅助函数不接受异常元组；实际失败为来源行被读取规则隐藏后，追踪未按完整来源身份拒绝。
修复按订单直接查询完整来源行身份，随后逐个验证原始调用者权限，避免已过滤的One2many缓存遗漏来源；
正向测试显式使用A公司上下文，拒绝断言指定实际AccessError类型。
修复后专项 **24项，0 failed，0 errors**：`2026-10-30 07:00:30 UTC`，8.65秒、16,325次查询，
`.runtime/m2c-trace-focused-green.log`。13项辅助测试、Ruff、Python编译、5份XML及manifest路径检查通过。
完整回归 **361项，0 failed，0 errors**：`2026-10-30 07:03:30 UTC`，123.67秒、225,167次查询，
`.runtime/m2c-trace-regression-green.log`，包含原337项及新增24项。
独立规格及代码质量审查尚未完成，会计准备和角色页面流程继续等待追踪任务通过两道审查。

独立规格审查发现列表成员已校验，但退货移动的原移动、退货单和backorder的原单仍可经原生关联字段返回名称。
原生 `web_read` 在只请求关联 `display_name` 时会跳过目标读取检查并提权取名。
新增两项实际用例：用户无法读取原交付移动/交付单，但可读退货及backorder；随后新建追踪并读取嵌套来源名称。
主会话原生RED为 **2 failed，0 errors**：`2026-10-30 07:12:23 UTC`，5.16秒、9,910次查询，
`.runtime/m2c-trace-provenance-red.log`。最小修复限定于追踪实际展示的来源目标及公司核对，
在构建快照前和每次读取时检查原移动、退货原单和backorder原单；拒绝时不产生新快照或业务变化。
26项追踪/363项整体回归通过：**363项，0 failed，0 errors**，`2026-10-30 07:18:31 UTC`，
128.77秒、227,398次查询；`.runtime/m2c-trace-provenance-green.log`。
13项辅助测试、Ruff、编译及diff检查再次通过，规格复审和独立质量审查待完成。

规格复审通过 `d6e5178`。独立质量审查又指出两个读取边界：原生复制/名称搜索/条件计数未经过快照的实时来源校验；
退货移动列表还展示了未校验的所属 `picking_id`。新增四项实际回归，30项追踪/367项整体为后续基线。
主会话RED：**4项，9 failed，0 errors**，`2026-10-30 07:35:16 UTC`，6.21秒、10,221次查询，
`.runtime/m2c-trace-read-surfaces-red.log`。实际观察到来源权限撤销后仍可复制父子快照、搜索返回原销售单号、
金额条件计数返回1，以及隐藏的退货入库单关联仍可读；普通和原UID保留的sudo路径均纳入用例。
最小修复将查询限定为真实用户仍有来源权限的快照，再应用调用方条件、排序和分页；
只在这两个临时模型上扩展原生查询，快照复制直接拒绝，原有清理机制继续使用原生删除路径。
已补齐退货移动所属单据的目标读取权限及同公司核对。
专项 **30项，0 failed，0 errors**：`2026-10-30 09:08:28 UTC`，9.72秒、19,968次查询，
`.runtime/m2c-trace-read-surfaces-focused-green.log`。13项辅助测试、Ruff、编译和diff检查通过。
完整回归 **367项，0 failed，0 errors**：`2026-10-30 09:14:15 UTC`，123.59秒、229,875次查询，
`.runtime/m2c-trace-read-surfaces-green.log`。包含原337项及新增30项追踪测试。
最终规格及独立代码质量复核均通过 `a787d561bae208e94ffbfbae48149aef98002ef3`，无待修复Critical或Important项。
查询会先扫描调用者的短期快照身份再校验来源；成本与其快照数相关，后续性能阶段保留实测。

追踪检查点文档提交 `4420a0fa867b5a3ae271ffb56244f1b71b6d2e27` 已发布到[草稿PR #12](https://github.com/xdfu-nier/odoo-erp-portfolio/pull/12)。
该提交的push及PR检查通过；PR运行 [41033814151](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41033814151) 的6份白名单报告已实际下载核对。
Linux原生结果 **367项，0 failed，0 errors**：`2026-10-30 09:32:59 UTC`，95.08秒、229,223次查询。
两次初始化摘要逐字相同，19身份、40件库存及1次期初盘点保留；登录HTTP200、认证/模块读取和4个业务模型导航通过，固定源码版本一致。
报告保存在主项目 `.runtime/m2c-trace-ci/`。PR保持草稿，后续会计准备、完整角色链路及页面核对未据此宣称完成。

在项目根目录、已初始化独立回归库的前提下复现完整回归：

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache .upstream/odoo/odoo-bin \
  -c .runtime/odoo.conf --addons-path=.upstream/odoo/addons,addons \
  -d odoo_portfolio_m1_test -u portfolio_trade --without-demo=all \
  --test-enable --test-tags /portfolio_trade --stop-after-init \
  --http-port=8077 --logfile=.runtime/m2c-business-tests.log
```

主会话在隔离worktree执行时显式传入该worktree的绝对addons路径和主项目配置路径，
避免误测主分支代码。并发用例要求数据库名称精确为上述回归库；不得改成开发库执行。

以上日志位于 `.worktrees/fulfillment-trace-acceptance/.runtime/`，业务测试只使用独立数据库
`odoo_portfolio_m1_test`。开发库仍为M2b的1.4.0，履约策略默认关闭。

## 后续验收

只读追踪任务已通过原生回归和两道独立审查；会计准备、显式启用后的本地完整流程、七角色HTTP与自动化浏览器页面核对尚待开发及实测。
原19个验收标识和40件期初盘点证据必须保持；完整流程运行后当前库存应为10，不能混淆期初与当前数量。
外部订单导入、备份恢复和性能验证保留在M3/M4，人工UAT单独记录。
