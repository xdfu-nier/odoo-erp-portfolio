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
