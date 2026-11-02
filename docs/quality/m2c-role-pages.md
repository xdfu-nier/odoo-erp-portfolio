# M2c 开发库与七角色页面验收：进行记录

本阶段分别留存原生业务测试、开发库实际运行、HTTP权限读取及自动化浏览器渲染证据。
浏览器自动化核对不等同于人工UAT；外部导入、备份恢复与性能验证仍在后续阶段。

## 版本与开发库流程

版本准备提交 `66d36ff3cbce6bb5abe3e6eff524380a18e1dc38` 将模块升级到 `18.0.1.5.0`。
独立测试库原生升级回归 **409项，0 failed，0 errors**：`2026-11-02 03:34:00 UTC`，196.36秒、384,035次查询；
`.runtime/m2c-1.5-upgrade-green.log`。

开发库 `odoo_portfolio` 升级前为1.4.0、19个验收标识、内部库存40、一次原始期初盘点，A/B履约均关闭。
升级仅使用原生模块升级，不运行测试或自动启用；升级后逐项比对标识、库存、盘点和策略，除版本外一致。
根代理随后执行只读初始历史预检，显式会计检查/准备（保持关闭）、发件身份准备、显式启用A，再运行完整流程两次。
两份流程报告逐字相同，最终库存10、毛交付100/退10/净90、实际商品分录净过账7650；19个身份和原始DONE40盘点保留，B公司策略未变。

安全证据保存在主项目 `.runtime/m2c-development-before-upgrade.json`、
`.runtime/m2c-development-after-upgrade.json`、`.runtime/m2c-development-finance-{check,prepared,enabled}.json`、
`.runtime/m2c-development-senders.json`、`.runtime/m2c-development-flow-{first,second}.json` 和
`.runtime/m2c-development-after-flow.json`。私有凭据不进入报告或GitHub产物。

## 财务与采购的只读销售表单

真实HTTP探针发现原生销售表单仍向Billing-only和Buyer-only用户提供确认、发送、取消、重置等按钮；财务表单没有明确edit=False。
新增3项原生视图回归，首轮测试还错误假定普通销售具有删除权限，仅修正该假设为实际原生unlink ACL。
干净RED **3项，9 failed，0 errors**：`2026-11-02 08:22:46 UTC`，0.79秒、1,005次查询，
`.runtime/m2c-sale-readonly-views-clean-red.log`。

最小修复按实际UID的Sales角色处理返回视图，非Sales的form/list明确create/edit/delete=0，并移除表头写入按钮，
保留追踪、原生预览及按原生Billing组过滤的准备发票入口；销售/经理/混合角色和表头外只读导航保留。
返回字典与XML均独立复制解析，不修改公共视图缓存，不改变业务ACL。
专项 **3项，0 failed，0 errors**：`2026-11-02 08:26:42 UTC`，0.94秒、1,030次查询；
完整 **412项，0 failed，0 errors**：`2026-11-02 08:31:13 UTC`，194.53秒、388,538次查询，
日志分别为 `.runtime/m2c-sale-readonly-views-focused-green.log`、`.runtime/m2c-sale-readonly-views-regression-green.log`。
提交 `57ed842b72ee5978528b7f71ada2e6159f1eee02` 已通过独立规格及代码质量审查；重启后的真实HTTP探针确认财务/采购均只读且无销售写入按钮。

## HTTP与浏览器核对

HTTP验证器最初的 `res.users.has_group` RPC未按记录方法传用户ID，真实请求触发TypeError；补充原生参数边界回归后修正。
仓库角色原生允许读取关联SO/PO，验证器不能将“禁止追踪”误写为“禁止原生单据读取”；修正后继续要求实际ID/公司/状态一致，
且B公司角色不能返回任何A公司单据。
七角色实际HTTP验证已通过：独立会话、用户与公司范围、菜单/动作/原生表单及列表、实际来源单据、追踪数量与财务字段、跨公司拒绝及失败无快照变化。
财务读取实际7650净过账金额，非Billing追踪不包含财务字段，仓库不能打开追踪。
安全报告位于功能worktree `.runtime/m2c-role-http.json`。

第一次实际浏览器核对通过销售、两位经理及采购的渲染与截图，在仓库列表检查处失败：
检查器要求的picking_type_id默认隐藏，location_dest_id在该角色无多库位权限时不存在。
改为验证原生可见name/origin/state及实际五张库存单据，增加一项Node反例测试，保持原生列偏好及权限。
修正仓库列检查后，浏览器通过前六个角色（包含财务实际7650/90及只读源表单），
在B销售空列表检查停止。脱敏DOM诊断确认原生空提示可见，同时存在 `o_view_sample_data` 标记及10行示例占位数据。
这些是原生无数据状态的演示占位行，检查器需与实际业务记录分开；B公司的实际HTTP读取仍为空。
检查器按原生Layout样本标记及可见空提示区分示例行，不将其作为真实业务记录；
仍拒绝在A角色预期有单据时接受示例层，也不允许把已知A公司流程单据伪装成示例行。
七角色实际浏览器最终通过，功能worktree `.runtime/m2c-browser/report.json` 的 `ok` 为true，
销售、两位经理、采购、仓库、财务及B销售各保存一张PNG；原生登录表单、可见菜单/单据、点击追踪、单位/数量/净额、
财务只读及B原生空状态均已按实际DOM核对。运行日志为 `.runtime/m2c-browser-final.log`。
临时Chrome用户目录由工具自行清理，输出不包含Cookie、凭据或配置文件。

