# Changelog

## 0.6.0 — 2026-09-27

### Alterado — a pesquisa profunda só acontece mediante a flag `--deep-research`
- **Gatilho único e verificável**: o modo pesquisa profunda deixa de ser ativado
  por contexto ("pesquisa profunda", "deep research", "investiga a fundo",
  "revisão da literatura"…) e **só acontece quando a invocação leva a flag
  `--deep-research`**. Sem a flag: `research init|lint` recusam com exit 2
  (`Erro:` + `Solução: …`) e o `search` faz **sempre** pesquisa simples — o
  SKILL.md manda, nesse caso, fazer a pesquisa simples e avisar que o modo
  profundo exige a flag.
- **`search --deep-research "pergunta"`**: nova porta de entrada do modo —
  equivalente a `research init --deep-research` (kickoff do protocolo: cria o
  dossiê `pesquisas/AAAA-MM-DD-<slug>.md`, offline e sem gastar créditos; as
  restantes opções de `search` não se aplicam neste modo).
- **`research init|lint --deep-research`**: a flag passa a ser **obrigatória**
  nos dois subcomandos (`--out` e `--json` mantêm-se; o caminho de `--out`
  também entra pelo `research init --deep-research`).
- Documentação coerente com o comportamento real: `SKILL.md` (gatilho único,
  regra de ouro nova, tabela de comandos e opções), `README.md`,
  `references/pesquisa-profunda.md` (porta de entrada) e `references/api.md`.
- `selftest`: novo cenário do portão da flag (recusa sem ela, nada criado;
  kickoff com ela; `search --deep-research` cria o dossiê) — agora **121**
  cenários offline.

## 0.5.0 — 2026-09-27

