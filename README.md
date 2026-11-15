# MVP Oficina — Financeiro

Microsserviço da Fase 4 responsável por orçamentos, aprovação, recusa, comunicações,
pagamentos e histórico financeiro da oficina.

## Persistência e composição

O serviço usa uma tabela DynamoDB por ambiente, em modelo de tabela única. Orçamentos,
snapshots, comunicações, tentativas, guards, idempotência e histórico são persistidos
por chaves PK/SK e operações condicionais; `GSI1` atende consultas por OS e `GSI2`
reserva o acesso da futura Outbox.

A aplicação segue as fronteiras `domain` → `application` → ports. O composition root
em `create_app` instancia `DynamoDBFinanceRepository` e
o provider selecionado, constrói `FinanceService` e o expõe à apresentação. As
rotas não importam infraestrutura. O bootstrap idempotente da tabela é executado por
`scripts/start.sh` antes do Uvicorn e não durante requisições; testes criam tabelas
isoladas explicitamente.

## Comportamento financeiro

- A geração de orçamento exige contexto válido da OS e grava snapshots próprios.
- Comunicações registram localmente as evidências de WhatsApp e e-mail; nenhum envio
  externo ocorre nesta etapa.
- Guards e versão otimista protegem as transições de orçamento e pagamento.
- Chaves idempotentes evitam duplicação e conflito silencioso de tentativas.
- O histórico por OS preserva tentativas e estados financeiros, inclusive resultados
  técnicos inconclusivos.
- `PAYMENT_PROVIDER=simulated` permanece como padrão determinístico dos testes.
- `PAYMENT_PROVIDER=mercado_pago` cria uma cobrança PIX de teste com
  `X-Idempotency-Key=payment_id` e `external_reference=payment_id`.
- O identificador e a URL PIX retornados são vinculados condicionalmente à tentativa e
  ao `PAYMENT_GUARD` no DynamoDB.
- O webhook assinado é somente um gatilho: o serviço consulta `GET /v1/payments/{id}`
  antes de aplicar qualquer estado. Referência, valor, moeda e `live_mode=false` são
  validados contra a tentativa local.
- Aprovação grava a tentativa, o guard, o lock da referência e
  `PaymentConfirmed.v1` na mesma transação. Reentregas e fatos atrasados não duplicam
  a Outbox nem fazem um pagamento confirmado regredir.
- Timeout ou resposta inconclusiva resulta em `UNKNOWN`; `POST
  /api/v1/payments/{payment_id}/reconcile` consulta o estado autoritativo novamente.

A Outbox transacional e o publisher independente da Etapa 2 estão ativos. O worker
usa o GSI2 somente para localizar candidatos e adquire cada lease por `UpdateItem`
condicional, valida os quatro eventos financeiros v1 e os publica na FIFO de Ordens.
Inbox, consumers e orquestração da Saga permanecem fora deste checkpoint.

## Estrutura

| Caminho | Responsabilidade |
|---|---|
| `src/app/domain/` | modelos e exceções financeiras |
| `src/app/application/` | casos de uso e ports de persistência/pagamento |
| `src/app/infrastructure/` | adapter DynamoDB, provider simulado e bootstrap |
| `src/app/presentation/` e `src/app/api/` | injeção e rotas HTTP |
| `contracts/` | OpenAPI interno e eventos financeiros versionados |
| `tests/` | testes unitários, arquiteturais, contratuais e DynamoDB Local |
| `.github/workflows/ci.yml` | Ruff, suítes com cobertura mínima de 80% e build da imagem |

Os manifests Kubernetes específicos serão materializados somente na Etapa 4, junto da
preparação do deployment AWS. A Etapa 1 não contém placeholders Kubernetes.

## Execução e validação

```bash
make install
make lint
make test
make build
docker compose up --build
docker compose -f compose.test.yaml run --rm --build tests
docker compose -f compose.test.yaml down --volumes --remove-orphans
```

O health check fica em `GET /health` na porta `8003`. O CI provisiona DynamoDB Local e
ElasticMQ, executa bootstrap, lint, testes unitários/contratuais/de integração, gate
global de cobertura e build sem publicar a imagem.

## Publisher da Outbox

O publisher roda separado do FastAPI por `python -m app.publisher` ou
`scripts/start-publisher.sh`. Lote, polling, lease, timeouts, limite de tentativas e
backoff estão configurados por ambiente em `.env.example`; o endpoint SQS alternativo
é rejeitado em produção.

Os quatro eventos financeiros seguem para a FIFO Financeiro→Ordens com
`MessageGroupId=payload.order_id` e `MessageDeduplicationId=message_id`. A semântica é
at-least-once: se o broker aceitar o envio e a confirmação condicional no DynamoDB
falhar, o lease expira e o mesmo evento pode ser reenviado. A futura Inbox será a
proteção de negócio contra efeitos repetidos.

## Mercado Pago em ambiente de teste

Nenhuma credencial é aceita no código ou em arquivo `.env` versionado. Configure por
variáveis de ambiente ou Secret:

| Variável | Uso |
|---|---|
| `PAYMENT_PROVIDER` | `mercado_pago` para habilitar o adapter; o padrão é `simulated` |
| `MERCADO_PAGO_ACCESS_TOKEN` | Access Token **do ambiente Teste**, somente no backend |
| `MERCADO_PAGO_WEBHOOK_SECRET` | assinatura secreta exibida na configuração de Webhooks |
| `MERCADO_PAGO_NOTIFICATION_URL` | URL HTTPS pública terminando em `/api/v1/payments/mercado-pago/webhook` |
| `MERCADO_PAGO_API_BASE_URL` | padrão `https://api.mercadopago.com`; alterável só para mocks |
| `MERCADO_PAGO_CURRENCY` | `BRL` |
| `MERCADO_PAGO_*_TIMEOUT_SECONDS`, `MERCADO_PAGO_MAX_ATTEMPTS` | limites de timeout e retry |

Preparação do sandbox:

1. Crie/abra uma aplicação em [Suas integrações](https://www.mercadopago.com.br/developers/panel/app) e selecione o ambiente **Teste**.
2. Copie o Access Token de teste para um Secret. As [credenciais de teste](https://www.mercadopago.com.br/developers/pt/docs/checkout-api-orders/create-application) não processam pagamentos reais.
3. Em Webhooks, cadastre a URL HTTPS pública e selecione notificações de pagamentos;
   copie a assinatura secreta para outro campo do Secret. A validação segue o
   [manifesto HMAC-SHA256 oficial](https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/payment-notifications).
4. Reinicie apenas a API Financeiro com as variáveis acima. Não é necessário alterar
   banco, volume, Saga, contratos ou os demais serviços.
5. Crie uma tentativa PIX pela API. A resposta contém `checkout_url`; a confirmação
   só ocorrerá após notificação válida e consulta autoritativa, ou após reconciliação
   explícita.

Para o teste ponta a ponta, exponha temporariamente o webhook por uma URL HTTPS
controlada e use somente credenciais/conta de teste. Consulte as [regras atuais para
compras de teste](https://www.mercadopago.com.br/developers/pt/docs/checkout-api-payments/integration-test/make-test-purchase).
