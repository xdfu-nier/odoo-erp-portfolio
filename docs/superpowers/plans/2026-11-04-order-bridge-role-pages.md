# M3 最终认证HTTP与浏览器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已完成的两单本地导入在实际角色登录、原生菜单/表单和可见浏览器页面中得到可复现证据，并在开发库及CI保留原有M2黄金数据。

**Architecture:** HTTP核对器与浏览器核对器分开。输入使用600凭据和安全profile/flow报告，运行目标只允许loopback及原有两库；模型访问、菜单动作、业务结果和权限反例由实际用户会话验证，失败不把网络错误当权限通过。

**Tech Stack:** 标准库urllib/json、现有Node22/Chrome CDP、原生Odoo接口，全部工具/profile/cache/report项目内；不增加全局依赖。

---

### Task 1: 实际认证HTTP核对

**Files:** 新建 `scripts/verify_order_bridge_roles.py`、`tests/test_order_bridge_http.py`；必要时增加小型无Odoo依赖的输入报告校验函数，不修改业务模型/核心helper。

- [x] **Step 1: 纯契约失败测试，根代理实际RED**

输入验证覆盖三角色私有凭据、原七角色凭据（仅Sales和B公司other_sales会话）、M1验收报告、M3静态17/动态9报告与数据库一致、正确模型/唯一正整数ID、两报价draft/CNY金额309与161.5/owner和creator/四尝试ordinal及reason、无原始密码/hash/config输出。路径仅当前或主项目runtime、无符号链接、输入不能被输出覆盖；无效目标/报告在网络之前拒绝。

模拟HTTP层区分真AccessError/UserError/ValidationError与传输失败、过期session、错误JSON、HTML/redirect、未知异常。只把预期服务端错误类型计为权限反例通过，不能把断网或任意VerificationError当成功。原始远程error.data.debug/description、cookies和认证响应不输出。

- [x] **Step 2: 固定真实角色检查**

CLI参数：`--base-url --database --credentials --sales-credentials --acceptance-report --profile-report --flow-report --output`。标准Odoo`/web/session/authenticate`登录五位实际角色：新Settings/Operator/Integration、已有M1Sales、B公司other_sales。各会话独立CookieJar、禁proxy/redirect、有限响应大小/超时；目标只有http://127.0.0.1:port且无路径/参数/用户信息，dbname两库之一。

核对session实际UID、当前/允许公司、真实组及禁止组：新Operator只有基础Sales、无AllDocs/经理/其他业务或Settings；新Integration无Sales；Settings有System但不因此获得Job原始载荷，Sales/B用户无导入组。

通过真实`ir.ui.menu.load_menus`和`/web/action/load`、模型get_view/list/form验证角色可见菜单及实际数据。Integration/Operator可读两任务、四尝试及本源scope，Settings来源/两原始映射+repair映射及inactive专用cron，Sales读本人两张原生报价、来源/金额/商品/单位/折扣/税/草稿/待审批；B公司用户不见这些单据。

Integration读取自己任务原始报文仅在内存解析后重复submit同内容，必须返回原job、quote与reusedTrue；只技术修订可变化，任务原始raw/hash/状态/次数、两报价和尝试保持。Operator公开process队列完成后返回0不增单。Sales真实action_confirm必须收到预期UserError并且随后报价仍draft、无新审批/库存/发票；未知错误不算审批通过证据。

直接HTTP反例核对：Integration/Settings/Sales/B用户处理或重试、Settings读取原始任务、Integration读cron私有字段/映射/报价、任务/尝试public create/write/unlink/copy、跨公司allowed_company_ids和伪造uid/default状态、done打开retry、越权id-only web_read/export/query。每组反例后再做允许的健康读取和活库数量/原始审计比较，防止离线、session失效或请求格式错误造成假阳性。

输出schema/ok/database/每角色UID/公司/组布尔值、菜单/动作ID、允许的业务单据ID/状态/金额/数量/attempt摘要、反例布尔值，不含密码、cookie、原始payload/错误体。固定检查集合须完整，不能只验证已有的子集就给ok=true。caller命令错误写安全错误分类并非0退出。

- [x] **Step 3: 根代理纯验证与双重审查后提交**

先根代理实际RED再实现，纯契约/全部Python/Ruff通过、spec→quality后提交；此时脚本存在不代表真实HTTP已验收。后面开发库或CI实际运行后才更新通过记录，不预先虚构结果。

### Task 2: 可见原生浏览器页面

**Files:** 新建 `scripts/browser_order_bridge_check.js`、`tests/browser_order_bridge_check.test.js`，最小复用既有Chrome启动/短IPC工具。避免为新流程大幅重构已有M2验收行为。

- [x] **Step 1: Node契约测试与截图实现**

