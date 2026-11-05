# M3 外部订单接入验证记录

状态：开发中。需求 REQ-009—012，验收 UAT-014—019、023—024；M3 未完成。

## M3a 精确载荷与业务摘要

2026-11-03，分支 `feat/order-bridge`，代码提交 `a6b8f91`。基线来自 M2 合并提交 `6809203`，运行环境沿用项目内 Python 3.12，无新增依赖。

- 根代理先实际执行 28 项失败测试，均因解析器尚不存在而失败；不是导入或环境错误。
- 实现后根代理完整 Python 辅助测试 83 项通过，其中新增解析契约 28 项；既有 Node 验收工具 27 项通过，相关 Ruff 检查通过。
- 核对业务等价输入摘要相同、业务变更摘要不同；数量/价格/折扣精确规范化；行顺序、无效尾零和传输信息处理；结构/长度/UTF-8 字节限制及错误不回显输入；独立规格和质量审查通过。
- 本地忽略证据为 `.runtime/m3a-red-tests.log`、`.runtime/m3a-green-python.log`、`.runtime/m3a-green-node.log`。

这一步只验证无 Odoo 依赖的载荷解析；不表示持久化或幂等已经实现。

## M3b 来源配置与唯一映射

2026-11-03，模块 `portfolio_order_bridge` 版本 `18.0.1.0.0`；固定 Odoo 源码与项目内 PostgreSQL，专用测试库 `odoo_portfolio_m1_test`。开发库既有进销存单据不参与该测试。

- 根代理实际 RED 为 31 项 post-tests 因模型未实现失败，零测试发现/安装错误，记录 `.runtime/m3b-config-red-native.log`。
- 首轮真实角色验证发现多对多授权字段部分查询绕过原生字段组检查；增加调用方查询字段预检，并覆盖普通、点路径、any、排序与分组反例。另修复原生关系默认过滤归档用户造成的配置校验漏检。测试中不以 `.sudo()` 成功代替普通角色验证。
- 质量审查发现继承的 public exists 可经 RPC 探测无授权 ID；真实方法分发门禁回归先有 12 个子场景失败（`.runtime/m3b-exists-red.log`），再将三个自定义模型上的该 helper 限于内部调用。内部验证及其他原生模型的 RPC 方法保持可用。
- 聚焦最终验证 `.runtime/m3b-config-final-native.log`：**32 项 post-tests、零失败/零错误**，15.16 秒、34699 次查询。包括实际角色/公司边界、来源和映射数据库唯一键、归档键仍占用、默认值注入、方法自身批量回滚及 exists 分发限制。
- 根代理最终完整回归 `.runtime/m3b-config-final-full-native.log`：**444 项 post-tests、零失败/零错误**，2026-11-03 08:48:58 UTC 结束，238.01 秒、430203 次查询。其中既有进销存 412 项、新增配置 32 项；不用含生命周期钩子的统计项冒充测试用例数。
- 根代理辅助回归 83 项 Python、27 项 Node 和完整 Ruff 检查通过；日志 `.runtime/m3b-config-final-v2-python.log`、`.runtime/m3b-config-final-node.log`。独立规格、质量以及 CI 同时选择两个模块的参数变更审查通过。

本节统计仅覆盖来源与映射。接收任务的后续验证单独记录；生成报价、调度与重试界面、独立事务并发及恢复、完整导入 HTTP 尚未实现，相关 UAT 不据来源测试标记通过。设计与后续任务见 [M3 设计](../superpowers/specs/2026-11-03-order-bridge-design.md)、[来源映射计划](../superpowers/plans/2026-11-03-order-bridge-m3b-config.md) 和[持久化接收计划](../superpowers/plans/2026-11-03-order-bridge-m3b-ingress.md)。

## 来源映射阶段的实际 GitHub CI

