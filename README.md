# MVP Oficina — Financeiro

Microsserviço responsável por orçamentos, comunicação com clientes, aprovação,
recusa, pagamentos e histórico financeiro da oficina.

> **Limitação conhecida:** o adapter Mercado Pago PIX, a assinatura de webhook e os
> testes com doubles estão implementados, mas a integração ponta a ponta com o sandbox
> real do Mercado Pago ainda não foi validada.

## Sumário

- [Responsabilidades](#responsabilidades)
- [Tecnologias e persistência](#tecnologias-e-persistência)
- [Orçamentos e comunicação](#orçamentos-e-comunicação)
- [Pagamentos](#pagamentos)
- [Mercado Pago e provedor simulado](#mercado-pago-e-provedor-simulado)
- [Webhooks, idempotência e PAYMENT_GUARD](#webhooks-idempotência-e-payment_guard)
- [Outbox e eventos](#outbox-e-eventos)
- [Execução local](#execução-local)
- [Swagger](#swagger)
- [Testes e cobertura](#testes-e-cobertura)

## Responsabilidades

- criar orçamento a partir do contexto versionado consultado em Ordens;
- persistir snapshots dos serviços, peças, contato e totais;
- registrar evidências de comunicação por WhatsApp e e-mail;
- aprovar ou recusar somente orçamento comunicado;
- criar e acompanhar tentativas de pagamento;
- integrar o provedor simulado e o adapter Mercado Pago PIX;
- validar webhooks e reconciliar resultados inconclusivos;
- publicar fatos financeiros duráveis para Ordens.

Financeiro não altera diretamente a OS e não consulta o banco de Ordens. A integração
usa REST autenticado e eventos versionados.

## Tecnologias e persistência

- Python 3.12, FastAPI, Pydantic e Uvicorn;
- DynamoDB/DynamoDB Local com boto3 e operações condicionais;
- SQS FIFO/ElasticMQ para a Outbox financeira;
- HTTPX no adapter Mercado Pago e na integração REST com Ordens;
- JWT HS256, JSON Schema, OpenTelemetry, Pytest, pytest-cov e Ruff.

O modelo usa uma tabela DynamoDB por ambiente com `PK`/`SK`, `GSI1` para consultas
por OS e `GSI2` para localizar a Outbox pendente. Orçamentos, comunicações, tentativas
de pagamento, idempotência, locks de referência e guards ficam em documentos
independentes e auditáveis. Valores monetários usam `Decimal`.

## Orçamentos e comunicação

Financeiro consulta `GET /internal/v1/orders/{id}/quote-context` em Ordens. O
contexto precisa indicar `DIAGNOSTICO_CONCLUIDO` e traz versão, contato e snapshots
ativos.

Estados do orçamento:

```text
RASCUNHO → COMUNICADO → APROVADO
                      └→ RECUSADO
```

A comunicação exige evidência de sucesso para WhatsApp e e-mail. Nesta entrega, as
evidências são registradas localmente; não há envio real para esses canais. Aprovação
e recusa usam atualização condicional e publicam, respectivamente,
`QuoteApproved.v1` ou `QuoteRejected.v1`.

## Pagamentos

Cada tentativa possui documento e histórico próprios. Estados implementados:
`PENDING`, `PROCESSING`, `CONFIRMED`, `FAILED`, `EXPIRED`, `REJECTED` e
`UNKNOWN`.

Antes de criar uma tentativa, Financeiro consulta o contexto de pagamento em Ordens e
confirma OS `FINALIZADA`, orçamento aprovado, versão e total. Estados ativos ou
`CONFIRMED` bloqueiam outra tentativa. Uma nova tentativa só é permitida após
`FAILED`, `EXPIRED` ou `REJECTED`, com nova chave de idempotência.

## Mercado Pago e provedor simulado

`PAYMENT_PROVIDER=simulated` é o padrão local e de CI. Ele permite confirmar o
pagamento de forma determinística sem chamada externa.

`PAYMENT_PROVIDER=mercado_pago` seleciona o adapter PIX. Ele cria a cobrança com
`X-Idempotency-Key=payment_id`, usa `external_reference=payment_id` e armazena a
referência e a URL de checkout de forma condicional. Tokens e segredos devem ser
injetados por ambiente/Secret.

O código e os testes automatizados cobrem esse adapter, porém **não existe evidência
de validação com credenciais de teste e webhook HTTPS no sandbox real do Mercado
Pago**.

## Webhooks, idempotência e PAYMENT_GUARD

O endpoint `POST /api/v1/payments/mercado-pago/webhook` valida a assinatura
HMAC-SHA256. O webhook é apenas um gatilho: antes de mudar estado, Financeiro consulta
o pagamento na API do provedor e valida referência, valor, moeda e
`live_mode=false`.

Proteções implementadas:

- chave de idempotência repetida com o mesmo payload devolve a tentativa existente;
- a mesma chave com payload divergente retorna conflito;
- `PAYMENT_GUARD` permite no máximo uma tentativa ativa e uma confirmação definitiva
  por OS;
- lock por referência externa impede associá-la a dois pagamentos;
- timeout ou resposta inconclusiva gera `UNKNOWN`, nunca confirmação presumida;
- `POST /api/v1/payments/{payment_id}/reconcile` consulta novamente a fonte
  autoritativa;
- fatos atrasados ou repetidos não fazem um pagamento confirmado regredir.

## Outbox e eventos

As transições financeiras e a Outbox são gravadas na mesma transação lógica do
DynamoDB. O publisher usa lease condicional, valida o JSON Schema e envia para a FIFO
Financeiro → Ordens:

- `QuoteCommunicated.v1`;
- `QuoteApproved.v1`;
- `QuoteRejected.v1`;
- `PaymentConfirmed.v1`.

A entrega é *at-least-once*. Ordens usa Inbox e idempotência de negócio para absorver
reentregas. Falha de publicação não desfaz uma decisão financeira já persistida.

## Execução local

Para executar o sistema completo com DynamoDB Local, ElasticMQ e os demais serviços,
use o [README de Infra](../mvp-oficina-infra/README.md#subir-o-ambiente-no-minikube).

Preparação do serviço no host:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
make install
```

As variáveis estão descritas em `.env.example`. O processo runtime executa o
bootstrap idempotente da tabela e inicia o Uvicorn por `scripts/start.sh`. O
publisher é separado:

```bash
python -m app.publisher
```

Para validar o fluxo completo com o provedor simulado:

```bash
cd ../mvp-oficina-infra
make test-bdd
```

## Swagger

Com o Checkpoint A ativo:

```bash
kubectl --context=minikube -n mvp-oficina port-forward service/financeiro-api 8003:8003
```

Swagger UI: <http://127.0.0.1:8003/docs>

OpenAPI JSON: <http://127.0.0.1:8003/openapi.json>

O Kong publica somente os prefixos `/api/v1/quotes` e `/api/v1/payments`; ele não
publica Swagger, OpenAPI nem rotas internas.

## Testes e cobertura

```bash
make lint
make test
```

O gate de cobertura de branches é 80%, conforme `pyproject.toml`. Para executar a
suíte isolada com DynamoDB Local e ElasticMQ:

```bash
docker compose -f compose.test.yaml run --rm --build tests
docker compose -f compose.test.yaml down --volumes --remove-orphans
```

## Documentação relacionada

- [Arquitetura distribuída](../mvp-oficina-infra/docs/diagrams/arquitetura.md)
- [Saga e comunicação](../mvp-oficina-infra/docs/diagrams/saga-comunicacao.md)
- [Contratos financeiros](contracts/README.md)
- [README principal de Infra](../mvp-oficina-infra/README.md)
