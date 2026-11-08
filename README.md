# MVP Oficina — Financeiro

Microsserviço responsável por orçamentos, aprovação, recusa, comunicações e pagamentos da oficina.

## Estado atual

O recorte independente da Etapa 1 está disponível com persistência DynamoDB para
orçamentos, snapshots e histórico de tentativas de pagamento. Guards, idempotência,
referências externas, concorrência otimista e a estrutura persistente da Outbox são
validados contra DynamoDB Local.

A geração exige contexto da OS em `DIAGNOSTICO_CONCLUIDO`, ao menos um serviço
ativo e snapshots com e-mail e telefone. A comunicação é um registro local de
evidências: uma tentativa de WhatsApp e uma de e-mail por rodada. Somente uma rodada
com as duas evidências bem-sucedidas promove o orçamento de `RASCUNHO` para
`COMUNICADO`. O serviço não envia mensagens externas nesta etapa; o histórico pode ser
consultado em `GET /api/v1/quotes/{quote_id}/communications`.

## Estrutura inicial

- `src/app/`: aplicação FastAPI e camadas próprias de domínio e persistência;
- `tests/`: testes próprios do serviço;
- `docs/`: contratos e documentação próprios;
- `compose.yaml`: API e DynamoDB Local de desenvolvimento;
- `compose.test.yaml`: testes isolados com DynamoDB Local em memória.

O serviço usa uma tabela DynamoDB por ambiente e mantém snapshots financeiros próprios,
sem reutilizar modelos ORM ou consultar bancos dos demais serviços. A Outbox ainda é
somente estrutural: publicação, polling, workers, Inbox e mensageria pertencem à Etapa 2.

## Comandos

```bash
make install
make lint
make test
make build
docker compose up --build
docker compose -f compose.test.yaml run --rm --build tests
docker compose -f compose.test.yaml down --volumes --remove-orphans
```

O health check fica disponível em `GET /health` na porta `8003`.
