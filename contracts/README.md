# Contratos versionados do Financeiro

- `messages/financial-events.v1.schema.json`: `QuoteCommunicated.v1`, `QuoteApproved.v1`, `QuoteRejected.v1` e `PaymentConfirmed.v1`.
- `messages/financial-events.v1.examples.json`: um exemplo válido por evento.
- `openapi/internal-v1.openapi.json`: reconciliação por `financial-status`.
- `common/`: envelope e erro HTTP v1.

Os eventos são somente contratos nesta etapa: não há publicação, Outbox funcional, SQS ou consumo. O endpoint interno está protegido e permanece explicitamente planejado para implementação funcional na Etapa 2.

