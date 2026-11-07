# MVP Oficina — Financeiro

Microsserviço responsável por orçamentos, aprovação, recusa, comunicações e pagamentos da oficina.

## Estado atual

Este repositório contém somente a estrutura mínima inicial da Etapa 1 da Fase 4. Nenhum módulo do monólito foi segregado ainda.

## Estrutura inicial

- `src/`: código-fonte futuro do serviço;
- `tests/`: testes próprios do serviço;
- `docs/`: contratos e documentação próprios.

O serviço usará DynamoDB e manterá snapshots financeiros próprios, sem reutilizar modelos ORM ou consultar bancos dos demais serviços.
