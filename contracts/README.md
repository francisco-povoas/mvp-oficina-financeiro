# Contratos versionados do Financeiro

- `messages/financial-events.v1.schema.json`: `QuoteCommunicated.v1`, `QuoteApproved.v1`, `QuoteRejected.v1` e `PaymentConfirmed.v1`.
- `messages/financial-events.v1.examples.json`: um exemplo válido por evento.
- `openapi/internal-v1.openapi.json`: reconciliação por `financial-status`.
- `common/`: envelope e erro HTTP v1.

Os eventos continuam somente como contratos nesta frente: não há publicação, Outbox funcional, SQS ou consumo. O endpoint interno de reconciliação está protegido e funcional.

## OpenAPI 1.1.0

- `PaymentStatus.status` passou a aceitar `UNKNOWN` nas consultas e reconciliações, sem alterar transições ou o `PAYMENT_GUARD`.
- `Problem` foi alinhado ao schema comum, incluindo os limites de `message` e os campos opcionais `operation_id` e `details`.
- respostas de sucesso e erro declaram `X-Correlation-ID`; as rotas internas exigem um UUID nesse header.
- respostas 422 e 500 passaram a ser declaradas explicitamente, sempre no formato `Problem`.
- `financial-status` consulta o histórico próprio por GSI, identifica orçamento e tentativa de pagamento com suas versões e diferencia ausência, estados pendentes, `UNKNOWN` e confirmação.

