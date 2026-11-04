# M3d 导入操作界面与停用处理计划 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让设置、接入和处理角色通过真实Odoo菜单查看来源/映射/任务/审计，处理待办、带原因重试并准备默认停用的专用用户cron。

**Architecture:** 原生XML列表/表单与少量经过实际用户授权的动作，复用已验证工作器和重试服务。任务及审计只读；重试原因通过TransientModel收集。调度使用原生ir.cron、固定模型/代码和配置中的专用Operator，创建后保持inactive，不授予系统权限或注册后台服务。

**Tech Stack:** 固定Odoo18 ORM/XML/ir.actions/ir.cron、当前模块18.0.1.2.0升级18.0.1.3.0、原生角色测试，无新依赖。

---

## 文件与边界

- 新建 `models/import_ui.py`：队列处理、重试/报价/映射动作、受保护的来源cron关联与准备动作。
- 新建 `wizard/import_retry.py`、wizard init：实际Operator私有原因向导，最终调用现有`action_retry(reason)`。
- 新建 `views/import_source_views.xml`、`views/import_job_views.xml`、`views/import_attempt_views.xml`、`views/import_menu.xml`、`wizard/import_retry_views.xml`。
- 修改manifest、init、安全CSV/XML；新建 `tests/test_import_ui.py`。
- 复用服务器已验证的安全与业务路径，不为UI另做一套状态逻辑、计数账或匿名控制器。实际本地seed/HTTP/browser/部署在下一计划，不把get_view通过写成人工UAT通过。

### Task 1: 原生界面与授权动作

- [ ] **Step 1: 编写失败原生测试，根代理实际RED**

`TestImportUI`以post_install/-at_install标记。setUp先断言动作入口/向导/视图存在，再建立现有普通Settings/Integration/Operator/Sales及有效映射夹具。失败测试应因未实现动作断言失败，不因ref缺失或测试发现错误。

测试菜单对角色可见范围、动作模型与domain、当前用户`get_view/get_views`实际解析XML、必填字段新建时可输入和保存后只读、任务/审计list/form禁止create/edit/delete、Operator处理/重试/打开报价动作，Integration只读查询、Settings维护来源/映射。不能用固定字符串存在代替模型/动作/权限实际可用。

测试所有public动作直接RPC/ORM路径（不依赖按钮）：无角色、错误公司/他人来源、retained sudo、伪造defaults/uid/context、授权撤销、非failed重试、空/过长原因、向导换绑job或跨用户读取，均拒绝且活库任务/审计不变。

原生cron配置验证实际用户是专用Operator、公司/来源一致、固定model_id与代码、默认inactive、重复准备复用同一cron不改变既有active状态、不支持任意模型/代码/账号注入。通过该cron的原生server action以cron.user_id实际运行，验证报价create_uid为Operator、不是Settings/sudo。不得给Operator全局cron管理权限。

- [ ] **Step 2: 根代理运行RED**

根代理固定源码、当前worktree和专用库，`-u portfolio_order_bridge --test-tags /portfolio_order_bridge:TestImportUI --stop-after-init --http-port=8077`，日志`.runtime/m3d-console-red.log`。确认真实缺失功能失败后授权实现。

- [ ] **Step 3: 原生页面及public动作**

统一菜单父项为`portfolio_trade.menu_portfolio_root`。设置用户看到来源、客户映射和商品映射维护；Integration/Operator看到外部订单只读队列与尝试详情；其他业务角色看原生报价，不获得原始载荷或导入审计菜单。

Odoo18使用`list,form`和表达式modifiers，不用旧attrs。来源新建时公司/外部公司代码/来源代码必须可填写，保存后不可更改；公司默认当前实际公司，现有服务端实际公司检查不变。客户/SKU映射的来源新建时可选择/由动作预填，保存后只读，原生目标可由Settings修复/归档。private来源字段按已有groups限制，Integration不可通过view或动作获得配置。

任务页主区显示来源、公司、外部订单号、状态、次数/处理时间、人工可读失败信息、原生报价结果；接收报文及技术摘要放只读辅助页供授权排查。尝试列表显示结果/执行人/时间、错误路径及重试原因，快照只读。字段不接受从one2many命令修改历史。

动作签名：`job.action_process_pending()`处理本人获授权队列的固定批次并返回通知/刷新；传入记录时也检查其当前读取/来源权限，不借错误选择触发其他队列。`job.action_open_retry()`仅所属Operator的failed单任务，返回原因向导；`job.action_open_sale_order()`检查当前角色对真实报价的原生read权限后返回form动作。来源提供`action_open_customer_mappings()`和`action_open_product_mappings()`，仅Settings/所属Operator且当前来源可读，返回精确来源domain，不返回sudo记录。

- [ ] **Step 4: 原因向导及默认停用cron**

`portfolio.import.retry`包含readonly job_id、派生公司及required reason。创建/读取/web_read/写入/apply都恢复实际Operator并核对当前job来源、公司和状态；向导创建后禁止换绑job，其他用户不能读或复用其向导，授权撤销后旧向导不可继续查看。界面readonly job字段使用force_save确保显式ID传入，不能只依赖caller context default。`action_apply()`调用既有`job.action_retry(reason)`，完成后关闭并刷新。

保持TransientModel原生按创建人隔离及技术超级用户autovacuum清理，不能用env.su让普通用户绕过，也不阻止真实原生清理。普通Integration/Sales/Settings无向导业务ACL，向导不是原始载荷修改入口。

来源增加`processing_cron_id`，readonly、仅Settings可读，public create/write/default注入禁止；初始明确False。`source.action_prepare_processing_cron()`只接受当前实际Settings且有权的单来源，锁内重验配置、专用处理账号，建立原生ir.cron并私有写入关联：model为portfolio.import.job，code固定`model._process_pending()`，user为source.processor_user_id，interval1minute，activeFalse。无任意参数或角色授予。

重复调用复用已关联且model/code/user一致的cron，不复制、不静默启用/停用；关联缺失或配置被改成其他模型/账号时明确拒绝，不擅自接管别人的计划。来源改处理账号后，Settings需显式核对原生cron账号，不由接入用户修改。

项目原生开发配置`max_cron_threads=0`继续默认手动处理。文档说明如需自动处理，需明确启用专用cron并在项目配置启用cron线程后重启；本地验收不自动启用。测试以原生server action在cron.user_id下运行验证业务身份，不给Operator全局cron读写权，不覆写上游cron安全逻辑。

- [ ] **Step 5: 根代理GREEN、回归、两阶段审查**

根代理聚焦UI及已有工作器/重试，再完整两模块原生、92项Python、27项Node、Ruff。审核对象视图与动作在普通角色下可打开，原因输入能触发真实状态/审计变化，privatefields/othercompany/fakecontext反例拒绝。先独立规格、后质量审查；修复先实际失败证据，再最小实现。通过后提交自有文件并更新实际结果；真实HTTP/browser验证不提前宣布完成。
