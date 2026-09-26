# Changelog

## 0.2.1 — 2026-09-26

### Removido
- **Plugin DSH `dsh-tavily-resilient-search` eliminado por completo** (projeto
  `~/Projects/dsh-tavily-accounts`, estado `~/.dsh/dsh-tavily-resilient-search`,
  registos do plugin-manager e entrada `minimumReleaseAgeExclude` do perfil web).
  Esta skill passa a ser a **única implementação** de pesquisa Tavily da máquina.
- Todas as referências ao plugin na documentação e no script.

## 0.2.0 — 2026-09-26

### Adicionado
- **Registo interno persistente do pool** (`pool-state.json`, 0600, só hashes —
  nunca material de chave): estado por chave, cooldowns, contadores, último
  erro, consumo real (`/usage`) e cursor de round-robin. A invocação seguinte
  não recomeça do início nem re-tenta chaves mortas. `status --reset-state`
  limpa; `--no-state`/`--state-dir`/`TAVILY_STATE_DIR` controlam.
- **Limite de requests simultâneas por conta** (cross-processo, predef. 2,
  TTL anti-processo-morto): conta no teto é saltada; todas ocupadas → espera e
  retry da MESMA request até `--max-wait`; sem espera → contingência keyless.
  `--max-inflight-per-key` / `TAVILY_MAX_INFLIGHT_PER_KEY`.
- **`status --check` via `GET /usage`**: validação ao vivo sem gastar créditos
  de pesquisa (limite próprio: 10 req/10 min) e registo de usados/restantes.
- **Merge cross-processo do registo**: contadores por delta (sem lost updates)
  e `REVOKED` absorvente (vistas antigas não ressuscitam chaves mortas).
- Documentação dos limites reais da API (100/1000 RPM, `/usage`, 432/433).

### Corrigido
- Diretoria de estado não gravável rebentava com traceback: agora degrada em
  modo memória e a pesquisa continua (contrato `Erro:`/`Solução:` preservado).
- Modo keyless não emitia o aviso em stderr prometido pela documentação.
- `pool-state.lock` criado com 0644 → 0600.

## 0.1.0 — 2026-09-24

- Versão inicial: `search`/`status`/`selftest`, rotação automática de chaves
  (401/429/432/433/5xx/rede), contingência keyless, redação de segredos,
  orçamento de contexto (50 KB) e 11 cenários de selftest offline.
