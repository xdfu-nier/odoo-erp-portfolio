"""Private, cursor/actor/document-bound frames for native accounting internals."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_NATIVE = ContextVar("portfolio_native_accounting", default=None)
_STATE = ContextVar("portfolio_native_account_state", default=None)
_SALE_COMPUTE = ContextVar("portfolio_sale_invoice_compute", default=None)


@dataclass
class NativeAccounting:
    cursor: object
    uid: int
    company_ids: frozenset
    move_ids: set = field(default_factory=set)
    new_moves: int = 0

    def matches(self, env):
        return self.cursor is env.cr and self.uid == env.uid

    def accepts(self, moves):
        return self.matches(moves.env) and self.move_ids.issuperset(moves.ids)

    def include_created_parents(self, moves):
        """ORM has allocated parent IDs before its nested product-line create."""
        missing = set(moves.ids) - self.move_ids
        if (
            self.matches(moves.env) and len(missing) <= self.new_moves
            and set(moves.sudo().company_id.ids).issubset(self.company_ids)
        ):
            self.move_ids.update(missing)
            self.new_moves -= len(missing)


def native_frame(env, moves):
    frame = _NATIVE.get()
    return frame if frame and frame.matches(env) and frame.accepts(moves) else None


@contextmanager
def accounting_frame(env, companies, moves, new_moves=0):
    frame = NativeAccounting(env.cr, env.uid, frozenset(companies.ids), set(moves.ids), new_moves)
    token = _NATIVE.set(frame)
    try:
        yield frame
    finally:
        _NATIVE.reset(token)


def include_account_parents(moves):
    frame = _NATIVE.get()
    if frame:
        frame.include_created_parents(moves)


@contextmanager
def state_action(moves, target):
    token = _STATE.set((moves.env.cr, moves.env.uid, frozenset(moves.ids), target))
    try:
        yield
    finally:
        _STATE.reset(token)


def permits_state(moves, target):
    capability = _STATE.get()
    return bool(capability and capability[0] is moves.env.cr and capability[1] == moves.env.uid
                and capability[2].issuperset(moves.ids) and capability[3] == target)


@contextmanager
def sale_computation(lines, fields):
    token = _SALE_COMPUTE.set((lines.env.cr, lines.env.uid, frozenset(lines.ids), frozenset(fields)))
    try:
        yield
    finally:
        _SALE_COMPUTE.reset(token)


def permits_sale_computation(lines, fields):
    capability = _SALE_COMPUTE.get()
    return bool(capability and capability[0] is lines.env.cr and capability[1] == lines.env.uid
                and capability[2].issuperset(lines.ids) and capability[3].issuperset(fields))
