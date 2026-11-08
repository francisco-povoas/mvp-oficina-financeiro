class FinanceConflictError(RuntimeError):
    pass


class FinanceNotFoundError(LookupError):
    pass


class IdempotencyConflictError(FinanceConflictError):
    pass
