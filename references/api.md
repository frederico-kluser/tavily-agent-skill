# Contrato da API Tavily, a máquina de rotação e o escudo (Nível 3)

Carregue este ficheiro só quando precisar de interpretar `--verbose`, afinar
`--timeout`/`--max-wait`/`--ban-hours`, ou perceber exatamente o que o script
faz quando uma request morre.

## Endpoint usado

`POST https://api.tavily.com/search` — pesquisa contextualizada com ranking,
desenhada para agentes/RAG (entrega excertos limpos, não HTML bruto).

Corpo enviado por `scripts/tavily.py search`:

```json
{
  "query": "...",
  "search_depth": "basic",
  "max_results": 5,
  "topic": "general",
  "include_answer": true,
  "chunks_per_source": 3,
  "include_usage": true
}
```

`include_usage` faz a resposta trazer `usage.credits` (créditos realmente
gastos): o script soma-os a `credits_spent` da conta que serviu — é o que
mantém viva a estimativa de saldo entre consultas a `/usage` (sem o campo,
usa o custo documentado: `advanced` = 2, `basic`/`fast`/`ultra-fast` = 1).
`include_published_date` (sempre enviado, sem custo) traz a data de publicação
estimada de cada resultado — aparece entre parênteses no texto e como
`published_date` no JSON. `include_raw_content` nunca é enviado (explosão de
contexto): o texto integral lê-se com `extract`, sob orçamento.

Filtros opcionais (só entram no corpo quando usados — pesquisa profunda):

| Opção do script | Campo da API | Notas |
| --- | --- | --- |
| `--preset NOME` (repetível) | `include_domains` | listas curadas (`academico`, `saude`, `computacao`, `oficial`) — ver `fontes-de-pesquisa.md` |
| `--include-domains a.org,b.org` | `include_domains` | máx. 300; só domínios (URLs são reduzidos ao domínio; curingas/caminhos recusados) |
| `--prefer-domains` | `include_domains_mode: "prefer"` | prioriza os domínios em vez de restringir |
| `--exclude-domains …` | `exclude_domains` | máx. 150 |
| `--time-range day\|week\|month\|year` | `time_range` | |
| `--start-date` / `--end-date AAAA-MM-DD` | `start_date` / `end_date` | validadas localmente (início ≤ fim) |
| `--exact` | `exact_match: true` | só resultados com a(s) frase(s) entre aspas da consulta |
| `--topic finance` | `topic` | além de `general`/`news` |

Erros de filtro (domínio inválido, data impossível, listas acima do limite)
são detetados **antes** do pedido — nunca gastam créditos. Existem também
`/crawl`, `/map` e `/research` na API — não cobertos por esta skill (o modo
pesquisa profunda é orquestrado pelo agente, com controlo de qualidade e
escudo próprios).

### Endpoint de extração (`extract`)

`POST https://api.tavily.com/extract` — texto integral de 1..20 URLs, com a
**mesma máquina de rotação** (bans, round-robin, concorrência, keyless):

```json
{
  "urls": ["https://arxiv.org/pdf/2402.14207"],
  "extract_depth": "basic",
  "format": "markdown",
  "include_images": false,
  "include_favicon": false,
  "include_usage": true,
  "timeout": 60.0,
  "query": "opcional: ordena os trechos",
  "chunks_per_source": 3
}
```

- `query` (+ `chunks_per_source` 1..5) faz a API devolver só os trechos mais
  relevantes de cada página, unidos por `[...]` — ideal para confirmar uma
  citação literal sem trazer o artigo inteiro.
- `timeout` do servidor = `--timeout` do script − 5 s (entre 1 e 60): uma
  página lenta falha **sozinha** (`failed_results`) em vez de o pedido inteiro
  morrer por timeout do lado de cá — o que baniria a chave. Por isso o
  `--timeout` predefinido do `extract` é 75 s (o servidor usa até 30 s em
  `advanced`).
- Custo: `usage.credits` quando vem na resposta; senão 1 crédito (basic) ou 2
  (advanced) por cada 5 URLs extraídas com sucesso. URLs falhados não custam.
