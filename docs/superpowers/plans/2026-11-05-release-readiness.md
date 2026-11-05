# M4 备份恢复与性能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实际恢复当前完整进销存/导入数据到新库，验证附件，并留下固定规模的原生性能证据。

**Architecture:** 备份/恢复工具的目标、文件与私有manifest检查集中在独立helper；备份、恢复、原生验证和性能CLI各负一责。源开发库只读核对，恢复与bench各使用新的库和filestore，CI沿用现有项目运行环境。

**Tech Stack:** Python标准库、项目psycopg2/pgserver、固定Odoo18、项目PostgreSQL16、既有GitHub Actions。

---

### Task 1: 私有运行边界和一致备份

**Files:** 新建`scripts/release_runtime.py`（纯边界/manifest）、`scripts/release_snapshot.py`（延迟导入的只读数据库采集）、`scripts/backup_local.py`、`tests/test_release_runtime.py`、`tests/test_backup_local.py`。`release_runtime.source_snapshot`保留稳定入口，调用时再导入snapshot模块，避免纯测试与无效输入触发原生依赖。

- [x] **Step 1: 纯契约测试ONLY与根代理RED**

约定`release_runtime`提供`load_local_config(config_path)`、`runtime_path(path, existing=False)`、`source_snapshot(config)`、`read_manifest(backup_dir)`及安全`ReleaseError(code)`；CLI入口为`backup_local.main(argv=None)`，参数`--config --database --output-dir --allow-local-release`。只有源`odoo_portfolio`、loopback、私有项目配置、全新备份目录可通过；无效输入必须在PG/Odoo导入、子进程或文件创建之前拒绝。测试使用合成配置/目录验证越界、符号/硬链接、输出覆盖、错误端口/库、secret不回显与manifest拒绝篡改。实际RED命令：

JSON固定schema_version=1、严格类型/重复键拒绝，manifest32MiB、报告1MiB、配置64KiB，文件项100000/路径256字符；错误版本/大小/集合先拒绝。为PG环境构造器增加带敌对`PGHOSTADDR/PGSERVICE/PGOPTIONS/PGPASSFILE`的纯测试，证明传入子进程只含受控PG键且连接参数独立固定；原生导入同样隔离继承环境。

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p 'test_*release*.py' -v
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p 'test_backup_local.py' -v
```

- [x] **Step 2: 一致备份实现与冻结**

按设计实现配置/路径、只读namespace/角色/政策/业务/附件快照，PG工具仅通过参数列表与私有环境调用。`backup_local`先验证8069停服和其他连接0，再快照→dump/filestore→hashmanifest→快照比较。报告固定schema/ok/database/versions/file counts/checksums/一致性布尔值，未恢复时不得给restore通过。失败仅固定代码、非0退出，不泄露原异常或删除新建资源。

源码溯源采用`source_provenance(project_root=ROOT)`：ERP Git提交、addons内容摘要、tarball的固定lock/stamp及实际`upstream_sha256`；不在没有`.git`的Odoo目录向上误读ERP提交。纯测试覆盖无Git目录的有效tarball、错误/缺失标记、内容/路径变化及仅缓存/时间变化。manifest绑定实际upstream摘要，兼容性不由ERP Git提交相等决定。

快照纳入全部5个商品的原生模板父记录、按5个商品归集的move/move_line/quant（含无picking期初明细），并核对完整来源任务/尝试、客户或origin或商品关联的报价集合严格为2/4/2。用受控源表回调测试政策/单位、期初明细、预留/库位变更影响指纹，额外未标记单据即拒绝，实际SQL适配器同一路径喂入生产摘要。归档限制在gzip读取/物理GNU/PAX头部解析前执行，包含padding/尾部，测试以小额度证明不会先读取过大扩展元数据。

- [x] **Step 3: 根代理验证、spec→quality后提交**

根代理执行全部Python、Node和Ruff；审查源码范围、快照SQL与权限后提交。实际停服备份已于Task 1提交后执行并复验，随后恢复main开发服务；这不代表Task 2恢复或附件原生验证已通过，后续仍须独立验收。

### Task 2: 新库/文件恢复和原生只读验收

**Files:** 新建`scripts/restore_local.py`、`scripts/verify_restored.py`、`tests/test_restore_local.py`、`tests/test_verify_restored.py`；必要的共享归档/报告校验放`release_runtime.py`。

- [ ] **Step 1: 纯失败测试与根代理RED**

入口`restore_local.main(argv=None)`参数`--config --backup-dir --purpose restore|benchmark --output --allow-local-release`；`verify_restored.main(argv=None)`参数`--restore-report --output --allow-local-release`。测试目标生成白名单/现有库拒绝、manifest SHA/文件列表、tar前缀/重复/点段/链接/特殊成员/1GiB与100000成员边界、私有目录/配置和输出别名，错误在建库前拒绝；验证报告必须全部固定检查通过，不能缺项即成功。`--purpose`分别生成restore/bench前缀，不能由调用者传源库作恢复目标。

restore输出独立不可变receipt的`restore_complete=true`，不提前输出验证`ok=true`；verify另写完整`ok=true`报告。两者绑定目标/purpose/runtime/backup及manifest摘要和配置，测试篡改/错配及仅伪造布尔值拒绝。Git提交只溯源，兼容性按addons树/上游revision/模块版本比较。

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p 'test_restore_local.py' -v
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p 'test_verify_restored.py' -v
```