输入继承HTTP输入的本地目标和安全报告，并验证HTTP报告五角色全部通过/ID对应、输出目录在runtime。所有角色各自独立浏览器context或profile，凭据只用于登录不写报告；等待页面用可验证DOM条件和时间上限，不能只sleep后截图。

Settings实际打开来源及客户/商品映射页，核对A公司/来源代码/负责人与专用Operator、三条映射及对应商品、专用计划默认停用。Operator/Integration实际打开两任务表单、raw只读辅助页及四尝试/原因历史，核对done、次数1/2、来源公司、creator/处理人和外部单号。Operator及Sales打开两张原生报价，核对draft、309/161.5 CNY、Sales owner、商品数量/折扣/税和需审批状态；已完成任务不显示重试操作。Settings/Sales/B用户没有不应展示的导入载荷入口。

2026-11-05原生功能组预检补充：实际Operator/Sales均无`uom.group_uom`及`sale.group_discount_per_so_line`，单公司字段也受原生显示规则限制。公司以明确标注来源的可见navbar证明；审批以可见Sales Approval页签、draft状态及申请按钮配合实际HTTP审批检查证明，不虚构隐藏字段。可选择启用原生显示组、另加定价视图或核对现有可见行的金额关系；本步采用第三种，保留原角色与界面配置。页面必须显示商品、数量、单价、税及小计；折扣列未显示时，以这三个可见数值计算0/15%折扣并记录`origin=visible_quantity_price_subtotal`，同时要求HTTP真实行折扣/单位检查通过。若列已显示则直接验证；单位列未显示也明确记录为原生功能未启用，不声称浏览器显示了单位。小计分别为105/204与102/59.50，错误或不可见数值不能作为通过证据。Settings还核对实际Integration标签，菜单证明覆盖全部导入/业务根与叶节点，Integration的列表表头处理按钮和表单按钮分别核对。

这一步读取已经完成的流程历史，不额外创建第三单或改动严格九标识验收集合。真实原因向导创建/apply已由原生Form验证；浏览器证据精确描述可见结果和历史，不将只读页面称为再次执行了重试。

表格只匹配可见的准确业务行，不把Odoo sample占位行当记录。atomic可见行查找/点击，不能分步选错行。安全报告必须验证完整角色/页面集合和实际DOM证据再ok=true；截图文件名为固定白名单，失败只输出分类/计数，不输出页面正文、输入框、session或Chrome原始stderr。

- [x] **Step 2: 根代理测试/静态审查后提交**

根代理先Node实际RED、实现后新旧Node/全部Python/Ruff通过；spec→quality后提交。实际运行由根代理在下面环境准备后执行，CI也必须执行并保留安全PNG/report，不以契约单测替代浏览器通过。

### Task 3: 开发库保护、真实验收和CI

- [x] **Step 1: 根代理停服备份与版本预检**（备份尚未恢复，M4另行验证）

只操作现有项目开发库odoo_portfolio；先只读记录M1十九标识、既有角色、A/B政策、M2十履约标识、库存10/净交付90/净过账7650。核对已知开发服务PID和项目命令后优雅停止。项目PG16 pg_dump与filestore归档写忽略runtime/backups唯一目录、目录700/文件600，记录hash/源码模块版本，无凭据进入日志或公共制品。源数据库不删除/不覆盖，M4完整恢复验证尚未做需如实记录。

先装Bridge并校验UI1.3数据库安装版本/表字段/视图就绪，使用feature源码启动8069（main暂未包含Bridge时禁止旧main重载新模型）。profile两次、flow两次输出报告各自相同，M2完成流程只读复验和原七角色HTTP/浏览器继续通过；然后真实执行新HTTP和浏览器五角色脚本，保留安全日志/JSON/PNG。异常不自动重设库存/角色或回滚删业务，先定位问题。

- [ ] **Step 2: CI串联与确切提交证据**

CI现有M2完整履约后安装Bridge到CI开发库，profile两次、flow两次cmp，原M2HTTP/浏览器保留，新HTTP/浏览器随后执行。各阶段日志明确，20分钟不足时基于实际时长适当扩展或拆分验证，不省略业务用例。制品白名单只增加安全报告/PNG/log，不上传凭据、cookies、备份或runtime通配目录。

根代理检查确切head的push/PR四任务和下载制品，核对Native总数、两次相同报告、五角色DOM/业务事实及M2黄金事实。修复引入变更必须复验必要项，CI和review最终匹配精确版本。

- [ ] **Step 3: 最终交付**

完成角色流程、实际环境、源码审查与CI后才ready/mergePR13，关联Issue6逐项证据关闭；同步main精确合并树、主源码启动、再做已有完成流程/HTTP读验，保留worktree/evidence。README写清框架源码固定下载、项目级依赖、按顺序安装/准备/运行、私有凭据位置、自动处理需明确开启cron和线程的条件。更新真实个人项目简历及导出，仅写已验证能力，人工UAT/M4恢复性能仍单独声明未完成并继续推进。
