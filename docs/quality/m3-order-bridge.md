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

本阶段尚未建立接收任务、生成报价、调度或重试界面。独立事务幂等与恢复、认证 HTTP 及 M3 GitHub 原生 CI 仍未执行；对应 UAT 保持未执行。设计与后续任务见 [M3 设计](../superpowers/specs/2026-11-03-order-bridge-design.md)、[来源映射计划](../superpowers/plans/2026-11-03-order-bridge-m3b-config.md) 和[持久化接收计划](../superpowers/plans/2026-11-03-order-bridge-m3b-ingress.md)。
