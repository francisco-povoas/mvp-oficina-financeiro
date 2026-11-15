# CI e qualidade

O workflow `.github/workflows/ci.yml` é executado em pull requests para `main` e
também pode ser iniciado manualmente. DynamoDB Local e ElasticMQ são serviços
efêmeros do job; tabela e fila FIFO são criadas de forma idempotente antes dos
testes e os containers são removidos mesmo quando um check falha.

Com Python 3.12 e as variáveis/serviços descritos no workflow ativos, os comandos
equivalentes são:

```bash
python -m pip install -r requirements-dev.txt
python -m app.infrastructure.bootstrap
python .github/scripts/bootstrap_ci_services.py
python -m ruff check src tests .github/scripts
python -m pytest --cov=src/app --cov-report=term-missing \
  --cov-report=xml:reports/coverage.xml --junitxml=reports/junit.xml
python -m radon cc src/app --json > reports/radon-complexity.json
python -m radon mi src/app --json > reports/radon-maintainability.json
python -m xenon --max-absolute D --max-modules C --max-average B src/app
docker build --target test -t mvp-oficina-financeiro:test-local .
docker build --target runtime -t mvp-oficina-financeiro:runtime-local .
```

O Pytest lê `fail_under = 80` do `pyproject.toml`. Xenon reprova blocos acima de
D, módulos acima de C ou média acima de B. Os XMLs e relatórios Radon são
publicados como um único artefato por execução, inclusive quando os testes falham.
