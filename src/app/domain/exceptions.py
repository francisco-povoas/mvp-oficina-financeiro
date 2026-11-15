class FinanceConflictError(RuntimeError):
    pass


class FinanceNotFoundError(LookupError):
    pass


class IdempotencyConflictError(FinanceConflictError):
    pass


class PaymentProviderError(RuntimeError):
    pass


class PaymentProviderTimeoutError(PaymentProviderError):
    pass


class PaymentProviderUnavailableError(PaymentProviderError):
    pass


class PaymentProviderRejectedError(PaymentProviderError):
    pass


class PaymentProviderContractError(PaymentProviderError):
    pass


class InvalidWebhookSignatureError(PaymentProviderError):
    pass
