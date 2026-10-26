# Odoo ERP Portfolio / 进销存 ERP 作品集

以 Odoo 18 社区版为基础的工业配件分销 ERP 个人工程项目。业务闭环：客户报价、审批、缺货采购、分批交付、开票、退货和贷项；外部订单接入在业务闭环之后开发。

工业配件分销需求与方案基线0.1已经确认，M0已通过原生环境安装、模块加载、登录和菜单检查，见[实测记录](docs/quality/m0-verification.md)。现有四个业务菜单仍只是标准模块入口，审批、幂等导入和业务追踪尚未实现，业务验收尚未执行。pg_trgm和PDF渲染工具缺项仍在跟踪。

开发按照“案例调研 → 需求评审 → Odoo差异分析 → 分里程碑实现 → 集成测试 → 业务验收 → 发布恢复验证”推进。文档入口见[需求与交付文档](docs/requirements.md)，具体完成条件见[里程碑与流程](docs/delivery/workflow.md)。

默认使用源码开发环境，新增依赖安装在当前目录内：

| 路径 | 用途 |
| --- | --- |
| `.tools/` | 项目专用 Python、数据库程序及辅助工具 |
| `.venv/` | Python 虚拟环境 |
| `.upstream/odoo/` | Odoo 官方源码，不随作品集重复提交 |
| `.runtime/` | PostgreSQL 数据、Odoo 配置、文件存储及日志 |
| `addons/portfolio_trade/` | 自主开发的业务扩展模块 |
| `scripts/` | 环境初始化、开发启动及测试命令 |
| `docs/` | 需求来源、实现方案、演示与验证记录 |

## 本地开发

要求 macOS Apple Silicon / Intel 或 Linux x86_64，系统已有 `curl` 和 `uv`。也可以把 uv 可执行文件放在 `.tools/bin/uv`。Python 3.12 会自动下载到 `.tools/python`，不会修改全局 Python 或 PATH。

```bash
# 安装项目内 Python、虚拟环境依赖和固定版本的 Odoo 源码
./scripts/setup.sh

# 初始化项目内 PostgreSQL 数据目录并安装自定义模块
.venv/bin/python scripts/manage.py init

# 启动源码开发服务
.venv/bin/python scripts/manage.py start
```

打开 <http://127.0.0.1:8069>。新建开发数据库的初始 Odoo 登录账号为 `admin` / `admin`。数据库连接密码和数据库管理密码在首次配置时随机生成，保存在被 Git 忽略的 `.env` 中；它们与页面登录密码不同。

PostgreSQL 二进制由 `pgserver` wheel 安装在 `.venv` 中；数据存放在 `.runtime/postgres`，仅监听 `127.0.0.1:55432`。Odoo 使用独立的非超级用户数据库角色。停止 Odoo 后，数据库保持运行，可用以下命令停止：

```bash
.venv/bin/python scripts/manage.py stop-db
```

修改模型或 XML 后更新模块：

```bash
.venv/bin/python scripts/manage.py update
```

VS Code 打开本项目目录，选择 `.venv/bin/python` 后使用 `Odoo: source development` 启动配置。数据库须先完成初始化并保持运行。调试配置需要 VS Code 的 Python Debugger 扩展。

## 依赖管理

- Odoo 源码版本固定在 [odoo-source.lock](odoo-source.lock)，下载到 `.upstream/odoo`；保留已有不同版本的源码目录，避免覆盖个人修改。
- [requirements-dev.txt](requirements-dev.txt)记录Odoo兼容的开发依赖来源；实际安装使用已验证CPython 3.12环境生成的[requirements-dev.lock](requirements-dev.lock)，固定包括间接依赖在内的66个包。
- 原生开发使用 `psycopg2-binary`，避免全局安装 libpq；当前范围不包含 LDAP 登录，因此未引入需要额外本机开发库的 `python-ldap`。
- `.tools`、`.venv`、`.cache`、`.runtime`、`.upstream` 与 `.env` 不提交到 GitHub。
- 启动脚本只管理本项目数据库，不注册系统服务；不要求 Docker。

## 当前验证

```bash
python3 -X pycache_prefix=.cache/pycache -m unittest discover -s tests -v
python3 -X pycache_prefix=.cache/pycache -m compileall -q scripts addons tests
```

基础检查覆盖项目内路径、仅监听本机、随机凭据、重复初始化保护、非法配置、PostgreSQL二进制及导航验证反例。CI另设原生Odoo作业，安装项目内依赖、初始化真实数据库、运行自定义模块测试并验证登录与菜单。CI作业的实际结果和业务UAT覆盖须分别记录，不能把尚无业务测试的模块标为业务验收完成。

## 开发顺序

| 里程碑 | 目标 | 完成证据 |
| --- | --- | --- |
| M0 | 项目内开发环境可用 | 实际安装、启动、登录及模块安装记录 |
| M1 | 报价与审批 | 阈值边界、自审限制、改单失效、并发和权限测试 |
| M2 | 采购到退货的履约闭环 | 按角色执行分批收发货和发票贷项，数量与金额对平 |
| M3 | 外部订单接入 | 幂等、并发提交、失败保留和重试恢复测试 |
| M4 | 发布和恢复 | 新库安装、升级、备份恢复及性能记录 |

真实案例来源与本项目规则在[调研记录](docs/discovery/case-research.md)中区分。14条需求对应[28个验收场景](docs/quality/acceptance.md)，当前均尚未执行业务验收。测试公司、客户及单据使用虚构数据。
