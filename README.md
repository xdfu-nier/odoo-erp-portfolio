# Odoo ERP Portfolio / 进销存 ERP 作品集

以 Odoo 18 社区版为基础的个人开发作品集。首版范围：销售审批、采购补货、收发货、开票查询、接口幂等和多公司权限。

当前里程碑是**项目初始化**：已建立扩展模块、四个标准业务入口、本地环境脚本、VS Code 调试配置和基础 CI。审批、幂等导入、多公司专项约束及演示数据尚未实现，Odoo 模块安装及完整业务流程也尚未验证。

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
- [requirements-dev.txt](requirements-dev.txt) 基于固定版本 Odoo 的官方依赖，额外包含项目内 PostgreSQL、调试和检查工具。
- 原生开发使用 `psycopg2-binary`，避免全局安装 libpq；当前范围不包含 LDAP 登录，因此未引入需要额外本机开发库的 `python-ldap`。
- `.tools`、`.venv`、`.cache`、`.runtime`、`.upstream` 与 `.env` 不提交到 GitHub。
- 启动脚本只管理本项目数据库，不注册系统服务；不要求 Docker。

## 当前验证

```bash
python3 -X pycache_prefix=.cache/pycache -m unittest discover -s tests -v
python3 -X pycache_prefix=.cache/pycache -m compileall -q scripts addons tests
```

配置测试覆盖项目内路径、仅监听本机、随机凭据及重复初始化保护、非法端口及数据库名拒绝。当前 CI 执行这些测试与 Python 编译检查，不代表 Odoo 业务集成测试已完成。

## 后续开发

- 根据可核实的公开企业案例输出需求来源、用户流程及验收条件。
- 销售金额阈值审批与审批后改单失效。
- 外部订单导入、数据库唯一约束和重复请求冲突处理。
- 多公司隔离与角色权限专项验证。
- 演示数据、完整进销存操作步骤及 Odoo 集成测试。

需求分析会区分公开案例事实与本项目设计规则，逐条注明来源。演示公司、客户及单据使用虚构数据，本项目定位为个人作品集。
