# MVP Oficina — Financeiro

Microsserviço responsável por orçamentos, aprovação, recusa, comunicações e pagamentos da oficina.

## Estado atual

O esqueleto executável independente da Etapa 1 está disponível. O modelo DynamoDB de orçamento e pagamento será extraído incrementalmente conforme `docs/architecture-boundary.md`.

## Estrutura inicial

- `src/app/`: aplicação FastAPI e futuras camadas de domínio;
- `tests/`: testes próprios do serviço;
- `docs/`: contratos e documentação próprios;
- `compose.yaml`: API e DynamoDB Local de desenvolvimento;
- `compose.test.yaml`: testes isolados com DynamoDB Local em memória.

O serviço usará DynamoDB e manterá snapshots financeiros próprios, sem reutilizar modelos ORM ou consultar bancos dos demais serviços.

## Comandos

```bash
make install
make lint
make test
make build
docker compose up --build
```

O health check fica disponível em `GET /health` na porta `8003`.