- Orçamento **justo** entre fontes (max-min): as curtas ficam inteiras e as
  longas repartem as sobras — um PDF enorme não apaga os restantes.
- Saída de texto em **envelope com nonce** aleatório por invocação
  (`⟪FONTE n · nonce …⟫ … ⟪/FONTE n · nonce …⟫`) — ver `escudo-injecao.md`.

### Endpoint de consumo (`status --check`)

`GET https://api.tavily.com/usage` — devolve o consumo real por chave e por
conta (`key.usage`, `account.current_plan`, `account.plan_usage`,
`account.plan_limit`, e o teto próprio da chave em `key.limit`) **sem gastar
créditos de pesquisa**. É o que alimenta o registo interno (`usados/restantes`
por conta): `remaining` é o menor saldo conhecido entre o plano
(`plan_limit − plan_usage`) e o teto da chave (`key.limit − key.usage`);
`plan_limit` nulo e sem teto de chave → saldo desconhecido.

Quem consulta `/usage`:

- `status --check` — sempre, em todas as contas (validação ao vivo);
- `status` sem `--check` — **sozinho**, só nas contas cujo saldo registado tem
  mais de 60 min (ou nunca foi visto), com timeout curto (5 s) e no máximo
  **1 consulta por conta a cada 6 min** (conta a última tentativa, com sucesso
  ou falha — um 429 também conta), nunca em contas REVOKED. A consulta é
  **reservada atomicamente no registo** (sob `flock`) antes do pedido, por isso
  N `status` em paralelo fazem 1 consulta, não N; sem registo persistente não há
  refrescamento automático (o `status` avisa). Pára à primeira falha de rede
  (não soma timeouts) e diz quais contas refrescou e quais falharam. Cada
  consulta automática tem um prazo **total** de ~7,7 s (DNS incluído). Um 2xx
  sem dados de consumo (ex.: proxy ou portal cativo) não conta como sucesso:
  não apaga o saldo conhecido nem valida a conta (nem no `--check`).
  `--no-refresh` desliga;
- `selftest --live` — 1 consulta por conta.

## Limites da API (medidos em 2026-09)

| Limite | Chave dev | Chave produção |
| --- | --- | --- |
| Endpoints normais (`/search`, `/extract`, …) | 100 RPM | 1.000 RPM |
| `/crawl` | 100 RPM | 100 RPM |
| Criação de tarefas `/research` | 20 RPM | 20 RPM |
| `/usage` | 10 req / 10 min | 10 req / 10 min |

- Exceder qualquer limite → **HTTP 429 com `retry-after`** (o script ignora-o:
  o ban de falha é fixo, ver abaixo).
- Cotas de créditos são **por conta** (plano Researcher: 1.000/mês, repostas no
  1.º dia do mês); a API não documenta um teto de requests *simultâneas* — o
  teto de concorrência do script é um controlo próprio (ver abaixo).

## Cabeçalhos

| Cabeçalho | Quando | Função |
| --- | --- | --- |
| `Authorization: Bearer <key>` | sempre que há credencial | autenticação |
| `X-Tavily-Access-Mode: keyless` | contingência (pool impedido) | invocação sem chave, limites mais severos |

## Classificação de erros e comportamento do script

| HTTP | Significado | O que o script faz | O que o agente vê |
| --- | --- | --- | --- |
| 200 | sucesso | normaliza, redige segredos, aplica orçamento | o resultado |
| 400 | consulta malformada | **terminal** — erro do pedido, não ban, não gasta mais chaves | `Erro:` + `Solução:` (simplificar consulta) |
| 401 | chave inválida/revogada | **ban 24 h** (`REVOKED`), rotaciona já | nada (transparente) |
| 403 | recurso interdito | **terminal** — erro do pedido, não ban | `Erro:` + `Solução:` |
| 429 | rate limit | **ban 24 h** (`RATE_LIMITED`), rotaciona | nada |
| 432 | cota mensal esgotada | **ban 24 h** (`QUOTA_EXHAUSTED`), rotaciona | nada |
| 433 | teto PAYGO | igual a 432 | nada |
| 5xx | instabilidade | **ban 24 h** (`SUSPENDED`), rotaciona | nada |
| rede/timeout | socket morto ou resposta HTTP malformada | **ban 24 h** (`SUSPENDED`), rotaciona | nada |

