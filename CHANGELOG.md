# Changelog

## 0.3.0 — 2026-09-26

### Adicionado
- **Rotação orientada ao saldo**: com mais do que uma conta ACTIVE, é preferida a
  de maior saldo estimado (`remaining` do último `/usage` menos os créditos
  gastos desde então). Contas com saldo igual ou desconhecido continuam em
  round-robin a partir do cursor persistido; sem nenhum saldo conhecido o
  comportamento é o round-robin anterior (A→B→C). Saldos de um mês UTC
  anterior (cotas repostas no dia 1) contam como desconhecidos.
- **Créditos reais por pesquisa**: o pedido leva `include_usage` e o
  `usage.credits` da resposta é somado a `credits_spent` da conta (fallback:
  `advanced` = 2, restantes = 1). É isto que mantém a estimativa de saldo viva
  entre consultas a `/usage` — sem ela a "conta mais cheia" seria sempre a mesma.
- **Saldo refrescado sozinho no `status`**: sem `--check`, as contas com saldo
  registado há mais de 60 min são consultadas via `GET /usage` (timeout de
  5 s), com no máximo 1 consulta por conta a cada 6 min — reservada
  atomicamente no registo, por isso N `status` em paralelo fazem 1 consulta —
  para nunca rebentar o limite de 10 req/10 min. `status --no-refresh` desliga.
- **`selftest --live`** (opt-in, ≈1 crédito por conta): depois do selftest
  offline, prova o pipeline real com 1 consulta `/usage` + exatamente 1
  pesquisa `ultra-fast` por conta (sem rotação, retry nem keyless), sem mexer
  no registo persistente; variáveis com formato inválido contam como FAIL.
  Nunca corre na CI.
- **CI** (GitHub Actions): `selftest` + `py_compile` em cada push/PR, em
  Python 3.10, 3.12 e 3.14.
- **Nome canónico no `status`**: quando `TAVILY_API_KEY` tem o mesmo valor de
  `TAVILY_API_KEY_A`, a entrada chama-se `TAVILY_API_KEY_A` e o alias aparece
  como nota (`alias: TAVILY_API_KEY`); o pool não duplica.
- **Validação do formato das chaves**: valores com espaços, aspas (incluindo
  tipográficas), quebras de linha, caracteres não-ASCII ou < 8 caracteres são
  ignorados com aviso (`[FORMATO INVÁLIDO]` no `status`) em vez de rebentarem
  no cabeçalho HTTP.
- Selftest: 19 → 96 cenários (Retry-After com data HTTP, `/usage` real,
  `resolve_max_inflight`, `load_keys`, saldo, refrescamento sem rajadas,
  status ponta a ponta, merge cross-processo, registo corrompido, bordas do
  processo), herméticos e sem rede. Validados por revisão adversarial
  multi-agente (58 defeitos confirmados e corrigidos) e por mutation testing
  (55 mutações dirigidas: todas mortas exceto 1 equivalente).

### Corrigido — rotação e esperas
- A espera por contas ocupadas (teto de concorrência) acabava em ~5 s por
  esgotar um orçamento fixo de iterações, sem chegar a tentar o keyless; agora
  vai até `--max-wait` e só depois recorre ao keyless.
- Falhas transitórias (5xx/rede/timeout) eram repetidas sem backoff até ~20×
  por passagem (com timeouts, vários minutos pendurados) e nunca chegavam ao
  keyless; agora cada credencial tem no máx. 2 falhas transitórias por
  invocação, com backoff curto entre rondas, e cada conta é tentada no máx.
  uma vez por passagem.
- Uma chave REVOKED no pool impedia a espera pelo cooldown das restantes
  (a pesquisa desistia em vez de esperar ~2 s por uma conta em rate limit).
- `Retry-After: 0` (ou uma data no passado) gerava uma rajada contra a chave
  limitada — piso de 0,5 s; valores absurdos limitados a 1 h. O backoff
  exponencial sem `Retry-After` nunca escalava (as falhas eram zeradas ao
  reviver); agora só um sucesso as zera.
- `Retry-After` em data HTTP dependia do locale (`strptime`); passa a
  `email.utils` (RFC 9110).

### Corrigido — registo persistente
- Um processo com uma vista antiga do registo desfazia `status --reset-state`
  e `status --check` ao terminar (ex.: uma chave REVOKED voltava para sempre);
  `--reset-state --check` regravava o registo anterior; apagar o ficheiro à mão
  ressuscitava-o; `--reset-state` era ignorado sem chaves. Cada processo passa
  a registar só o que observou.
- `status --check` sobrescrevia contadores gravados por pesquisas concorrentes;
  agora só as chaves verificadas impõem estado e os contadores acumulam por delta.
- O consumo (`usage`) de uma vista antiga podia sobrepor-se a um `/usage` mais
  recente — vence o `checked_at` mais recente (carimbos no futuro são inválidos).
- Com o teto de concorrência a 0, o fim de cada request libertava um slot de
  OUTRO processo; e slots com TTL fixo de 120 s expiravam a meio de pedidos
  longos (`--timeout` alto). Cada processo liberta agora só o seu slot, e os
  slots guardam a sua expiração (≥ 2 × timeout).
- Registos corrompidos mas em JSON válido (NaN/Infinity, inteiros gigantes,
  surrogates, aninhamento absurdo, tipos inesperados) rebentavam pesquisas e
  o próprio `--reset-state`; agora são reduzidos aos campos conhecidos.

### Corrigido — contrato de saída e segredos
- Uma chave colada com quebra de linha ou aspas tipográficas rebentava fora da
  rotação (sem tentar as outras chaves) e, com `--verbose`, o traceback
  mostrava-a em claro.
- Erros do argparse ecoavam o valor recusado (ex.: `--api-key "$CHAVE"`) sem
  redação; `--verbose` e mensagens de erro também não eram redigidos; uma
  chave que fosse prefixo de outra deixava escapar o resto.
- stdout sem UTF-8 (ascii/latin-1), surrogates na resposta da API, leitor que
  fecha o pipe (`| head`), consulta com bytes não-UTF-8, respostas HTTP
  malformadas e valores fora de gama (`--timeout 1e12`, `--max-wait inf`)
  davam traceback ou "falha interna" depois de gastar créditos; agora são
  erros instrutivos ou tratados. Rede de segurança final em `main()`.
- `status` anunciava "saldo refrescado" mesmo quando a consulta falhava, e
  somava timeouts por conta sem rede; agora diz quais falharam, pára à
  primeira falha de rede e cada consulta automática tem um prazo total (DNS
  incluído). Um 200 sem dados de consumo (proxy/portal cativo) deixou de apagar
  o saldo conhecido e de "validar" a conta no `--check`.
- `--help` com o pipe fechado pelo leitor saía com exit 120 e aviso do
  interpretador; uma pesquisa concorrente com vista antiga regravava falhas
  consecutivas/último erro anteriores a um `--reset-state`.

## 0.2.1 — 2026-09-26

### Removido
- A antiga integração de pesquisa Tavily do DeepSeek Harness foi eliminada por
  completo desta máquina (projeto, estado, registos e configuração associados).
  Esta skill passa a ser a **única implementação** de pesquisa Tavily da máquina.
- Todas as menções a essa integração na documentação e no script.

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
