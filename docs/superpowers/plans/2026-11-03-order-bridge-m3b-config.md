# M3b 来源与映射 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已确认的外部来源、唯一主数据映射和最小权限配置落实到真实 Odoo 模型，作为任务接收的前置条件。

**Architecture:** 完整安装 `portfolio_order_bridge`，单向依赖 `portfolio_trade`。设置角色维护来源和映射；接入角色只能读取授权来源的最小标识；处理角色读取本人负责来源及其映射。模型显式核对实际 UID 和公司，原生 ACL/全局记录规则共同约束 RPC 与 ORM。

**Tech Stack:** 固定 Odoo 18、项目内 Python/PostgreSQL、标准 ORM/XML/CSV、原生 TransactionCase；无新依赖。

---

## 文件职责

- 新建 `addons/portfolio_order_bridge/__init__.py`、`__manifest__.py`、`models/__init__.py`：标准可安装模块与模型注册。
- 新建 `models/access.py`：实际用户/公司校验及公开读取路径恢复实际用户环境；设置维护检查。
- 新建 `models/import_source.py`：来源身份、CNY 配置、处理/销售/接入用户与配置有效性。
- 新建 `models/import_mapping.py`：来源下唯一客户/商品映射和关联公司检查。
- 新建 `security/order_bridge_security.xml`、`security/ir.model.access.csv`：角色、全局公司规则和来源授权规则。
- 新建 `tests/__init__.py`、`tests/test_import_config.py`：普通角色及异公司反例，直接 ORM 路径和原子性验证。
- 本任务不建立任务、处理器、调度或菜单；那些功能有后续独立测试和审查。

### Task 1: 原生来源、映射及授权

- [x] **Step 1: 测试先行与最小发现结构**

创建模块 manifest（`18.0.1.0.0`、LGPL-3、depends 仅 `portfolio_trade`、installable true）和测试包，初始不注册业务模型或加载不存在的安全文件。该结构只用于让原生测试发现失败测试。每个测试 setUp 先断言三个模型已注册，再建立来源：

```python
def setUp(self):
    super().setUp()
    for name in ("portfolio.import.source", "portfolio.import.customer.map", "portfolio.import.product.map"):
        self.assertTrue(name in self.env.registry, "M3 source/mapping model is not implemented")
```

使用 `@tagged('post_install', '-at_install', 'portfolio_import_config')`。setUpClass 只准备原生模型数据：独立 CNY 公司 A/B、价目表、仓库、客户、产品及用户，不访问尚不存在的接入组。接入组相关用户在模型存在断言之后建立。数据处于原生测试事务，不提交，不修改开发库或全局默认价目表。

测试清单必须落为独立或表驱动断言：

1. Settings 可建立有效来源及公司/共享客户、商品映射；返回字段与公司/关联对象正确。
2. 源身份、名称和外部映射键为非空无首尾空白/控制符/代理字符的有界字符串；来源公司代码/来源代码/外部客户和 SKU 最大 64 字符。
3. 外部公司代码 + 来源代码全局唯一；同一公司 + 来源代码唯一；客户/商品映射分别来源 + 外部代码唯一，归档记录也占用键；不同来源可复用代码。
4. 来源公司、外部公司代码、来源代码创建后不可变；禁止 public copy、unlink，允许 Settings 归档。映射公司为来源派生且不可伪造/跨来源挪动，关联客户/商品可由 Settings 修复，映射禁止 copy/unlink、允许归档。
5. 来源只接受公司本位币 CNY、有效 CNY 价目表、同公司有效仓库；价目表允许共享或同公司，其他公司/币种/失效对象拒绝。
6. 负责销售必须有效、内部、实际 Sales 基础组且属于来源公司。处理用户必须有效内部用户、接入处理组和 Sales 基础组，单独且仅属于来源公司，与负责销售不同；不得有 Settings/访问权管理/销售经理/采购/仓库/Billing 组。允许接入用户必须有效内部、接入组、属于来源公司，不能持有上述业务组（包含 Sales）。配置不自动修改任何用户组或公司。
7. 普通 Sales/经理/采购/仓库/Billing 不得读取来源或映射或执行维护；接入只读取自己被授权的来源，不获得映射、价目表、仓库及其他用户名单；处理用户只读取自己负责的来源及映射。Settings 可维护其实际且当前允许的公司记录。
8. 强制 context 伪造其他公司、普通用户 `.sudo()`、异公司源/客户/商品 ID、批量混合合法/非法写入、`default_company_id` 和 `default_source_id` 注入均不产生越权。当前 actor 公司被移除或来源授权被撤销后，旧记录集不得继续读取。
9. 检查 `search/read/web_read/search_read/search_count/read_group/export_data` 和显式 ID 的限制；映射和来源私有配置不能通过搜索域/排序/分组推断。客户/商品直接变为异公司或失效后，处理前验证拒绝该映射。
10. 使用标准 `unittest.TestCase.assertRaises` 捕获批量维护错误，flush/invalidate 后核对没有部分写入；不要用 Odoo assertRaises 自动保存点掩盖方法自身原子性。