Regra central (v0.4.0): **qualquer falha da credencial ban-a por 24 h** —
durante esse prazo a chave não é selecionável em processo nenhum. O `Retry-After`
do servidor não encurta nem prolonga o ban (é ignorado); o prazo é
`--ban-hours` / `TAVILY_BAN_HOURS` (predef. 24 h). Erros do **pedido**
(400/403) não são culpa da chave e não banem.

## Algoritmo de rotação (o ciclo transparente)

```
arranque:
    aplicar o registo persistente ao pool      # bans/contadores de antes
    cursor = depois da última chave usada      # nunca recomeça sempre em A

para cada passagem (máx. 6):
    repetir:
        escolher a próxima conta ACTIVE em ROUND-ROBIN ESTRICTO
                                               # sempre a partir do cursor: a chave da
                                               # chamada anterior nunca se repete enquanto
                                               # houver alternativas; revive bans expirados;
                                               # salta as que já falharam NESTA passagem,
                                               # as ocupadas e as desativadas (keys disable)
        conta no teto de concorrência → marcá-la ocupada e escolher outra
        nenhuma conta disponível:
            só há contas vivas ocupadas → esperar ~0,5 s e repetir
                                          (até --max-wait; esgotado → keyless)
            senão → contingência keyless (1× por passagem) ou fim da passagem
        enviar a MESMA request                 # reserva 1 slot in-flight na conta
        sucesso → somar usage.credits à conta e devolver o resultado
        401 → ban 24 h (REVOKED)               # tudo isto é invisível para quem chama
        429 → ban 24 h (RATE_LIMITED)
        432/433 → ban 24 h (QUOTA_EXHAUSTED)
        5xx/rede/timeout → ban 24 h (SUSPENDED)
        400/403/outro → erro instrutivo (terminal)
        sempre → libertar o PRÓPRIO slot e registar só o que foi observado
    fim da passagem:
        esperar o BAN mais curto SE couber em --max-wait
        (um ban de 24 h nunca cabe → desiste logo com erro instrutivo),
        ou ~0,5 s se ainda houver conta viva, e repetir a MESMA request
senão: erro instrutivo (tentativas, pool, ban restante, o que fazer)
```

Limites garantidos por invocação (o que impede rajadas e esperas infinitas):

- cada conta: no máx. **1 pedido por passagem** — a falha bane-a e ela não
  volta a ser tentada nesta invocação;
- keyless: no máx. 1 pedido por passagem;
- todas as esperas (concorrência, ban curto) somadas: até `--max-wait`
  (mais o jitter da última) — bans de 24 h nunca são esperados;
- no máx. 6 passagens.

## Round-robin estrito (a ordem da rotação)

```
escolhida = a primeira conta ACTIVE a partir do cursor global,
            fora das desativadas (keys disable), das banidas e das já
            falhadas nesta passagem
cursor    = avança a cada escolha e persiste entre invocações
```

- **A chave da chamada anterior nunca se repete** enquanto houver alternativas:
  chamadas seguidas com [A, B] fazem A→B→A→B…
- O saldo estimado (`usage.remaining − créditos gastos desde então`) **não
  decide a ordem** — aparece no `status`/`keys list` como informação. (Uma
  conta com menos saldo não é descartada: é simplesmente a próxima da vez.)
- Com cursor persistido, uma invocação que falhou em A e serviu em B continua
  na seguinte **depois de B** — nunca recomeça em A.

## Ban de 24 h (o que "fora de rotação" quer dizer)

- Uma falha marca `cooldown_until = agora + ban_s` (predef. 24 h) no registo
  do pool — **global**: qualquer processo salta essa chave até o prazo passar.
- Passado o prazo, a chave volta a `ACTIVE` automaticamente (lazy, na próxima
  escolha). Um 401 volta também — se continuar morta, falha de novo e entra em
  novo ban.
- **`keys unban <seletor|--all>`** readmite imediatamente (controlo global);
  `status --reset-state` limpa todos os bans.