- [ ] **Step 2: 恢复实现、原生核对与冻结**

新目录中安全逐文件重建，PG新建目标+single-transaction恢复，生成独立data_dir/600配置/无HTTP和cron。`verify_restored`先验证目标报告，再延迟导入Odoo；读取当前版本和namespace/业务/角色摘要，普通业务角色核对单据，附件全量SHA1/长度与归档SHA256比较，源库快照保持。安全输出固定check集合与namespace counts、黄金值和版本，不输出密码/hash/报文或私有manifest。

实际角色矩阵：Sales黄金单/两报价、Operator任务/事件/两报价、Integration任务/事件/公开来源及报价/私有来源拒绝、Finance黄金单/追踪。不放宽现有准备器两库白名单，改用只读采集。附件分别处理文件/数据库/空/URL存储，独立存在性与长度检查阻止`_file_read`静默空字节假阳性。

- [ ] **Step 3: 根代理测试和双重审查后提交**

全部Python/Node/Ruff及spec→quality通过后提交。根代理核对已启动main PID，停服务至退出，创建唯一备份，恢复一份restore库并运行实际验证。失败不覆盖源、不自动DROP；定位后补RED/GREEN和审查，再继续。验证成功保留库、目录和安全报告。

### Task 3: 独立bench副本的固定规模检查

**Files:** 新建`scripts/benchmark_local.py`、`tests/test_benchmark_local.py`，实现细节在`scripts/release_benchmark.py`（避免CLI承担业务测量）。

- [ ] **Step 1: 纯目标/统计测试ONLY与实际RED**

入口`benchmark_local.main(argv=None)`参数`--restore-report --verification-report --output --allow-local-release`，只接受已验证的新bench副本，原样例/restore副本/非本机均拒绝。统计函数`summary(samples)`输入固定`elapsed_ms/sql_count`列表，验证非空、非负有限数字、bool拒绝及nearest-rank p95；报告必须保留原始样本、规模和环境、完整check集合，无密码或载荷。

- [ ] **Step 2: 实际角色测量代码与冻结**

bench库确认没有`PERF-`历史后生成50单×2行，使用既有来源/映射和普通Integration/Operator，接收/批处理/重放每样本独立提交；处理每批5共10批。新单金额309，期望50个新done/唯一draft/成功事件，原2任务/报价/4事件保持。追踪原M2销售单由Finance预热5次、测30次，实际action与字段读取，逐次rollback瞬态快照并核对100/10/90/7650。游标SQL计数差与真实壁钟同时记录，构造数据和计量范围分开；不测量或宣称HTTP吞吐。

50次接收+50次重放应精确增加来源ingress_revision 100，原业务记录投影逐值比较，clone的全源指纹不可混同源码库保护。处理动作通知不当作计数，实际落库结果证明5条/批；SQL计数在事务结束后、游标关闭前采集。bench开始前重新活库确认验证receipt绑定，不只读取`ok=true`。

- [ ] **Step 3: 根代理纯测试、双重审查、实际运行**

审查后用同备份再生成bench库，先实际恢复验证，再运行测量。核对最终实际记录数、金额、重放复用和原样例；记录机器/缓存/事务/源码与完整安全样本，不预先承诺阈值或优化提升。source开发库及restore副本重新只读比较，保留实际证据后提交结果文档。

### Task 4: CI、M3状态结项和发布文档

**Files:** 修改`.github/workflows/checks.yml`、`README.md`、`docs/delivery/workflow.md`、`docs/product/prd.md`、`docs/quality/acceptance.md`、M3质量/计划状态；新建`docs/quality/m4-release-readiness.md`及`docs/delivery/release-operations.md`。

- [ ] **Step 1: CI最小串联与静态审查**

保留613项原生与全部原角色流程/HTTP/浏览器，结束页面检查后显式kill并wait Odoo，再在PG运行期间执行备份→restore/verify→bench/verify/measure。工具使用CI项目配置及新namespace；trap兜底关闭PG，不能提前停止数据库或忽略任何命令失败。只上传命名明确的安全结果JSON/受控日志，禁止manifest/config/credentials/dump/tar/filestore/runtime通配路径。根代理YAML解析、shell语法和白名单检查后spec→quality提交。

- [ ] **Step 2: 精确CI及交付记录**

根代理发布精确head，核对push/PR四任务和实际下载制品；独立恢复、全部固定检查、50×2订单/10批/50重放/30追踪的样本与统计相符，黄金值与源保护成立。新库安装证据来自现有CI，模块升级证据/限制按既有实际记录引用，不把还原后的加载当升级迁移。README与M3状态改为已合并并附最终613项CI，M4记录实际恢复/测量结果及未覆盖边界。

- [ ] **Step 3: 合并、main验证与收尾**

通过最终审查和精确CI后合并M4 PR，关闭Issue #7，精确同步main、启动原开发服务并只读复验，保留备份/restore/bench与工作树证据。正式简历只补实际已交付能力，不写模拟吞吐为生产收益；人工UAT/可选PDF工具等状态继续明确保留。
