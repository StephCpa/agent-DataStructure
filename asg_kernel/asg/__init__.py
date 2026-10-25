"""ASG 语义内核（阶段 A）—— 依据《面向大模型智能体的数据结构详细设计说明书》V1.0。

模块映射（说明书 2.2 节）：
- write_service  -> M1 写入服务（串行提交协议 7.1；observe/assess/revise/invalidate 5.1–5.4）
- authorize      -> M4 策略服务（operation_digest 规范化 9.1；Grant 生命周期 6.4）
- gateway        -> M2 执行网关（prepare_effect 5.5；outbox 循环 7.6；对账 7.7；聚合 6.1）
- ledger         -> 预算账本与资源预留（7.5，不变量 I5）
- projection     -> M7 投影与确定性回放（7.9，不变量 I9）
- registry       -> 记录类型注册表（附录 A）与 schema 校验（3.2/3.15）
- storage        -> 存储层（第 4 章 SQLite 开发适配）
- errors         -> 错误码目录（第 10 章 A/B 子集）
"""
from .errors import AsgError, ERROR_NAMES
from .ids import uuid7, digest_obj, canonical_json
from .ledger import Ledger, reserve_in_txn, settle_in_txn
from .projection import Projection, reducer_v1
from .storage import connect, init_schema, write_txn
from .write_service import WriteService
from .authorize import Authorizer, compute_operation_digest
from .gateway import Gateway, Connector, ConnectorContract
from .invalidation import process_queue, read_gate, watermark, marks_on
from .evidence import justify, evaluate_support, root_source, eligible
from .context import project_context, binding_unit, manifest_valid
from .purge import Purger

__all__ = [
    "AsgError", "ERROR_NAMES", "uuid7", "digest_obj", "canonical_json",
    "Ledger", "reserve_in_txn", "settle_in_txn",
    "Projection", "reducer_v1", "connect", "init_schema", "write_txn",
    "WriteService", "Authorizer", "compute_operation_digest",
    "Gateway", "Connector", "ConnectorContract",
    "process_queue", "read_gate", "watermark", "marks_on",
    "justify", "evaluate_support", "root_source", "eligible",
    "project_context", "binding_unit", "manifest_valid", "Purger",
]
