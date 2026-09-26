# Contrato da API Tavily e a máquina de rotação (Nível 3)

Carregue este ficheiro só quando precisar de interpretar `--verbose`, afinar
`--timeout`/`--max-wait`, ou perceber exatamente o que o script faz quando uma
request morre.

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
`include_raw_content` nunca é enviado (explosão de contexto). Existem também
`/extract`, `/crawl`, `/map` e `/research` na API — não cobertos por esta skill.

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

- Exceder qualquer limite → **HTTP 429 com `retry-after`**.
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
| 400 | consulta malformada | **terminal** — não gasta mais chaves | `Erro:` + `Solução:` (simplificar consulta) |
| 401 | chave inválida/revogada | marca `REVOKED`, rotaciona já | nada (transparente) |
| 403 | recurso interdito | **terminal** | `Erro:` + `Solução:` |
| 429 | rate limit | marca cooldown (`Retry-After` em segundos ou data HTTP, máx. 1 h; sem ele, fórmula), rotaciona | nada |
| 432 | cota mensal esgotada | suspende até 1.º dia do mês seguinte (UTC), rotaciona | nada |
| 433 | teto PAYGO | igual a 432 | nada |
| 5xx | instabilidade | rotaciona (transiente) | nada |
| rede/timeout | socket morto ou resposta HTTP malformada | rotaciona (transiente) | nada |

## Algoritmo de rotação (o ciclo transparente)

```
arranque:
    aplicar o registo persistente ao pool      # óbitos/cooldowns de antes
    cursor = depois da última chave usada      # nunca recomeça sempre em A

para cada passagem (máx. 6):
    repetir:
        escolher a próxima conta ACTIVE        # orientada ao saldo (ver abaixo) + round-robin;
                                               # revive cooldowns expirados; salta as que já
                                               # falharam NESTA passagem, as ocupadas e as que
                                               # esgotaram 2 falhas transitórias na invocação
        conta no teto de concorrência → marcá-la ocupada e escolher outra
        nenhuma conta disponível:
            só há contas vivas ocupadas → esperar ~0,5 s e repetir
                                          (até --max-wait; esgotado → keyless)
            há contas com UMA falha 5xx/rede → backoff ~0,5 s e 2.ª ronda para elas
            senão → contingência keyless (1× por passagem) ou fim da passagem
        enviar a MESMA request                 # reserva 1 slot in-flight na conta
        sucesso → somar usage.credits à conta e devolver o resultado
        401 → REVOKED                          # tudo isto é invisível para quem chama
        429 → cooldown: Retry-After (piso 0,5 s, teto 1 h) ou backoff exponencial
        432/433 → suspensão até ao dia 1 do mês seguinte (UTC)
        5xx/rede/timeout → +1 falha transitória (à 2.ª a conta sai desta invocação)
        400/403/outro → erro instrutivo (terminal)
        sempre → libertar o PRÓPRIO slot e registar só o que foi observado
    fim da passagem:
        esperar o cooldown mais curto das contas que VÃO recuperar
        (RATE_LIMITED/QUOTA_EXHAUSTED; REVOKED e contas fora da invocação não
        contam), ou ~0,5 s se ainda houver conta viva, e repetir a MESMA request
senão: erro instrutivo (tentativas, pool, se o keyless foi tentado, o que fazer)
```

Limites garantidos por invocação (o que impede rajadas e esperas infinitas):

- cada conta: no máx. 1 pedido por passagem, mais 1 repetição após uma falha
  transitória; 2 falhas transitórias (5xx/rede/timeout) tiram-na da invocação;
- keyless: no máx. 1 pedido por passagem e as mesmas 2 falhas transitórias;
- todas as esperas (concorrência, backoff, cooldown) somadas: até `--max-wait`
  (mais o jitter da última);
- no máx. 6 passagens.

Cooldown quando a API não manda `Retry-After`:

```
T = min(60 s, 0.5 s × 2^k) + δ        k = falhas consecutivas da chave (só um sucesso o zera)
                                      δ ∈ [0, 0.5 s) — jitter anti-ressaturação
```

## Rotação orientada ao saldo

Com mais do que uma conta ACTIVE, a escolha não é round-robin cego:

```
saldo(conta) = usage.remaining − (credits_spent − usage.spent_at_check)
               # último /usage menos os créditos gastos desde essa consulta

candidatas  = ACTIVE, fora do teto de concorrência e não falhadas nesta ronda
melhor      = maior saldo conhecido entre as candidatas
escolhida   = a primeira, a partir do cursor, com saldo == melhor (e > 0)
              OU com saldo desconhecido
se nenhuma  → a primeira a partir do cursor (a estimativa pode falhar: PAYGO)

saldo desconhecido = sem /usage, /usage de um mês UTC anterior (as cotas
                     repõem no dia 1) ou carimbo no futuro (relógio avariado)
```

- Contas com **saldo desconhecido ou igual** continuam em round-robin a partir
  do cursor persistido — sem nenhum saldo conhecido é exatamente o
  comportamento clássico (A→B→C) e a invocação seguinte não recomeça em A.
- Como cada pesquisa desconta créditos reais, duas contas com o mesmo saldo
  **alternam** (A→B→A…) e uma conta com 900 é drenada até igualar a de 218,
  em vez de se esgotar a de 218 primeiro.
- Uma conta com menos saldo não é descartada: continua a ser a alternativa
  quando as preferidas falham (429/5xx/rede/teto de concorrência).

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
  ressuscita uma chave morta) e o consumo (`usage`) mais recente vence por
  `checked_at`. Só `status --check` impõe estado com autoridade, e apenas às
  chaves que acabou de verificar.
- **Cada processo só regista o que observou** (estado, cooldown e erros das
  contas que usou ou verificou). Uma pesquisa que carregou o registo antes de
  um `status --reset-state` ou `--check` nunca o desfaz ao terminar; apagar o
  ficheiro limpa-o de facto.
- `spent_at_check` é amostrado do registo **imediatamente antes** do
  `GET /usage` — créditos gastos por outros processos durante o próprio pedido
  contam a dobrar (erro conservador: o saldo estimado fica por baixo).
- Um registo corrompido (JSON inválido, aninhamento absurdo, tipos
  inesperados, NaN/Infinity, inteiros gigantes) é reduzido aos campos
  conhecidos ou tratado como ausente — nunca parte uma pesquisa.
- É isto que impede "tentar sempre em ordem e sempre do início as keys que não
  funcionam": óbitos (401) persistem, cooldowns atravessam invocações e o
  cursor retoma o round-robin onde parou.
- `status --reset-state` limpa o registo; `status` mostra o que lá está.

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

- **429**: a chave volta a `ACTIVE` assim que o cooldown expira (dentro da
  própria invocação ou na seguinte).
- **432/433**: a Tavily repõe as cotas no primeiro instante do primeiro dia do
  mês civil (UTC) — a chave volta a ficar utilizável nessa altura.
- **401**: fica `REVOKED` no registo e **não é re-tentada** nas invocações
  seguintes; `status --reset-state` (ou uma chave nova no mesmo slot) a reconsidera.

## Orçamento de contexto

A saída é truncada a 50 KB por invocação (`--max-bytes`), com corte em limites
de ponto de código UTF-8 (nunca parte caracteres). `meta.truncated: true` no
JSON indica que houve truncagem.
