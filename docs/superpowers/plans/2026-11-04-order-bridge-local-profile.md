# M3 最终本地角色验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以独立本地角色、专用假数据和真实认证HTTP/浏览器完成M3接收、报价、失败修复、重试和结果核对，保留现有M2黄金数据。

**Architecture:** plain Python本地准备器和执行器，显式opt-in、仅loopback及既有允许数据库，独立静态/流程命名空间，全部业务动作切到真实角色。先后端profile与流程，再HTTP/浏览器和CI；开发库安装前根代理停服务备份，避免运行源码与数据库版本不一致。

**Tech Stack:** 固定Odoo18、项目Python/PostgreSQL、标准库安全凭据/HTTP、现有Node浏览器工具；无新增全局依赖。

---

### Task 1: 独立角色和静态profile

**Files:** 新建 `addons/portfolio_order_bridge/local_acceptance.py`、`scripts/prepare_order_bridge.py`、`scripts/order_bridge_credentials.py`、`tests/test_order_bridge_credentials.py`、`tests/test_prepare_order_bridge.py`、`addons/portfolio_order_bridge/tests/test_local_acceptance.py`；修改测试init。无安装钩子、无RPC模型入口。

- [x] **Step 1: 先写失败测试并根代理实际RED**

凭据标准库测试覆盖独立settings/operator/integration三角色、24字以上不同密码、固定login、600私有文件、owner/regular/no-symlink、原子只创建一次、重复不改、数据库schema错拒绝且无凭据输出。CLI无opt-in、错误库、非127.0.0.1、缺失/坏配置在Odoo导入/文件创建前拒绝。原生测试先断言plain helper文件存在，准备M1/M2 finance先决条件，不继承其他测试类重复用例。

profile原生测试包括首次准备/重复ID相同/无业务单据、M1已有19个标识与角色/公司/库存不变、Source与maps实际Settings创建、专用Operator基础Sales且无AllDocs/其他高权限、Integration无业务权限、inbox+.invalid、cron默认停用、未知同名/同码/缺失/错误模型/重指向标识拒绝、缺失安装/版本/实际表字段/错误公司/目标/非实际Settings保留sudo拒绝，以及调用方捕获异常后的方法自身完整回滚。

- [x] **Step 2: 实现有界准备与安全CLI**

plain API `prepare_local_import(env, passwords_by_role, allow_local=False)`，仅`odoo_portfolio`或`odoo_portfolio_m1_test`且运行PG host严格127.0.0.1，实际用户为Settings；环境、实际公司、已安装bridge版本与模型/视图/表字段就绪先核对。缺少前置M1和已准备CNY finance时拒绝，不自动装模块/改币种/启用履约或重设库存。

静态namespace `portfolio_order_bridge_acceptance`固定拥有三位新角色、独立客户/供应商/新商品/供应商价、来源、客户/SKU映射和停用cron标识。复用M1公司A/仓库/Sales负责人和已标记的M2 CNY价目表，只读验证这些借用对象；不替M1账号授予导入组、不改原有角色/密码/邮件偏好/其他公司，不使用轴承或已有业务商品。

新角色login为`portfolio.bridge.acceptance.settings/operator/integration`；Settings用于来源/映射，Operator仅内部用户+处理组+基础Sales，Integration仅内部用户+接入组，三者只属A公司，inbox通知和.invalid邮件地址。用户/商品由明确本地管理准备器创建，来源/映射/cron改用新普通Settings身份调用已有服务。新商品库存型、交付开票、native单位、无税、有效SKU/ref及CNY供应商，初始无库存调整。source.processor为新Operator、sales为已有M1Sales。Source外部公司代码PARTS-A、来源代码B2B-ACCEPTANCE，不接入真实系统。

全程方法自身保存点+技术namespace事务锁，caller负责commit。已建立profile必须先检查完整namespace/对象/公司/角色及凭据可用性，不用同名未标记对象补身份，不重建丢失记录，不静默修复用户修改。重复仅验证/返回原ID，无新单/邮件/角色赋权/库存。ir.cron只能通过已测Settings准备动作创建，核对固定代码/用户并保持既有启用状态；手动验收要求该专用计划inactive，若被用户显式修改则拒绝，不自动覆盖。

私有凭据位于`.runtime/order-bridge-accounts.json`，三角色schema独立，不修改既有acceptance-accounts.json。CLI参数`--config --database --allow-local-acceptance --credentials`，显式准备后caller单次commit，stdout只输出安全JSON：schema/database/namespace/company/source、各已标识对象的model/id、cron inactive和前置M1标识指纹，不含密码、cookie或配置值。所有新增依赖/临时文件/缓存项目内。

- [x] **Step 3: 聚焦、完整回归和双重审查后提交**

根代理先新纯测试/新原生profile，再完整两模块、全部Python/Node/Ruff；spec→quality，实际日志和计数记录后提交。错误必须无半个profile，重复不借用户的其他记录。此任务不执行进口订单或真实HTTP，不提前声称最终流程通过。

Task1实际记录：代码`22765de`，22项纯测试先因缺少凭据/CLI失败，22项原生先缺helper失败。维护入口明确仅actual UID1+su可兼容原生停用技术root，不改变其active状态，业务配置仍由新普通Settings执行。真实诊断发现唯一多出的权限是Odoo站内通知自动组，精确纳入expected集合，其他超权仍拒绝；namespace锁394与既有391—393分离。最终聚焦22项原生零失败/零错误，65.58秒、123970次查询；完整571项post-tests零失败/零错误，2026-11-04 08:11:33 UTC，428.66秒、700804次查询（`.runtime/m3-local-profile-full-native.log`）。114项Python、27项Node、Ruff与独立规格/质量审查通过。已提交但尚未在开发库执行准备。