### Adicionado — modo pesquisa profunda
- **Protocolo** `references/pesquisa-profunda.md`: brief como estrela-guia,
  decomposição por perspetivas × facetas (STORM), dependências
  (*least-to-most*/*self-ask*), graduação de esforço, orquestrador +
  investigadores em paralelo sem teto, integração com níveis de fonte A–D e
  confiança tipo GRADE, contradições como sub-perguntas, auditoria de lacunas
  (checklist + crítico de contexto limpo + `research lint`), critérios de
  paragem e regra de estagnação, verificação adversarial (3 votos, 2/3 para
  derrubar), síntese por redator único e modelos de delegação prontos a usar
  (investigador, verificador, crítico) com retorno só em JSON. Desenhado a
  partir de três rondas de pesquisa com verificação adversarial e fontes
  primárias (Anthropic, LangChain, Gemini, STORM/Co-STORM, IterDRAG, FAIR-RAG,
  OpenAI, Microsoft, CaMeL, padrões de desenho, ataques adaptativos, FORGE/RQA,
  GRADE, SIFT, Wineburg & McGrew, estudos de citações inventadas).
- **Onde pesquisar** `references/fontes-de-pesquisa.md`: bases académicas e
  APIs abertas, presets de domínios, operadores, *snowballing* de citações e
  verificação de citações (DOI, retratações).
- **Escudo nativo anti-injeção** (`references/escudo-injecao.md`): em TODO o
  texto vindo da web (`search` e `extract`) — higienização de invisíveis
  (tags Unicode/«ASCII smuggling», bidi, zero-width), neutralização de
  marcadores de papel (`<|im_start|>`, `[INST]`, `<tool_call>`…), deteção
  EN/PT/ES com risco por fonte (`shield`, `meta.shield`, `⚠ escudo` no texto),
  `--quarantine` e envelope com nonce inforjável no `extract`.
- **`extract <url…>`**: texto integral de 1..20 URLs pela mesma máquina de
  rotação (bans, round-robin, concorrência); `--query`/`--chunks` para trechos
  relevantes, `--depth`, `--format`, `--json`; prazo do servidor abaixo do do
  cliente (uma página lenta não bane a chave); orçamento justo entre fontes;
  créditos por `usage.credits` ou 1–2 por cada 5 URLs.
- **Filtros de `search`**: `--preset academico|saude|computacao|oficial`,
  `--include-domains`, `--exclude-domains`, `--prefer-domains`, `--time-range`,
  `--start-date`/`--end-date`, `--exact`, `--topic finance`; a data de
  publicação (`include_published_date`) vem sempre. Erros de filtro são
  detetados antes do pedido.
- **`research init|lint`**: cria o dossiê Markdown (brief, síntese, FAQ em
  árvore, registo de rondas, matriz de evidência, contradições, fontes,
  incidentes de segurança, limitações, metodologia) e valida-o — linhagem da
  FAQ, estados/confiança, citações fantasma, fontes sem URL/DOI, triangulação,
  conclusão prematura, imagens remotas/HTML ativo/invisíveis — com veredito
  `CONTINUAR` (e a lista mínima da próxima ronda) ou `PRONTO-PARA-SINTESE`.
- **`shield [ficheiro|-]`**: escudo sobre qualquer texto (ex.: o retorno de um
  subagente — injeção de 2.ª ordem); `--json`, `--sanitize`.
- **Bibliotecário em série**: as APIs académicas (arXiv, Semantic Scholar,
  Crossref, OpenAlex) têm limites que somam todos os processos (o arXiv proíbe
  contorná-los) — um único papel faz DOI/metadados/retratações/snowballing.
- **`references/exemplo-dossie.md`**: dossiê real e concluído (a pesquisa que
  desenhou este modo: 3 rondas, 321 subagentes, 75 afirmações sob verificação
  adversarial), validado pelo `lint` no selftest.
- Escudo também contra **emoji smuggling** (seletores de variação em série,
  descodificados só para análise) e controlos C0/C1 (ESC/ANSI) — técnicas que
  evadiram detetores comerciais em 80–100 % dos casos.
- Selftest: 103 → **120 cenários** (filtros, domínios/datas, escudo com
  positivos e falsos positivos, texto oculto, envelope, quarentena, `extract`
  com rotação/créditos/orçamento justo, dossiê/lint, `shield`, CLI sem
  traceback).

### Mudado
- O ciclo de rotação passou a ser partilhado por `/search` e `/extract`
  (`_rotating_request`) — comportamento de `search` inalterado (os 103
  cenários anteriores continuam verdes).

## 0.4.0 — 2026-09-26

### Mudança de contrato (pedido do utilizador)
- **Rotação com troca de chave a cada chamada**: a rotação passa a ser
  **round-robin estrito** com cursor global persistente — cada chamada usa a
  PRÓXIMA chave da vez e a chave da chamada anterior nunca se repete enquanto
  houver alternativas (A→B→A→B…). Sai a rotação orientada ao saldo: o saldo
  estimado continua a ser calculado e apresentado (`status`, `keys list`), mas
  **não decide a ordem**.
- **Uma chave que falha fica de fora por 1 dia**: qualquer falha da
  credencial — 429 rate limit, 401 chave morta, 432/433 cota, 5xx, timeout ou
  rede — resulta em **ban de 24 h** (`--ban-hours` / `TAVILY_BAN_HOURS`):
  durante o prazo a chave **não é selecionável** em processo nenhum e volta
  sozinha ao fim dele (ou antes, com `keys unban`). O `Retry-After` do servidor
  passa a ser ignorado (o ban é fixo). Erros do pedido (400/403) não banem.

### Adicionado
- **Sistema global de controlo `keys`**: chaves cadastradas num registo comum
  (`keys.json`, 0600, fora do repo, no `$TAVILY_STATE_DIR`) que valem para
  QUALQUER agente/terminal — `keys add [--label] [--from-env VAR]` · `remove` ·
  `enable`/`disable` (fora/para dentro da rotação, globalmente) · `unban
  [--all]` (readmissão imediata de bans) · `next` (próxima da rotação) · `list`
  (estado, ban restante, próxima). Seletores por `#índice`, nome/etiqueta,
  `…últimos4` ou hash. As env vars (`TAVILY_API_KEY[_A..Z]`) continuam a
  funcionar como pool adicional, deduplicadas por valor.
- Novos comandos/opções: `search --ban-hours` · `search|status|keys --keys-file`
  · variáveis `TAVILY_BAN_HOURS` e `TAVILY_KEYS_FILE`.
- `status` mostra o **ban restante** de cada chave (`FORA DE ROTAÇÃO por mais
  …`) e a **próxima chave da rotação**; `keys list` mostra a tabela global.
- Novo estado `SUSPENDED` para ban por falha transitória (5xx/rede), com o
  último erro sempre visível.
- Selftest: 96 → **103 cenários** (ban de 24 h por classe de falha, round-robin
  estrito, banidos não selecionáveis, revive ao fim do ban, `keys`
  add/remove/disable/enable/unban/next, `keys.json` corrompido, prioridade do
  `--ban-hours`).

### Removido
- Rotação por saldo, backoff exponencial de 429, teto de 1 h no `Retry-After`,
  repete-de-2.ª-ronda para falhas transitórias (`TRANSIENT_STRIKES`) e a
  suspensão de cota até ao mês seguinte — substituídos pelo ban plano de 24 h.
  O parser de `Retry-After` (delta/data HTTP) sai com eles.

### Compatibilidade
- Entradas `REVOKED` sem prazo escritas pela v0.3.x continuam permanentes
  (só `keys unban`/`--reset-state` as libertam); com prazo, revivem ao fim.
- `pool-state.json` mantém o formato (`version: 1`); `keys.json` é aditivo.

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