- `keys disable` é diferente: tira a chave da rotação **sem prazo** (só
  `keys enable` devolve); o ban expira sozinho.
- Entradas `REVOKED` **sem** `cooldown_until` (escritas pela v0.3.x) são
  tratadas como permanentes por compatibilidade — `keys unban` ou
  `--reset-state` libertam-nas.

## Registo global de chaves (`keys.json`)

Localização: `$TAVILY_KEYS_FILE` ou `<state-dir>/keys.json` (predef.
`~/.local/state/tavily-agent-skill/keys.json`). Ficheiro `0600`, diretoria
`0700` — **único ficheiro que contém material de chave** (por isso nunca sai em
claro: toda a saída do script redige valores).

```json
{
  "version": 1,
  "keys": [
    {"key": "tvly-...", "label": "conta-A", "added_at": 1790422235.4}
  ],
  "disabled": ["<sha256(key)[:16]>"]
}
```

- Ordem do array = ordem de rotação inicial (as chaves do terminal vêm a
  seguir, deduplicadas por valor — o mesmo valor nos dois sítios conta uma vez
  e o nome do registo é o canónico).
- `disabled` lista por hash as chaves fora de rotação (de qualquer origem).
- Um `keys.json` corrompido é tratado como vazio (nunca parte uma pesquisa).

## Registo persistente do pool (`pool-state.json`)

Localização: `$TAVILY_STATE_DIR` ou `~/.local/state/tavily-agent-skill/`
(ficheiro `0600`, diretoria `0700`; `--no-state` desativa). Conteúdo:

```json
{
  "version": 1,
  "cursor": "<sha256(key)[:16] da última chave usada>",
  "keys": {
    "<sha256(key)[:16]>": {
      "ref": "\u2026ilaz", "status": "ACTIVE",
      "cooldown_until": 0, "failures": 0,
      "total_requests": 12, "total_failures": 1, "credits_spent": 11,
      "last_error": "HTTP 429", "last_used_at": 1790422235.4,
      "usage": {"plan": "Researcher", "used": 782, "limit": 1000, "remaining": 218,
                "key_used": 782, "key_limit": null,
                "checked_at": 1790420000.0, "spent_at_check": 4},
      "usage_queried_at": 1790420000.0
    }
  },
  "inflight": {"<hash>": [1790422355.4]}
}
```

(O ficheiro é escrito em ASCII puro — `…` aparece como `\u2026`. Cada slot
in-flight guarda a sua **expiração**, não o início.)

- **Nunca contém material de chave** — cada credencial é identificada por
  `sha256(key)[:16]`; a `ref` visível são os últimos 4 caracteres.
- Escrita atómica (tmp + `os.replace`) sob `flock` — seguro com vários
  processos a pesquisar em paralelo. O merge cross-processo **não perde
  observações**: contadores (requests, falhas, créditos) acumulam por delta, o
  estado vence por severidade (`REVOKED` é absorvente — uma vista antiga não
  ressuscita uma chave banida) e o consumo (`usage`) mais recente vence por
  `checked_at`. Só `status --check` impõe estado com autoridade, e apenas às
  chaves que acabou de verificar.
- **Cada processo só regista o que observou** (estado, ban e erros das contas
  que usou ou verificou). Uma pesquisa que carregou o registo antes de um
  `status --reset-state` ou `--check` nunca o desfaz ao terminar; apagar o
  ficheiro limpa-o de facto.
- `spent_at_check` é amostrado do registo **imediatamente antes** do
  `GET /usage` — créditos gastos por outros processos durante o próprio pedido
  contam a dobrar (erro conservador: o saldo estimado fica por baixo).
- Um registo corrompido (JSON inválido, aninhamento absurdo, tipos
  inesperados, NaN/Infinity, inteiros gigantes) é reduzido aos campos
  conhecidos ou tratado como ausente — nunca parte uma pesquisa.
- É isto que impede "tentar sempre em ordem e sempre do início as keys que não
  funcionam": bans persistem e valem para todos os processos, e o cursor retoma
  o round-robin onde parou.
- `status --reset-state` limpa o registo; `status`/`keys list` mostram o que lá
  está.

## Concorrência por conta (controlo próprio)