公开提交 `9280d2b13a5bd4f432bc1bd464606f1d610bc554` 已发布到草稿 [PR #13](https://github.com/xdfu-nier/odoo-erp-portfolio/pull/13)。[push 工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41606433644) 和 [PR 工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41606707063) 的 scaffold/native-odoo 共四个任务全部成功。

实际下载 PR 工作流白名单制品到本地 `.runtime/m3-config-ci`，共 21 个文件：Linux 原生结果为 **444 项 post-tests、零失败/零错误**，2026-11-03 09:01:34 UTC 结束、291.58 秒、390012 次查询。验收数据准备两次字节相同，原生履约两次报告字节相同；七角色 HTTP 与浏览器报告 `ok=true`，七张 PNG 文件头有效。结果仍为库存 10、净交付 90、净过账人民币 7650。

这一远程结果仅覆盖该公开提交的载荷与来源映射阶段。接收任务代码须独立完成原生验证、审查和其对应提交的GitHub CI，不沿用旧提交的成功结论。

## M3b 持久化接收与单事务幂等

2026-11-03，模块 `18.0.1.1.0`，专用测试库 `odoo_portfolio_m1_test`。

- 35 项初始接收测试实际因缺少任务模型失败（`.runtime/m3b-ingress-red.log`），再实现获授权来源接收、持久 pending 任务、同键同摘要复用及异摘要冲突、服务器设置全部审计身份、数据库唯一键和方法自身保存点。
- 真实升级发现原有 Python mixin 的隐含实例字典与 Odoo 模型扩展布局冲突；仅为两个 Python mixin 声明空 slots，保留原生 `_inherit` 和已有权限。
- 规格审查要求私有 SQL 锁、配置读取及结果辅助方法也独立校验实际身份/来源/公司。六项反例实际出现36个失败子场景（`.runtime/m3b-ingress-capability-red.log`）后补齐能力检查。质量审查发现规范行排序会误报原始输入行号；价格/折扣两项反例先失败（`.runtime/m3b-ingress-path-red.log`）后改用已验证的原始顺序校验，业务摘要不变。
- 最终接入模块74项 post-tests（配置32、接收42）零失败/零错误，21.15秒、45185次查询，`.runtime/m3b-ingress-final-addon.log`。
- 最终完整 **486 项 Odoo post-tests、零失败/零错误**，2026-11-03 09:37:43 UTC结束，233.86秒、451028次查询；`.runtime/m3b-ingress-reviewed-full-native.log`。83项Python、27项Node及完整Ruff检查通过，独立规格与质量审查通过。

接收不创建销售/库存/会计单据，也不提交调用方事务。缺失映射可保存为pending，商品单位精度由后续处理器验证。本节只覆盖单事务；独立事务证据见下一节，工作器生成“一张销售单”、失败重试与退出恢复仍保持未执行。

## M3b 独立事务并发接收

2026-11-03，严格限定专用库 `odoo_portfolio_m1_test`，UUID命名的隔离公司/角色/来源；普通Settings维护来源，普通Integration提交，既有货币功能组及价目表默认配置保持原样。测试清理先归档来源，再归档其账号，不删除不可变任务。

根代理实际运行4项测试，包含4次竞争与3个独立事务的公司/来源分离接收。每次竞争均由独立第三连接观察 `pg_stat_activity.wait_event_type=Lock` 和 `pg_blocking_pids` 的真实阻塞关系，第一事务提交后，等待事务实际收到 SQLSTATE `40001`，rollback并在全新游标/事务重试。

同载荷与等价数值/行顺序/传输字段变化均复用原任务；数量和价格变更均返回冲突。活库由新事务核对仅一个pending任务，原始JSON/摘要/提交人/创建与修改审计保持不变，尝试次数0、无销售单。相同外部单号在不同授权来源/公司分别生成任务，单公司账号只见本公司授权任务。

- 聚焦 `.runtime/m3b-ingress-races-native.log`：4项post-tests零失败/零错误，2.10秒、3532次查询；四次竞争均有Lock/40001/新txid证据。
- 完整 `.runtime/m3b-ingress-races-full-native.log`：**490项post-tests零失败/零错误**，2026-11-03 10:00:55 UTC，243.00秒、459508次查询。83项Python、27项Node及Ruff通过，独立规格/质量审查通过。

本节只新增测试，已存在的接收锁与幂等实现首次真实竞争即通过，没有新增生产逻辑，不以人为移除正确实现制造RED。本节只覆盖UAT-016的“唯一任务”部分；工作器、重试及退出恢复的后续证据分别记录，不能由接收测试单独代替。

## 接收阶段的实际 GitHub CI

提交`7b57e8b7816925f9e0ed2cc2581886483c8fc3ef`的[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41613021778)及[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41613011967)共四个任务成功。根代理下载PR白名单制品到`.runtime/m3-ingress-ci`，共21个文件；实际Linux为**486项post-tests零失败/零错误**，2026-11-03 09:48:59 UTC结束，299.49秒、400129次查询。

原生验收准备两次及履约两次报告各自字节相同，HTTP/浏览器报告`ok=true`，七张PNG签名有效；库存10、净交付90、净过账7650对平。此远程提交包含单事务接收，不包含后新增的并发测试`ada102c`，后者发布后须核对新CI。

## 真实并发阶段的实际 GitHub CI

提交`9c047f09ae45ebce2aefa29267dd60df1d446878`的[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41752911985)及[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41752903425)四个任务成功。下载PR制品到`.runtime/m3-races-ci`并核对21个白名单文件：Linux **490项post-tests零失败/零错误**，2026-11-04 01:24:17 UTC结束，298.20秒、403363次查询。

Linux日志实际记录四次第三连接阻塞观察、四次SQLSTATE40001、新txid及各一次重试；数量/价格冲突保留原任务，等价内容和同内容复用任务。准备/履约两次报告各自字节相同、HTTP和浏览器`ok=true`、七张PNG签名有效，库存10/净交付90/净过账7650对平。工作器、重试和退出恢复代码尚在后续开发，不由此CI结论代替其验收。

## M3c 原生报价工作器与人工重试服务

2026-11-04，模块`18.0.1.2.0`，严格专用测试库；先实际24项缺少工作器的失败，再实现普通Operator+基础Sales创建原生草稿报价。明确Sales负责人、实际Operator创建人、公司/仓库/CNY价目表、原始商品顺序、单位粒度及数量/价格/折扣、免税金额对平；15%折扣的报价由Sales确认时仍被M1审批阻止。处理器不确认、不采购、不发货或开票。

业务创建保存点内注入第二条真实销售行创建后的错误，报价/行/关联业务写入全部回滚，任务和不可变失败尝试留存；未知错误及数据库瞬态向外传播并恢复pending。缺失映射不自动创建主数据；Settings修复后Operator带原因重试，同任务第2次尝试成功，保留failed/retry/done全部审计，重复处理done及重复接收不产生新报价。

权限覆盖实际UID、保留sudo、他人来源与公司、默认字段/状态注入、不可变审计公开CRUD、原始载荷授权撤销后读取。规格审查补全“全部销售单据”组的配置及运行时拒绝：实际2项RED后共享forbidden组增加All Documents，不授予all-leads/经理权限。

质量审查发现接收/工作器真实外键死锁环：接收持有来源行、任务插入等待公司FK key-share，工作器持有公司FOR UPDATE后等待来源。新增确定性双事务反例实际复现PostgreSQL **40P01**（`.runtime/m3c-worker-fk-deadlock-red.log`）。修复为授权后统一**公司→来源**锁序，实际同一用例通过，有第三连接阻塞观察和预期**40001**新事务重试、无40P01，两个任务审计不变（`.runtime/m3c-worker-fk-deadlock-green.log`）。此证据不代替之后的实际进程退出恢复。

最终根代理接入模块**103项post-tests零失败/零错误**，40.18秒、77201次查询（`.runtime/m3c-worker-final-addon.log`）。完整**515项post-tests零失败/零错误**，2026-11-04 05:27:29 UTC结束，304.63秒、508102次查询（`.runtime/m3c-worker-fk-reviewed-full-native.log`）。83项Python、27项Node、完整Ruff及独立规格/质量复审通过。

本节提供UAT-014/017单任务报价、幂等和修复重试的原生自动化证据；真实领取/退出恢复及远程CI的后续结果分别记录，不由本节的单事务测试代替。定时任务/界面、隔离本地HTTP与最终M3发布仍待完成。

## 工作器服务的实际 GitHub CI

提交`1762e3aac03278f923b063cdf76767f51898a072`的[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41780055446)及[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41780050460)四个任务成功。根代理下载到`.runtime/m3-worker-ci`核对21个白名单制品；Linux **515项post-tests零失败/零错误**，2026-11-04 05:40:57 UTC，246.99秒、430323次查询，接收/工作器竞争仅预期40001新事务重试，无40P01。

准备/履约两次报告各自字节相同、七角色HTTP与浏览器`ok=true`、七PNG签名有效，库存10、净交付90、净过账7650对平。该提交不包含下节新增的真实子进程恢复用例。

## M3d 工作器竞争与实际进程退出恢复

2026-11-04，新增测试验证已有实现，无生产逻辑更改。严格专用测试库、已提交UUID夹具、普通Settings/Integration/Operator与独立Sales负责人、项目内Python，清理先归档来源再归档账号，其他公司默认价目表/功能组保持原样。

1. 两个真实工作器事务由第三连接观察锁等待；实际40001后新游标重试，最终唯一done任务、报价和成功尝试，原始接收审计不变。
2. 7个pending任务中，独立只读技术游标锁住最早2个任务行；实际工作器在释放前处理其后5个，释放后完成另2个，最终7张唯一报价，验证SKIP LOCKED候选选择。
3. 原生失败、修复映射后两个独立事务竞争人工重试，仅一个有效retry事件，另一请求在新事务因已pending拒绝；随后唯一报价、总尝试2次，保留失败与原始接收记录。
4. 真正的项目Python子进程以普通Operator创建并flush报价及2条原生销售行后，写0600安全见证并`os._exit(73)`，未执行事务提交或Python清理。父测试核对实际退出73和见证ID；全新Integration/Operator事务确认任务pending/次数0，未提交报价、行及尝试不存在。新事务处理仅生成一张已提交报价，一个done事件，重复处理不增单。

退出探针只接受严格测试库、项目原生配置/loopback、项目runtime/tmp下不存在且无符号链接的见证路径；无效目标在Odoo导入前拒绝。子进程无HTTP/cron，argv不带密码，stdout/stderr不透传，超时会kill/wait并清理，见证仅ID/阶段/布尔值。

根代理聚焦 `.runtime/m3d-worker-concurrency-native.log`：4项post-tests零失败/零错误，8.13秒、11369次查询，真实退出73、Lock/40001均有日志。完整 **519项post-tests零失败/零错误**，2026-11-04 05:52:33 UTC结束，309.65秒、527431次查询（`.runtime/m3d-worker-recovery-full-native.log`）。92项Python、27项Node、Ruff及独立规格/质量审查通过。

UAT-016—018/024的上述服务端自动化场景有真实事务/进程证据；新提交的GitHub CI还需单独核对。cron、界面、隔离本地样例及认证HTTP/浏览器、M3最终交付和M4仍未完成；完整人工UAT仍单独记录。

## 实际进程恢复版本的 GitHub CI

提交`7de23ddde4c2c9a8c5684d0e63db55dc18dc2e11`的[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41782798413)及[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41782792536)四个任务成功。下载PR制品到`.runtime/m3-recovery-ci`核对21个白名单文件；Linux **519项post-tests零失败/零错误**，2026-11-04 06:06:15 UTC，176.43秒、440780次查询。

实际日志记录退出73前已flush报价及两行，父测试复验回滚与唯一恢复；工作器/重试竞争和7任务SKIP LOCKED均在此CI实际执行。准备和履约两次报告各自相同、七角色HTTP/浏览器`ok=true`、七PNG签名有效，库存10/净交付90/净过账7650对平。界面与cron代码仍在后续阶段，本提交不证明尚未发布的新界面。

## M3d 原生角色操作界面、重试向导和停用计划

2026-11-04，模块`18.0.1.3.0`，代码`3ba2359`。Settings维护来源/客户及SKU映射，Integration/Operator只读任务/尝试，处理账号通过动作处理本队列、打开原生报价或带原因重试；其他销售账号不获得导入原始载荷菜单。

实际原生Form覆盖新来源公司/代码可输入且保存后只读、映射来源预填/目标修复、readonly默认job带force_save的原因向导创建和apply。未保存NewId预览也核对当前实际Operator/failed任务，已有origin继续核对创建人。向导读取、id-only web_read、查询/计数/原因分组、导出、修改和apply统一检查当前处理资格与来源/公司/创建人，实际uid1的原生autovacuum保留。

Settings明确准备原生ir.cron，固定Job模型/代码、来源专用Operator、1分钟周期且默认inactive；重复准备复用原ID并保留当前启用状态，错误模型/代码/账号/间隔或跨来源关联拒绝，不接管他人cron。Company→Source锁序与外键协议一致，关联字段禁止直接/默认值注入。原生server action以cron.user_id实际运行并生成Operator创建的报价，无全局cron管理权限授予；项目max_cron_threads=0保持。

初始28项实际RED后实现；规格审查新增私有计划校验和向导查询资格的两个反例，实际13个失败子场景后补齐。最终**30项UI post-tests零失败/零错误**，13.69秒、22784次查询（`.runtime/m3d-console-capability-green.log`）。完整**549项post-tests零失败/零错误**，2026-11-04 06:56:13 UTC，343.13秒、566194次查询（`.runtime/m3d-console-reviewed-full-native.log`）。92项Python、27项Node、Ruff及独立规格/质量审查通过。

这些是原生视图、表单和动作服务验证，不代替真实浏览器操作或完整人工UAT。隔离本地角色数据、认证HTTP/浏览器、当前新提交CI与M3最终交付仍待完成。

## 本地隔离 profile 准备器

2026-11-04，代码`22765de`：显式allow-local、实际loopback和两库白名单、实际Settings维护入口；只创建独立namespace的三位新角色、两件新商品/模板/供应商、专属客户/供应商、来源/映射及停用cron，共17个标识。M1的19个原始标识、A/B角色及现有库存/配置保持，M2已标记CNY价目表只读复用；不创建任务、报价、库存或财务单据。

来源/映射/cron业务配置实际由新普通Settings执行，Operator只有基础Sales与处理组，Integration无业务权限，三者单公司/inbox/.invalid。原生技术UID1默认inactive，准备器不激活它；仅在实际UID1+su维护路径允许bootstrap，其余inactive用户与Sales.sudo仍拒绝。权限精确比较包含Odoo为inbox通知自动添加的原生组，没有扩大业务权限。

独立600三角色凭据文件只原子创建一次，旧七角色文件不变；CLI无效目标/配置/路径在Odoo导入或凭据创建前拒绝，stdout仅安全稳定报告。profile自己的保存点、命名空间技术锁394与完整标识/角色/目标/密码hash只读验证，未知同名或缺失/被修改数据拒绝，不接管、不重建或覆盖用户设置，重复不调用任何配置create/write/cron准备动作。

纯22项及原生22项先实际RED；最终聚焦22项post-tests零失败/零错误，65.58秒、123970次查询（`.runtime/m3-local-profile-native-green-v3.log`）。完整**571项post-tests零失败/零错误**，2026-11-04 08:11:33 UTC，428.66秒、700804次查询（`.runtime/m3-local-profile-full-native.log`）。114项Python、27项Node、Ruff与独立规格/质量审查通过。

全量纯测试发现临时目录已按项目规范指向runtime/tmp，已将越界路径反例改为独立且确定的目录，生产允许范围保持。开发库尚未安装/运行此profile；完整导入流程、真实角色HTTP/浏览器及最终CI仍待后续验证。

## 界面与静态 profile 的实际 GitHub CI

界面提交`5e304eabc53264041b810c39a0c420ba2cbbe605`的[PR](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41792345076)与[push](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41792337112)四个任务成功；实际制品下载`.runtime/m3-console-ci`，Linux549项post-tests零失败/零错误，2026-11-04 07:35:44 UTC，351.06秒、463048次查询。

profile提交`934a752702480e86dd0431a696f26c20c4dd5cb5`的[PR](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41799090046)与[push](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41799081607)四个任务成功；制品`.runtime/m3-profile-ci`，Linux571项post-tests零失败/零错误，2026-11-04 08:30:32 UTC，296.34秒、552126次查询。

两版本各21个白名单文件均已核对：原七角色HTTP/浏览器`ok=true`、七PNG签名有效、准备及履约各两次报告字节相同、库存10/净交付90/净过账7650对平。此CI原生测试证明profile准备器，完整M3导入角色本地HTTP/浏览器仍在后续阶段，不据既有M2七角色页面提前宣布通过。

## 本地真实角色导入流程与只读复验

2026-11-04，plain非RPC本地流程严格要求已存在的静态17标识和既有600凭据，不隐式建账号或生成新密码。动态namespace独立九个标识：两任务/两报价/四个尝试事件及一条精确repair映射。

普通Integration真实公开提交/重复提交返回同任务；普通Operator公开动作处理第一单为done，创建人Operator、负责人既有M1Sales、原生draft且15%折扣确认由Sales收到准确审批业务拒绝。第二单保留原始顺序，缺失别名行失败且无报价，failed事件先留存；新Settings建立精确映射后Operator通过公开原因向导apply，再处理为done/尝试2，保留failed/retry/done历史。无确认、审批结论、库存/采购/财务写入。

完成后重复仅只读复验九标识/原始载荷及摘要/原生报价数量价格折扣金额/创建与负责角色/尝试时间、原因和映射快照，不再调用任何提交、处理、重试、向导或映射写入。两次报告相同。静态profile只接受这套完整九标识内唯一同源/同公司/第二自有商品/实际Settings创建的repair映射，仍拒绝未标记、部分或其他额外映射，无递归profile→flow→profile。

专用Operator如果还被用于任何其他来源，流程在固定批次之前拒绝，避免处理用户额外队列。已有source额外任务、商品销售/库存/采购/会计历史、缺失/重指/跨公司/原始审计或原生报价被改均拒绝，不修复覆盖。自己的保存点与独立锁395保证未知异常在第一张真实报价/第一次尝试后发生也不留下半个流程；静态17、M1全部19及finance/历史/当前轴承库存保持。

先实际13项纯CLI和24项原生RED。修正入口先净化调用方非法语言再查角色，持久损坏反例以确定的测试SQL值核对后执行，不放宽业务模型。最终聚焦46项post-tests零失败/零错误，184.45秒、278499次查询（`.runtime/m3-local-flow-native-green-v2.log`）。完整**595项post-tests零失败/零错误**，2026-11-04 09:23:57 UTC，529.24秒、866257次查询（`.runtime/m3-local-flow-full-native.log`）。127项Python、27项Node、Ruff及独立规格/质量审查通过。

开发库备份安装、实际三位新角色的HTTP/浏览器页面、对应新CI及最终M3交付尚未执行；这些结果不由原生测试提前代替。

## 本地流程版本的实际 GitHub CI

提交`25db52ffd8df308d4259aba3d2eaf5f5f71da3b4`的[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41808332124)与[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41808339442)四个任务成功。根代理下载PR白名单制品到`.runtime/m3-local-flow-ci`并核对21个文件：Linux **595项post-tests、零失败/零错误**，2026-11-04 09:41:17 UTC结束，363.72秒、663035次查询。原M1准备及M2履约各两次报告字节相同，七角色HTTP/浏览器通过，七张PNG签名有效，库存10/净交付90/净过账7650对平。

该版本CI覆盖新增导入流程的原生测试；当时CI开发库尚未安装Bridge，不能据原M2页面报告推断M3浏览器通过。

## 开发库保护、安装和两单实际导入

2026-11-04，根代理先只读记录开发库`odoo_portfolio`的M1十九标识、M2十履约标识、原七角色密码/组/公司/邮件配置的私有摘要、A/B政策及黄金业务值。核对原服务的项目命令后优雅停服，在8069无监听、数据库无其他连接时使用项目内PostgreSQL16工具创建custom dump与filestore归档。

备份目录`.runtime/backups/m3-pre-bridge-20261104T100207Z-f6bf21c3`权限700；数据库文件5,190,910字节、filestore归档2,945,871字节/870文件，文件及manifest权限600、记录SHA256。备份前后基线一致，备份不上传公共仓库或CI制品。**该备份尚未做恢复演练**，manifest保留`restore_verified=false`，恢复验证仍属M4。

开发库随后实际安装`portfolio_order_bridge 18.0.1.3.0`，既有trade保持`18.0.1.5.0`，使用feature工作树源码运行8069，避免尚不含Bridge的main源码加载已升级数据库。三位独立角色的静态profile准备两次、导入流程运行两次，各自报告字节相同。实际结果为两任务done、两张原生draft报价309/161.50元、四条不可变尝试历史；第二单缺失映射失败、Settings修复、Operator带原因重试成功，创建人与负责销售分别保持Operator/M1 Sales。

安装后、流程后及下述HTTP核对后均比较原基线：十九标识、十履约标识、原七角色摘要和A/B政策不变；库存10、毛交付100、退回10、净交付90、净过账7650不变。私有证据位于主项目`.runtime/m3-development-before-upgrade.json`、`m3-development-after-install.json`、`m3-development-after-http.json`以及`m3-development-{profile,flow}-{first,second}.json`。

## 真实RPC分发返回契约

实际HTTP揭示普通Python权限mixin的`search/search_fetch`仅有`@api.model`时，Odoo元类未继承记录集返回元数据，直接Python调用正常但JSON-RPC将结果编码为字符串。提交`5bedbebd5246ac26ebd3f2fb2113b0288fb28dc9`为这两个方法显式声明原生`@api.returns("self")`及`@api.readonly`，权限检查与业务逻辑保持。

新增五项原生`api.call_kw`测试覆盖六个导入模型的真实RPC正整数ID列表、空结果、直接ORM记录集、角色/公司/sudo/私有字段反例及无关原生模型。根代理先实际RED五项/50个子场景失败，再聚焦五项通过。完整**600项post-tests、零失败/零错误**，2026-11-04 11:28:03 UTC结束、605.92秒、887220次查询，`.runtime/m3-rpc-contract-full-native.log`；独立规格与质量审查通过。

## 开发库五角色实际认证HTTP

提交`ab00e450bdf40a62745dd3e60a52c9d01dc815ac`包含独立标准库HTTP核对器和契约测试；根代理实际RED后实现，最终35项聚焦/162项完整Python测试、27项既有Node及Ruff通过，独立规格/质量审查通过。真实运行安全报告为主项目`.runtime/m3-development-http.json`，`ok=true`，Settings/Operator/Integration/M1 Sales/B公司销售五个独立会话的全部固定检查通过（分别16/16/17/16/15项）。

实际检查登录UID/单公司/真实组、原生菜单动作和视图、来源与映射、停用cron、两任务/四事件、报价商品/单位/折扣/税与金额；Integration同内容重放复用原任务/报价，Operator处理已完成队列返回0，Sales确认仍被审批业务规则拒绝。直接RPC的越权处理/重试、原始载荷与配置读取、不可变审计写入、id-only探测/导出/查询及公司上下文伪造均按预期拒绝；每组反例后同会话健康读取和独立观察者复核业务未变。网络故障、会话失效、HTML/未知异常不计为权限通过。

真实Odoo导出禁用返回明确UserError，核对器只接受固定原生错误且同步验证System/ERP/Allow Export组均未授予；报价表单金额核对原生只读`tax_totals` widget，列表与模型继续验证`amount_total`。这些原生兼容修正未增加权限或修改业务模型。

HTTP后profile及flow再次各运行两次，报告与HTTP前留存版本均字节相同；最终仍为两任务、两草稿报价、四事件、309/161.50元、原十九标识及库存10。**真实浏览器五角色、对应最终CI及M3合并尚待完成**；上述服务端通过不替代页面或人工UAT证据。

随后根代理在同一开发库、当前feature服务复跑既有M2七角色HTTP及真实Chrome页面：两个报告均`ok=true`，七张固定PNG签名有效，并实际查看财务履约追踪截图中的100/10/90数量及7650净过账。证据为主项目`.runtime/m3-m2-http.json`和`.runtime/m3-m2-browser/`。新增两张导入报价未破坏旧流程页面；这仍不代替新增五角色页面验收。

2026-11-05浏览器前置只读HTTP核对确认：Operator与Sales实际均无原生折扣显示、单位及多公司功能组。浏览器验收沿用这些真实角色；折扣列隐藏时以可见数量/单价/小计计算折扣，明确区分直接可见与计算证据，并要求HTTP原生行折扣/单位已经通过。不会将单位列隐藏记为可见，也不会为截图修改原七角色或启用全局显示功能。此为验收设计调整，新增五角色浏览器本身仍待实际运行。

## 开发库五角色真实浏览器

2026-11-05，浏览器核对器`8d336bd2264cb6b77751a8f26b92883cfa9b20c3`及原生Notebook稳定等待修正`0bb3ef5a1a167a3fe4aab6855968631bae05c7f1`。根代理先实际17项缺少核对器的RED，再实现；独立规格/质量审查及原生运行发现后，补齐Integration标签、列表与表单分别检查处理按钮、完整禁止菜单、原生无XMLID配置标题、Ace可见代码、只读列表搜索范围和页签动画等待，各自有实际RED/GREEN及复审记录。

最终**55项Node契约测试（原有27、新增28）通过**，162项Python与Ruff通过，未修改业务模型、角色或原生视图。实际只读列表允许原生搜索框，但拒绝业务行中的可编辑输入；截图等待当前选中页签及其pane完全呈现，不以固定sleep或仅active样式作为就绪条件。

根代理使用已安装Chrome和五个独立browserContext，经可见原生登录表单登录，完成**全部19页**：Settings来源/客户映射/三商品映射/默认停用专用计划4页，Operator两任务/两只读接收审计/四事件列表/两报价7页，Integration任务/接收审计/事件5页，Sales两原生报价2页，B公司销售空列表1页。最终报告`ok=true`，19张白名单PNG签名全部有效。报文、规范载荷及摘要先检查只读/非空，再在截图中遮盖，不写入安全JSON。

实际DOM核对done/次数1与2、Integration接收及创建人、Operator事件执行人、失败/带原因重试/成功历史，native Metadata中的报价创建人为Operator、负责Sales，draft金额309/161.50元、来源/商品/数量/单价/税/小计和审批入口；菜单与处理按钮符合各角色边界，B公司只见原生空状态，sample占位行不计业务记录。折扣/单位显示按上节明确区分页面可见与HTTP证据。

安全证据位于主项目`.runtime/m3-development-browser/report.json`及19张PNG。根代理逐项核对报告/文件头，查看19页缩略总览及关键原尺寸图；实际失败修复历史和草稿报价的虚构数据截图已收录[本地操作说明](m3-local-operations.md)。浏览器后只读SQL基线与安装前比较，原十九/十标识、原七角色摘要、A/B政策及库存10/毛交付100/退10/净交付90/净过账7650均未改变。**对应最终GitHub CI及PR #13合并仍未完成**；M4恢复/性能和人工UAT继续分别追踪。