- [x] **Step 2: 根代理实际原生 RED**

由根代理运行固定源码、根配置和独立测试库，记录 `.runtime/m3b-config-red.log`：

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache .upstream/odoo/odoo-bin \
  -c /Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.runtime/odoo.conf \
  --addons-path=/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.upstream/odoo/addons,/Users/fuxiaoda/Documents/personspace/resume/odoo-erp-portfolio/.worktrees/order-bridge/addons \
  -d odoo_portfolio_m1_test -i portfolio_order_bridge --without-demo=all \
  --test-enable --test-tags /portfolio_order_bridge:TestImportConfig --stop-after-init --http-port=8077 \
  --logfile .runtime/m3b-config-red.log
```

预期原生测试因模型未注册断言失败。模块安装/连接或测试发现失败均不算有效 RED；根代理排除环境问题后才解冻生产实现。

- [x] **Step 3: 模型和安全实现**

统一字段约定：

| 模型 | 字段/约束 |
| --- | --- |
| `portfolio.import.source` | name、active、company_id、company_code、code、pricelist_id、warehouse_id、sales_user_id、processor_user_id、integration_user_ids；_check_company_auto=True |
| `portfolio.import.customer.map` | active、source_id、company_id（stored related source company）、external_ref、partner_id（check_company=True） |
| `portfolio.import.product.map` | active、source_id、company_id（stored related source company）、external_sku、product_id（check_company=True） |

两组 XML ID 为 `group_import_integration`、`group_import_operator`，均仅 implied `base.group_user`，不隐式赋予其他业务权限；专用处理用户另外明确授予 Sales。配置有效性方法为 `source._validate_configuration()`，映射有效性方法为 `mapping._validate_target()`，后续接收/处理调用前会再执行，不能只依赖 create 时约束。

所有模型的全局规则为 company_id in company_ids。角色规则：Settings 本公司全部；来源的接入规则 integration_user_ids 包含实际用户，处理规则 processor_user_id=实际用户；映射仅 Settings/所属来源处理用户。接入对映射无 ACL。维护只允许实际 Settings 用户，源和映射均禁止 public unlink/copy；`company_id`、`company_code`、`code` 与 mapping.source_id 不可通过写入重新解释。

来源的 pricelist_id、warehouse_id、sales_user_id、processor_user_id、integration_user_ids 字段只对 Settings/处理组可读。接入不能用域、排序、分组推断这些字段；应显式检查涉及受限字段的公开查询参数，不把 field groups 当作过滤器权限。后续 submit 内部只使用已授权来源的最小有界读取，不授予接入原生业务模型权限。

维护入口在当前用户环境验证实际角色、实际公司列表和 allowed_company_ids；丢弃调用方 defaults/context 后执行内部业务写入。create/write 自身保存点覆盖整个批次，失败后即使调用方捕获异常，也不残留部分写入。所有授权以当前 UID 为准，`.sudo()` 不提升接入/处理/维护身份。原生客户端 id-only web_read 和带关系字段的导出同样校验。

- [x] **Step 4: 根代理 GREEN 和回归**

```bash
# 根代理沿用 Step2 命令，将 -i 改为 -u、日志改为 m3b-config-green.log。
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests
.venv/bin/ruff check scripts tests addons
```

聚焦原生测试通过后，由根代理运行 `/portfolio_trade,/portfolio_order_bridge` 完整原生回归；查看 post-tests 统计为零失败/零错误。纯辅助解析测试通过不代替模型和真实角色验证。

发布开发分支前，将 `.github/workflows/checks.yml` 原生安装参数从单独 `portfolio_trade` 改为 `portfolio_trade,portfolio_order_bridge`，测试标签同时选择两个模块。这样草稿 PR 的原生任务实际包含本阶段模型测试；既有七角色履约流程和制品白名单保持原样。此配置由根代理单独审查 YAML 与实际 GitHub 运行结果，不新建仅镜像参数的测试。

- [x] **Step 5: 两阶段审查、记录与提交**

先独立规格审查，再独立质量审查。修复必须补有意义的反例并重新执行相关原生验证。全部门禁通过后仅提交本任务文件，提交信息 `feat: configure authorized order sources and mappings`，记录实际测试数、模块版本和证据，再编写持久化接收任务计划。

实际结果：2026-11-03，最终 32 项来源/映射原生测试通过（`.runtime/m3b-config-final-native.log`）。质量审查发现继承的 public exists 可探测无授权 ID；先以真实 RPC 方法分发门禁测试观察 12 个子场景失败，再用仅限这三个模型的 `@api.private` 包装保留内部 helper，复验通过。最终完整 444 项 Odoo post-tests 零失败/零错误（08:48:58 UTC、238.01 秒、430203 次查询，`.runtime/m3b-config-final-full-native.log`），83 项辅助 Python、27 项 Node 和 Ruff 通过。独立规格、质量及 CI 参数变更审查均通过；GitHub 实际 CI 待发布分支运行。