- Teto de requests **simultâneas** por conta: predefinição 2
  (`--max-inflight-per-key` / `TAVILY_MAX_INFLIGHT_PER_KEY`; `0` = sem teto).
- Slots in-flight são registados cross-processo e guardam a sua expiração:
  `início + max(120 s, 2 × --timeout + 10 s)` — um pedido longo não perde o slot
  a meio e um processo morto só o prende até expirar (expirações
  absurdamente no futuro são descartadas). Uma conta no teto é **saltada**,
  não contada como falha. Cada processo liberta apenas o seu próprio slot;
  com teto `0` nenhum slot é reservado (nem libertado).
- Todas as contas ocupadas → espera (~0,5 s + jitter) e **retry da MESMA
  request**, até `--max-wait`; esgotada a espera, contingência keyless.

## Recuperação de chaves

- **Regra geral**: qualquer ban expira ao fim do prazo (predef. 24 h) e a chave
  volta à rotação sozinha — dentro da própria invocação (se o prazo for curto
  e couber em `--max-wait`) ou nas seguintes.
- **432/433**: a Tavily repõe as cotas no primeiro instante do primeiro dia do
  mês civil (UTC); com o ban de 24 h, uma chave sem cota é re-tentada no dia
  seguinte — se a cota ainda não repôs, falha e entra em novo ban (sem custo:
  pedidos que falham não gastam créditos).
- **401**: ban de 24 h; se a chave continuar morta, cada re-tentativa diária
  falha de novo (1 pedido desperdiçado por dia, sem custo). Para a tirar de
  vez: `keys disable` ou `keys remove`.
- **Atalhos**: `keys unban --all` readmite tudo já; `status --reset-state`
  recomeça o registo do zero.

## Escudo anti-injeção (todo o texto vindo da web)

Aplicado a `answer`, `title`, `content` (e à URL/data, só higienização) de
`search` e `extract`, **antes** do orçamento — a deteção vê o texto completo:

1. **Higieniza**: remove *tags* Unicode (U+E0000–E007F, «ASCII smuggling»),
   seletores de variação em série (U+FE00–FE0F e U+E0100–E01EF, «emoji
   smuggling»; um seletor isolado, como em ❤️, fica), controlos bidi
   (U+202A–202E, U+2066–2069), invisíveis (zero-width, soft hyphen, BOM) e
   controlos C0/C1 (ESC/ANSI, backspace, NUL). O texto escondido em *tags* ou
   em seletores é descodificado **só para análise**.
2. **Neutraliza**: marcadores de papel/modelo (`<|im_start|>`, `[INST]`,
   `<<SYS>>`, `<system>`, `<tool_call>`, `<function_calls>`…) viram `⟦…⟧`; os
   caracteres do envelope (`⟪` `⟫`) viram `«` `»`.
3. **Deteta** (EN/PT/ES) → `shield: {"risk": "medio|alto", "flags": [...]}` na
   fonte e `meta.shield` no total. Sinais: `ignorar-instrucoes` (alto),
   `exfiltracao` (alto), `texto-oculto-com-instrucoes` (alto),
   `redefinir-papel`, `dirigido-a-ia`, `ocultar-do-utilizador`,
   `marcador-de-papel`, `unicode-oculto` (médios). Três sinais distintos, ou
   «dirigido-a-ia» + um comportamento pedido, sobem a risco alto.
4. **Isola** (`--quarantine`): fontes de risco alto ficam só com
   título/URL/sinais; uma `answer` sintetizada com risco alto é descartada.

Os padrões foram calibrados contra texto técnico legítimo (documentação de
APIs com «include your API key», CSS «override the rules», «you are now a
member»…) e o selftest prova ambos os lados. Continua a ser uma heurística —
a outra metade da defesa é o protocolo dos agentes (`escudo-injecao.md`).

## Orçamento de contexto

A saída é truncada a 50 KB por invocação (`--max-bytes`), com corte em limites
de ponto de código UTF-8 (nunca parte caracteres). `meta.truncated: true` no
JSON indica que houve truncagem. No `search` o orçamento é sequencial
(resposta, depois fontes por ordem); no `extract` é repartido de forma justa
entre fontes.
