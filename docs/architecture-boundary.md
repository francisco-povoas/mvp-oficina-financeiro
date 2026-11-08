# Limite arquitetural de Financeiro

Financeiro é dono de orçamentos, comunicações, aprovação, recusa, tentativas de pagamento e confirmação financeira. Seus documentos contêm snapshots suficientes para auditoria e referenciam `order_id` sem foreign key ou acesso ao banco de Ordens.

A persistência usa uma tabela DynamoDB por ambiente, chaves `PK`/`SK`, `GSI1` para consultas por OS e `GSI2` reservado ao Outbox. Não há ORM PostgreSQL neste serviço.

Nesta etapa, modelos, guards condicionais, idempotência e estrutura persistente do Outbox podem ser preparados. Publicação, polling, Inbox, workers e SQS/ElasticMQ permanecem na Etapa 2. Mercado Pago permanece na Etapa 5.
