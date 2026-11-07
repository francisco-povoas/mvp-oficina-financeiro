# Instruções — mvp-oficina-financeiro

- Preserve o monólito original; use-o somente como fonte de regras e testes legados.
- Este repositório é dono de orçamentos, aprovação, recusa, comunicações e pagamentos.
- Use DynamoDB com documentos e operações condicionais; não reutilize ORM PostgreSQL.
- Preserve idempotência financeira e histórico de tentativas de pagamento.
- Mercado Pago e SonarQube permanecem fora do escopo até a Etapa 5.
- Não compartilhe domínio, persistência ou banco com outros serviços.
- Integrações externas devem usar contratos versionados e transações locais.
- Nunca versione segredos, tokens, credenciais ou arquivos `.env`.
- Use o fluxo `feature/* -> main`; não faça push adicional sem autorização da atividade em curso.
