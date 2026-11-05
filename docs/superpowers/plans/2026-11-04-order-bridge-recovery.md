# M3d 工作器竞争与进程退出恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用真实独立事务和真正退出的子进程，证明工作器竞争、人工重试竞争以及提交前中断不会生成重复报价或丢失任务。

**Architecture:** 在已通过服务端门禁的工作器上增加验收测试，不预设需要修改生产逻辑。隔离的已提交UUID夹具仅进入专用测试库；线程代表独立请求，项目Python子进程代表中断的处理器，全部业务操作仍由普通角色通过原生ORM执行。

**Tech Stack:** PostgreSQL真实连接/行锁、Python threading/subprocess/os._exit、固定Odoo源、现有项目虚拟环境；不安装任何新依赖。

---

## 前置和文件

前置M3c服务端完整测试、规格和质量门禁通过。公司→来源→任务协议必须保留，不能为了测试并行而放宽M1公司政策锁。

- 新建 `addons/portfolio_order_bridge/tests/test_import_worker_concurrency.py`：已提交夹具、独立工作器/重试事务、活库结果和进程恢复。
- 新建 `addons/portfolio_order_bridge/tests/worker_process_probe.py`：只用于专用测试库的退出探针，作为独立子进程执行，不在模块导入时触发副作用。
- 修改测试init；如有必要添加聚焦的纯Python探针目标校验测试。
- 不在本阶段开发界面、cron或生产后台进程。只有真实失败证明存在缺陷时，才在根代理确认后修复最小生产路径。

### Task 1: 独立工作器竞争与退出恢复

- [x] **Step 1: 编写测试和隔离夹具，冻结给根代理**

严格数据库名`odoo_portfolio_m1_test`检查位于任何可提交游标之前。UUID公司/用户/来源/客户/商品/供应商/单位及映射按原生ORM建立；公司货币CNY、交付开票库存商品、有效ref/SKU/供应商、单公司专用Operator+基础Sales与独立Sales负责人。普通Settings创建来源/映射，普通Integration先提交任务。启用M1审批/主数据仅作用于自有测试公司。

创建CNY公司采用native disable_company_pricelist_creation，保留原有功能组及成员、默认价目表；不得污染其他公司或为Integration授予Sales。清理先归档自有来源再归档自有账号，保留不可变审计，不删除测试库或既有业务记录。

不要继承既有测试类而重复执行原有方法；可提取无测试方法的夹具基类，或者建立本文件专用夹具。每个线程必须使用独立registry.cursor和已加载registry，所有失败回传根测试，SQL超时/Event/monotonic/join有界。

- [x] **Step 2: 四个真实验收场景**

1. **两个工作器处理同一队列。** 首事务普通Operator实际创建报价后、提交前暂停；第二独立事务普通同一Operator预读旧快照后执行`_process_pending`。第三连接观察真实锁等待，随后放行提交。允许实际40001后新游标重试；最终只有一个done任务、一张报价、一次成功尝试，原始接收审计不变。不得声称同公司工作器完全并行：M1公司锁天然序列化商业写入。
2. **SKIP LOCKED不遗漏后续候选。** 提交7个有效pending任务，独立技术游标只读锁定前2个任务行；工作器真实执行后处理后5个，前2个仍pending，证明不是先取前5个ID再跳过。释放只读锁后处理剩余2个，最终7张唯一报价。测试仅对锁使用参数化SQL，不用SQL构造业务状态。
3. **人工重试竞争。** 原生处理制造missing mapping失败，由Settings修复后，两独立Operator事务竞争对同一failed任务`action_retry`。真实锁观察及允许40001新事务重试后，仅一次有效retry事件，第二请求因已pending被拒绝；再次处理生成唯一报价、总尝试2次，失败历史与原始载荷保持。
4. **实际处理进程在提交前退出。** Integration提交pending并已commit。独立项目Python子进程实际以普通Operator执行目标任务，包装原生sale.order.create仅在真实报价/商品行创建并flush后写安全见证文件，然后调用`os._exit(73)`，跳过Python保存点/游标清理。父进程确认退出73和见证的实际UID、公司、job/quote/行ID以及processing状态；新连接核对未提交报价/行/尝试全部不存在、任务pending且次数0。随后新事务重跑普通工作器，只有一张已提交报价、一个done事件，重复运行不增单。不能用单事务手动rollback或仅抛异常代替本场景。

每项都用新事务读实际持久结果，不使用旧测试事务/ORM缓存作结论。保留独立连接PID、SQLSTATE、txid和安全见证信息，不打印凭据、配置、载荷或任意子进程异常全文。

- [x] **Step 3: 安全、项目内退出探针**

探针只接受严格测试数据库、项目原生配置、loopback PostgreSQL和项目runtime临时目录下唯一标记文件；无效目标必须在导入/连接Odoo前拒绝。源码来自本项目固定`.upstream/odoo`，解释器使用项目`.venv/bin/python`，PYTHONPYCACHEPREFIX和TMPDIR继续指向项目目录。argv用参数列表传递，不拼接shell或传密码。

子进程加载同一已安装模块，在全新连接按实际Operator UID及唯一公司创建环境，目标job必须属于指定来源/公司且pending。见证文件采用独占创建和0600权限，只含阶段、ID、计数/布尔值，无载荷、地址或凭据；父测试在finally清理。子进程stdout/stderr不透传到公开日志，异常只报告安全退出码/缺少见证等分类。父进程设置有界超时并确保退出，不留下后台服务。

子进程用原生create的测试包装器实际执行后再退出，不添加生产测试后门或可伪造context绕过。当前生产工作器保持普通角色、公司锁、自己的保存点和无手动commit约束。

- [x] **Step 4: 根代理实际运行，再决定是否需要修复**

根代理以已有native命令、严格测试库和当前worktree addons运行`/portfolio_order_bridge:TestImportWorkerConcurrency`，日志`.runtime/m3d-worker-concurrency-native.log`。这是验证已有实现：首次通过即记录真实通过，不人为删除正确代码制造RED。连接/发现/探针启动错误先定位环境；真实约束失败先记录可复现RED，再授权最小修复。

根代理核对四项真实场景、子进程退出码73与数据库事实，而不是只看测试名或静态代码。之后完整两个模块原生回归、Python/Node/Ruff通过，先规格再质量独立审查；只提交本任务文件及有实际失败证据的最小修复。

- [x] **Step 5: 记录和交付边界**

更新M3质量记录及UAT-016—018/024相关自动化证据，记录模块/源码/数据库、实际用例数、命令和安全子进程见证。GitHub新提交必须实际运行这些场景并核对制品。之后才进入界面、专用用户默认停用cron、隔离本地样例和认证HTTP/浏览器的最后M3验收；M4备份恢复/性能仍保留。

实际结果：2026-11-04根代理执行4项真实验证，首次全部通过，无生产逻辑变更。实际子进程以73退出前已flush原生报价/两行并写0600见证；新事务确认全部回滚后仅恢复一张报价。真实工作器及重试竞争有第三连接Lock/40001/新事务证据；7任务锁最早2个时，后5个在释放前处理完成。聚焦4项零失败/零错误，8.13秒、11369次查询（`.runtime/m3d-worker-concurrency-native.log`）；完整519项post-tests零失败/零错误，05:52:33 UTC结束，309.65秒、527431次查询（`m3d-worker-recovery-full-native.log`）。92项Python（新增9项退出探针目标校验）、27项Node及Ruff、独立规格/质量审查通过。当前新代码CI及UI/cron/本地HTTP最后验收仍待核对。
