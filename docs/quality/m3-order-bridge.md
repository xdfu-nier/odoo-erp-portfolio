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

本阶段只新增测试，已存在的接收锁与幂等实现首次真实竞争即通过，没有新增生产逻辑，不以人为移除正确实现制造RED。UAT-016的“唯一任务”部分已有证据，“一张销售单”仍须工作器完成；领取竞争、失败处理、人工重试与提交前退出恢复尚未执行。

## 接收阶段的实际 GitHub CI

提交`7b57e8b7816925f9e0ed2cc2581886483c8fc3ef`的[PR工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41613021778)及[push工作流](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41613011967)共四个任务成功。根代理下载PR白名单制品到`.runtime/m3-ingress-ci`，共21个文件；实际Linux为**486项post-tests零失败/零错误**，2026-11-03 09:48:59 UTC结束，299.49秒、400129次查询。

原生验收准备两次及履约两次报告各自字节相同，HTTP/浏览器报告`ok=true`，七张PNG签名有效；库存10、净交付90、净过账7650对平。此远程提交包含单事务接收，不包含后新增的并发测试`ada102c`，后者发布后须核对新CI。