最终项目辅助测试 **54项通过**，Node浏览器证据与输入边界测试 **18项通过**；Ruff、JS语法和diff检查通过。
HTTP/浏览器脚本与CI配置的独立审查见下，匹配交付提交的GitHub CI状态以[PR #12](https://github.com/xdfu-nier/odoo-erp-portfolio/pull/12)检查记录为准。

独立规格审查指出：部分列表表头/单号及逐行产品、单位和数量虽然有正确DOM文本，却未逐项验证可见性，
隐藏字段也可能被当作渲染证据。新增两项Node反例保留正确文本但隐藏单位/数量/表头/单号，
主会话RED为20项中18通过、2失败；`.runtime/m2c-browser-visibility-red.log`。
修复逐项采集并核对可见性，保留B示例行及实际数据泄露检查。

真实浏览器复跑又捕获采购菜单切换中间态：导航栏已挂载但旧列表刚卸载，单次读取早于新列表完成。
增加限定时间内的完整证据采集/校验重试，返回同一份已验证快照，持续隐藏/缺单据仍超时失败，不用固定等待掩盖状态。
新增一项Node反例验证旧视图→中间态→错误记录→正确目标，以及持续隐藏字段的拒绝。
最终 **54项Python、21项Node测试通过**，Ruff、JS语法及diff检查通过；
`.runtime/m2c-browser-visibility-final-green.log` 及报告确认严格可见性下七角色实际页面再次全部通过，七张PNG重新生成。
规格复审通过 `137bdf164f0b87c7b4ff38d243fdc111f2f4d816`。
独立质量审查发现Python输出路径保护比较了未规范化路径，`子目录/../输入文件` 可被误认为不同输出，
存在成功验证后覆盖私有输入文件的风险。新增一项测试覆盖三个输入目标及双方路径别名，RED为24项中6个子断言失败、0错误；
`.runtime/m2c-role-path-alias-red.log`。修复在原有符号链接和常规文件/父目录校验后返回规范化路径，再比较输入与输出。
最终 **55项Python、21项Node测试通过**；七角色实际HTTP再次通过，严格可见性七角色浏览器证据保持有效。
最终代码 `1e5affe3d2704603d9207fe750e3a810020661e6` 已通过规格兼容复核与独立质量复核，无未解决Critical/Important项。

## 本地复现

先完成原生初始化/升级，保持数据库运行。已有未知业务历史会被拒绝覆盖；这些命令只管理本地虚构验收配置。

```bash
.venv/bin/python scripts/prepare_acceptance.py --database odoo_portfolio --allow-local-acceptance > .runtime/m2c-acceptance.json
.venv/bin/python scripts/prepare_local_finance.py --database odoo_portfolio --allow-local-acceptance --check
.venv/bin/python scripts/prepare_local_finance.py --database odoo_portfolio --allow-local-acceptance
.venv/bin/python scripts/run_local_fulfillment.py --database odoo_portfolio --allow-local-acceptance --prepare-senders
.venv/bin/python scripts/prepare_local_finance.py --database odoo_portfolio --allow-local-acceptance --enable-fulfillment
.venv/bin/python scripts/run_local_fulfillment.py --database odoo_portfolio --allow-local-acceptance > .runtime/m2c-flow-first.json
.venv/bin/python scripts/run_local_fulfillment.py --database odoo_portfolio --allow-local-acceptance > .runtime/m2c-flow-second.json
cmp .runtime/m2c-flow-first.json .runtime/m2c-flow-second.json
```

另一个终端使用 `.venv/bin/python scripts/manage.py start` 启动服务，再运行只读HTTP与自动化浏览器核对：

```bash
.venv/bin/python scripts/verify_m2c_role_flow.py \
  --base-url http://127.0.0.1:8069 --database odoo_portfolio \
  --credentials .runtime/acceptance-accounts.json \
  --acceptance-report .runtime/m2c-acceptance.json --flow-report .runtime/m2c-flow-second.json \
  --output .runtime/m2c-role-verification.json
node scripts/browser_m2c_check.js \
  --base-url http://127.0.0.1:8069 --database odoo_portfolio \
  --credentials .runtime/acceptance-accounts.json \
  --acceptance-report .runtime/m2c-acceptance.json --flow-report .runtime/m2c-flow-second.json \
  --role-report .runtime/m2c-role-verification.json --output-dir .runtime/m2c-browser
```

浏览器命令使用已有Node22/Chrome和项目内临时配置，无npm依赖。原生样本占位数据会与实际业务记录分开验证，
可见字段、库存单据、B公司隔离和财务金额均为必需证据。CI显式列出21个安全报告/截图路径，凭据、配置和浏览器用户目录不上传。

实际销售角色截图不显示财务追踪字段；财务角色显示实际过账数量及7650净额，截图使用虚构数据：

[销售角色追踪](../screenshots/sales-fulfillment-trace.png) · [财务角色追踪](../screenshots/finance-fulfillment-trace.png)

## 已发布检查点的CI证据

会计准备检查点 `6ca33accbd8d10e285aeb569eb4cea282d34b1a6` 的push与PR工作流成功。
PR运行 [41180800685](https://github.com/xdfu-nier/odoo-erp-portfolio/actions/runs/41180800685) 的6份报告已实际下载核对：
**392项，0 failed，0 errors**，`2026-10-31 07:33:27 UTC`，123.39秒、286,217次查询。
重复初始化报告逐字相同，19身份、40库存、一次盘点保留；认证及四个业务模型导航通过。
原始报告位于主项目 `.runtime/m2c-finance-ci/`。本报告不将旧检查点CI替代当前412项及页面代码的最终CI门禁。
