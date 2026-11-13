# MVP Oficina — Financeiro

Microsserviço da Fase 4 responsável por orçamentos, aprovação, recusa, comunicações,
pagamentos e histórico financeiro da oficina.

## Estado da Etapa 1

O serviço usa uma tabela DynamoDB por ambiente, em modelo de tabela única. Orçamentos,
snapshots, comunicações, tentativas, guards, idempotência e histórico são persistidos
por chaves PK/SK e operações condicionais; `GSI1` atende consultas por OS e `GSI2`
reserva o acesso da futura Outbox.

A aplicação segue as fronteiras `domain` → `application` → ports. O composition root
em `create_app` instancia `DynamoDBFinanceRepository` e
`SimulatedPaymentProvider`, constrói `FinanceService` e o expõe à apresentação. As
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
- A confirmação usa um provider simulado atrás de `PaymentProviderPort`; Mercado Pago
  não está implementado.

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

## Próximas integrações

Checkpoints posteriores implementarão consumers, Inbox e orquestração da Saga.
Infraestrutura Kubernetes/AWS será preparada na Etapa 4. O adapter real do Mercado
Pago, Pix e webhooks pertence somente à Etapa 5.
