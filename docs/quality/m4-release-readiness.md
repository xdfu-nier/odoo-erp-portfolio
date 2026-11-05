# M4 发布恢复与性能验证记录

状态：开发中。REQ-013/014、UAT-021/022；当前只完成一致备份，独立库恢复和性能尚未执行。设计见[发布验收方案](../superpowers/specs/2026-11-05-release-readiness-design.md)。

## 项目内一致备份

2026-11-05，代码`76b81a9440be92d32a393586e2ce6db3a345de4f`，基于已合并M3的main`9bd5684214bf28720474a6fb82300aa8a4c96541`。新增备份CLI、纯运行边界/manifest校验及延迟导入的只读SQL快照，不修改业务插件或已有角色。

先实际29项缺少实现的RED，再实现；源码审查补齐官方tarball溯源和实际内容摘要、全部商品模板/原始库存明细/quant与完整拥有范围的单据检查。GNU/PAX元数据限制反例先真实失败，再将限制放到解压读取及扩展头部解析之前。最终**210项Python辅助测试、57项Node及Ruff通过**，独立规格/质量及各问题闭环复查通过。备份工具35/44/48项聚焦契约与实际RED/GREEN日志保存在工作树`.runtime`，不以纯测试代替数据库操作证据。

根代理实际只读快照采集确认55个namespace标识（M1十九、M2十、M3静态17/动态9）、560条附件元数据，库存10、毛交付100、退10、净交付90、净过账7650。实际源码溯源验证Odoo tarball标记/lock为`98e48692891fc9a3aaa7f53001305b322f53cfa2`，addons摘要`19622159286c235fe2215967b4212db7a960b746dcc07225587e0d9d4384eb19`、上游实际内容摘要`82b5310c0eef737e730048ddacae4d7a473fbde07332c04b1a12d3d57b0dd2cd`。这记录当前源码内容，不宣称Git验证了无Git目录的上游原始性。

实际执行前核对main服务PID31791的项目命令与cwd，优雅停止并确认进程退出；数据库继续运行。CLI检查8069无监听、源库无其他连接，再采集快照、使用项目PostgreSQL16的custom dump并归档全部filestore、复核前后数据指纹和归档清单。

实际结果：**备份通过，源数据不变，尚未恢复**。数据库dump5,279,948字节，filestore归档3,081,199字节、878个文件（包括GC/checklist等非附件文件）；备份目录700，dump/归档/manifest600。manifest记录实际代码`76b81a9`、版本/摘要/私有快照，`restore_verified=false`保持。安全报告为主项目`.runtime/m4-backup-report.json`，私有目录由`.runtime/m4-backup-location.json`记录；备份和manifest不公开上传。

完成后从main恢复本机开发服务。下一步在新的restore库及独立文件目录验证数据、实际角色和附件；另建bench副本进行固定50单×2行及追踪测量。两者均未执行，不根据dump存在提前标记UAT-021/022通过。
