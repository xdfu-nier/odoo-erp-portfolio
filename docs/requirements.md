# 需求范围与资料记录

## 已确认范围

面向中小贸易企业的进销存 ERP，复用 Odoo 标准销售、采购、库存与开票能力，自主开发审批、外部订单导入、公司隔离及业务追溯。

当前阶段按用户要求先创建项目和公开 GitHub 仓库，依赖全部安装在项目内，默认源码开发方式。

## 可追踪的公开参考

OCA 的 [Sale Tier Validation](https://github.com/OCA/sale-workflow/tree/18.0/sale_tier_validation) 已阅读，其公开工作流包括触发审批条件、申请审核、查看审核状态、全部通过后确认销售订单。这可以作为审批设计的社区实践参考，不代表某个客户的内部需求。

已通过 Odoo 官方 sitemap 发现下列真实客户案例页面，但当前站点拒绝正文访问，尚未将页面标题推断为具体业务需求：

- [Murray Wholesale](https://www.odoo.com/blog/customer-reviews-6/murray-wholesale-runs-8-000-products-smoothly-with-odoo-1770)
- [KUCHT](https://www.odoo.com/blog/customer-reviews-6/kucht-integrates-sales-inventory-and-purchasing-with-odoo-to-improve-real-time-visibility-2389)
- [Banayed Trading](https://www.odoo.com/blog/customer-reviews-6/from-tools-to-transformation-banayed-trading-s-journey-with-odoo-1911)

下一阶段先补充可读取的原始案例，再整理“案例事实 → 需求推导 → 自主实现 → 验收测试”的对应关系，不把作品集设计写成客户原话。

## 功能分工

| 范围 | Odoo 标准能力 | 作品集计划增加的能力 |
| --- | --- | --- |
| 销售 | 报价、订单、订单明细 | 阈值审批、审批后修改重新审核 |
| 采购 | 询价、采购订单、收货关联 | 采购补货演示和来源追踪 |
| 库存 | 收发货、分批处理、退货 | 跨单据进度展示与异常约束 |
| 开票 | 发票、退款单、订单关联 | 业务链汇总查询 |
| 系统集成 | 已认证 ORM 调用 | 幂等订单导入、重复载荷冲突处理 |
| 权限 | 标准角色与公司机制 | 自定义模型公司规则和接口访问验证 |

首版功能表是开发方向，具体规则待需求分析后形成验收标准；当前初始化提交不包含上述自主业务逻辑。