### Task 2: 真实角色本地导入流程

**Files:** 新建 `addons/portfolio_order_bridge/local_import_flow.py`、`scripts/run_local_order_import.py`、对应纯测试及原生测试。

- [x] **Step 1: 失败测试与角色流程实现**

动态namespace `portfolio_order_bridge_flow`独立标记两任务/两原生报价、第二SKU修复映射；不把动态数据混入静态profile。普通Integration提交合法单和同内容重发（同任务），普通Operator经公开动作处理第一单为done/draft，Sales负责人尝试确认15%折扣报价仍需审批。第二单使用预留缺失SKU别名，处理后failed且无报价，普通Settings建立精确修复映射，Operator通过真实原因向导apply重排，再处理为done/尝试2，保留failed/retry/done历史。业务API不用sudo，不确认/发货/采购/开票。

动态命名空间固定九个标识：两任务、两报价、四个尝试事件（首单done、次单failed/retry/done）、一个repair SKU映射。静态profile在流程完成后再次验证时，只额外接受此完整动态命名空间中标记的单个repair映射；必须核对模型、同源/公司、固定repair别名、目标第二自有商品及实际Settings创建人。不得因此放开未标记或其他来源的额外映射，也不得产生profile↔flow递归校验。新增完整流程后profile重跑不变与未标记repair映射仍拒绝的反例。

执行前拒绝来源或商品上不属于本流程的任务/单据/映射历史，防止固定批次处理用户额外任务。Source用户、公司、M1/M2引用、模块安装和静态profile须全部通过就绪检查；source/cron权限不迁移。只由命名空间准备者维护标识，模型状态由普通角色原生服务写入。首次执行可逐步建立流程；任意已有缺失/冲突或半成状态都明确报冲突，不重置或接管。

完成后重复执行只读复验原任务/报价/尝试和原始payload/hash、来源/公司、实际creator/owner、金额、待审批和无库存/财务副作用；报告两次必须语义且字节相同。M2轴承库存10、净交付90、净过账7650及既有单据/19标识保持原样。技术事务锁不算业务写入，业务已完成时不能再提交/重试/建向导/改映射。

helper API/CLI同样显式local gate、plain非RPC、caller commit，stdout仅安全ID/状态/计数/金额。根代理执行新原生反例、完整回归，spec→quality后提交。

具体API `run_local_import(env, passwords_by_role, allow_local=False)`只运行已存在且验证通过的profile；缺静态标识时拒绝，不隐式调用其创建路径。CLI只读取既有600凭据，缺失不生成新密码文件。完成后复验可使用profile只读入口和动态标识事实，但不能再次调用submit/action/process/retry/向导create或映射write。

Task2实际记录：13项CLI与24项原生先因缺少实现失败；实现后的46项profile+flow原生聚焦零失败/零错误，184.45秒、278499次查询。调用方非法语言必须在角色查询前丢弃，损坏数据反例确认实际持久值再验证；普通Integration/Operator/Settings/M1Sales的公开动作、原因向导、审计与只读复验均通过。完整595项post-tests零失败/零错误，2026-11-04 09:23:57 UTC，529.24秒、866257次查询（`.runtime/m3-local-flow-full-native.log`）；127项Python、27项Node、Ruff与独立规格/质量审查通过。开发库尚未执行，真实HTTP/浏览器在Task3。

### Task 3: 认证HTTP、浏览器与开发库验收

- [ ] **Step 1: 写脚本契约与最小实现**

新增安全HTTP核对脚本和Node浏览器脚本，读取600三角色凭据与静态/流程报告。以真正session登录调用标准Odoo API，核对Integration只读本源任务/无Sales及设置权限、Operator本源待办/结果/原因历史、Settings来源映射/停用cron，其他公司/角色/raw字段和状态伪造拒绝。响应错误只输出允许分类，不透传traceback/session/password。

实际浏览器打开来源/映射、任务表单、两张native报价和处理历史，按可见行/业务ID验证；记录屏幕截图与安全报告，不用模型存在或隐藏菜单代替页面渲染。所有Chrome依赖项目内；沿用既有短本地IPC路径经验，只有必要OS IPC位于安全临时目录，工具和profile/cache项目内。

- [ ] **Step 2: 根代理备份开发库并安装/运行**

根代理先记录当前M2事实、停止已知开发服务，使用项目PG16工具将开发库及filestore备份到忽略的项目runtime，记录hash/权限/源码与模块版本；不删除源库。安装/升级Bridge后由当前feature源码启动服务（main尚无Bridge前不得回退旧源码直接加载已装新模型）。准备profile两次、跑流程两次、HTTP和浏览器验收，保存安全JSON/PNG；前后只读核对既有M2事实无变化。

开发库升级、实际子进程/浏览器、外部GitHub均由根代理执行。无备份或版本冲突时不继续升级；不得把M4完整恢复验证虚构为已做。

- [ ] **Step 3: CI与交付**

CI现有M2flow完成后安装Bridge到CI本地dev库，再profile两次/导入流程两次/真实HTTP和浏览器；原M2七角色验证保留。制品只增加安全report/log/PNG白名单，不上传三角色凭据、备份、cookies或任意runtime目录。核对确切head四个任务、实际日志/制品、版本、双重审查和最终验收后才合并PR13；main包含Bridge再启动main服务。M3 issue逐项证据完成后关闭，真实简历更新仅引用已交付能力。

之后进入M4独立恢复到新库、性能测量和发布说明，不把M3开发库备份当成已完成恢复/性能。
