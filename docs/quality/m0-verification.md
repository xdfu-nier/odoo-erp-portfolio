# M0 原生开发环境验收记录

验收日期：2026-10-26（Asia/Shanghai）。本记录只证明开发环境能够运行，不证明任何业务需求已经完成。

## 环境

| 项目 | 实测值 |
| --- | --- |
| 系统 | macOS，Apple Silicon |
| Python | 3.12.13，位于项目`.tools/python` |
| 虚拟环境 | 项目`.venv`，66个Python依赖 |
| Odoo | 18.0，源码提交`98e48692891fc9a3aaa7f53001305b322f53cfa2` |
| PostgreSQL | 16.2，二进制来自项目虚拟环境中的`pgserver==0.1.4` |
| 数据库 | 项目`.runtime/postgres`，监听`127.0.0.1:55432` |
| HTTP | `127.0.0.1:8069` |

Python下载包已与官方GitHub发行资产的SHA256比对一致。新增依赖与缓存均位于项目中，没有使用Docker或注册数据库系统服务。

## 已执行检查

| 检查 | 证据/结果 |
| --- | --- |
| Odoo模块初始化 | `scripts/manage.py init`退出码0，66个模块加载完成 |
| 核心业务依赖 | account、sale_management、sale_stock、purchase_stock均为installed |
| 自定义模块 | portfolio_trade为installed |
| 数据库权限 | odoo角色CREATEDB=true、SUPERUSER=false、CREATEROLE=false |
| 登录页面 | `/web/login`返回HTTP 200 |
| 真实登录 | `/web/session/authenticate`返回有效用户，会话用于后续RPC |
| 模块查询 | 通过已认证RPC查询自定义模块，状态installed |
| 业务菜单 | 5个菜单XML ID、父子关系、动作指向、list/form视图和四个标准模型注册均通过真实RPC检查 |
| 运行配置及工具 | 7个测试通过，包括三个PostgreSQL二进制实际执行和菜单误报反例 |

可复现命令：

```bash
.venv/bin/python scripts/manage.py init
.venv/bin/python scripts/manage.py start
# 在另一终端中执行
.venv/bin/python scripts/verify_native.py
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -v
```

验证脚本输出保存在被Git忽略的`.runtime/m0-verification.json`；不保存密码、Cookie或数据库连接凭据到公开记录。

## 暴露的问题与处理

原脚本从`pgserver`顶层导入`POSTGRES_BIN_PATH`，但0.1.4版通过`__all__`只导出命令函数，导致初始化失败。增加真实二进制回归测试，复现后改为从实际定义该常量的模块导入；三个工具均可在虚拟环境内解析并执行。

## 已知能力限制

- 该PostgreSQL wheel未包含`pg_trgm`，Odoo初始化会提示无法创建该扩展；本次基础安装及登录检查仍通过。相似搜索与相关索引/性能未验证，需在性能里程碑前解决。
- 尚未安装wkhtmltopdf/wkhtmltoimage，PDF报表与图片导出不计入已验证范围。
- 尚未执行28个业务UAT场景，也未验证审批、退货、幂等、备份恢复或性能。

业务开发可在该环境中继续；上述限制继续跟踪，不能在发布时省略。
