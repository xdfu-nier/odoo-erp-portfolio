# M3a 外部订单载荷契约 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供精确且有界的外部订单载荷解析与业务摘要，为持久化任务建立稳定输入。

**Architecture:** 使用标准库实现纯 Python 解析器，放在未来 `portfolio_order_bridge` 模块内，不导入 Odoo。不可变返回对象保存原始 JSON、规范业务 JSON 和 SHA-256；调用方每次取得新业务字典，避免修改已计算摘要的数据。此独立计划只覆盖 M3a，后续原生模型按已确认的 M3 设计继续。

**Tech Stack:** Python 3.12、Decimal、json、hashlib、dataclasses、unittest；现有项目虚拟环境，无新依赖。

---

## 文件职责

- `addons/portfolio_order_bridge/payload.py`：结构、标识与十进制校验，精确规范化和摘要。
- `tests/test_order_payload.py`：不启动 Odoo 的契约测试；用文件路径载入纯解析器。
- 不提前建立空 manifest、ORM 模型、ACL 或菜单。M3b 才安装完整模块。

### Task 1: 精确载荷与摘要

- [x] **Step 1: 写失败测试并冻结实现**

建立 fixture 与测试加载器；文件不存在时给出明确断言，不因 import 错误阻止测试发现：

```python
path = ROOT / "addons/portfolio_order_bridge/payload.py"
self.assertTrue(path.is_file(), "M3 payload parser is not implemented")
spec = importlib.util.spec_from_file_location("portfolio_payload_test", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
```

逐项断言真实输入/输出：数字前导零和尾零、外部行排序和 request_id/sent_at 变化保持相同摘要；客户/数量/价格/折扣变化产生不同摘要；原始 JSON 保留传输字段；业务 JSON 不包含传输字段；修改 `as_dict()` 结果不改变下一次结果或摘要。独立计算 `hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()` 核对摘要，不复制实现逻辑。

反例包括每一必填字段缺失、未知顶层/行字段、非字典/非列表、空行/101 行、重复行号、标识空白/控制字符/长度、错误币种类型、浮点/布尔/负数/指数/NaN/Infinity、小数有效精度超过六位、零数量、数量或价格超十亿、折扣超过百、数值字符串超过 64 字符、载荷超过 65536 字节。错误只含错误码和路径，不回显特制敏感输入值。

- [x] **Step 2: 根代理实际 RED**

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p test_order_payload.py -v
```

预期因缺少 `payload.py` 的断言失败。根代理确认后通知实现者开始生产代码。

- [x] **Step 3: 实现纯解析 API**

公开签名和返回约定：

```python
class PayloadValidationError(ValueError):
    def __init__(self, code: str, path: str):
        self.code, self.path = code, path
        super().__init__(f"{code}: {path}")

@dataclass(frozen=True)
class NormalizedOrder:
    raw_json: str
    canonical_json: str
    payload_hash: str

    def as_dict(self) -> dict:
        return json.loads(self.canonical_json)
```

入口为 `normalize_order_payload(payload) -> NormalizedOrder`，完整落实设计第 1 节全部规则。校验键集合后再遍历；数值先检查字符串形态与长度再转 Decimal，按精确数值范围和有效小数位判断，不用 float/round。用 `format(number, 'f')` 后仅去掉小数尾零，避免移除整数末尾有效零；不依赖 Decimal 默认上下文的四舍五入。

JSON 使用 `ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False`；原始载荷在递归资源检查后进行 UTF-8 字节上限检查。业务部分构造新字典、按行号排序，计算 SHA-256。返回对象不持有调用方的可变结构。不在异常中包含 JSON、原始键值或 Decimal 异常文本。

- [x] **Step 4: 根代理 GREEN 和辅助回归**

```bash
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests -p test_order_payload.py -v
.venv/bin/python -X pycache_prefix=.cache/pycache -m unittest discover -s tests
.venv/bin/ruff check addons/portfolio_order_bridge/payload.py tests/test_order_payload.py
node --test tests/browser_m2c_check.test.js
```

预期新契约测试及既有 55 项 Python、27 项 Node 测试全部通过，ruff 无错误。此任务不触及 Odoo 模型或现有模块，不用静态测试冒充原生集成验收。

- [x] **Step 5: 自审、独立审查和提交**

实现者检查边界、资源上限与十进制精确性后冻结文件。根代理先组织规格审查，再组织质量审查，任何发现先修复并重跑相关测试。通过后仅暂存本任务文件，提交 `feat: normalize external order payloads exactly`。更新本计划实际结果，再进入 M3b 模型计划。

实际记录：2026-11-03，28 项解析测试先因缺少实现逐项失败（`.runtime/m3a-red-tests.log`），实现后根代理完整 83 项 Python 测试、27 项 Node 测试和相关 Ruff 检查通过（`.runtime/m3a-green-python.log`、`m3a-green-node.log`）。独立规格与质量审查均通过，源代码提交 `a6b8f91`。M3a 为纯解析器；持久化、Odoo 权限、并发和恢复尚未实现，不由本记录宣称通过。
