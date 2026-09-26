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
  "chunks_per_source": 3
}
```

`include_raw_content` nunca é enviado (explosão de contexto). Existem também
`/extract`, `/crawl`, `/map` e `/research` na API — não cobertos por esta skill.

### Endpoint de consumo (`status --check`)

`GET https://api.tavily.com/usage` — devolve o consumo real por chave e por
conta (`key.usage`, `account.current_plan`, `account.plan_usage`,
`account.plan_limit`) **sem gastar créditos de pesquisa**. É o que alimenta o
registo interno (`usados/restantes` por conta).

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
| 429 | rate limit | marca cooldown (`Retry-After` ou fórmula), rotaciona | nada |
| 432 | cota mensal esgotada | suspende até 1.º dia do mês seguinte (UTC), rotaciona | nada |
| 433 | teto PAYGO | igual a 432 | nada |
| 5xx | instabilidade | rotaciona (transiente) | nada |
| rede/timeout | socket morto | rotaciona (transiente) | nada |

## Algoritmo de rotação (o ciclo transparente)

```
arranque:
    aplicar o registo persistente ao pool      # óbitos/cooldowns de antes
    cursor = depois da última chave usada      # nunca recomeça sempre em A

para cada passagem (máx. 6):
    repetir até esgotar a passagem:
        escolher próxima credencial ACTIVE     # round-robin + reviver cooldowns,
                                               # salta chaves no teto de concorrência
        se a conta está no teto de concorrência: saltar para a próxima
        se TODAS as contas vivas estão ocupadas:
            esperar (até --max-wait) e repetir  # RETRY — não falha já
            sem espera disponível → contingência keyless
        enviar a MESMA request                 # +1 slot in-flight por conta
        sucesso → devolver resultado
        401 → REVOKED e continuar              # tudo isto é invisível
        429 → cooldown e continuar
        432/433 → suspensão mensal e continuar
        5xx/rede/timeout → continuar
        400/403/outro → erro instrutivo (terminal)
        sempre → libertar slot e atualizar o registo
    se pool inteiro impedido:
        esperar o cooldown mais curto (até --max-wait) e repetir a MESMA request
senão: erro instrutivo (quantas tentativas, o que fazer)
```

Cooldown quando a API não manda `Retry-After`:

```
T = min(60 s, 0.5 s × 2^k) + δ        k = falhas consecutivas da chave
                                      δ ∈ [0, 0.5 s) — jitter anti-ressaturação
```

## Registo persistente do pool (`pool-state.json`)

Localização: `$TAVILY_STATE_DIR` ou `~/.local/state/tavily-agent-skill/`
(ficheiro `0600`, diretoria `0700`; `--no-state` desativa). Conteúdo:

```json
{
  "version": 1,
  "cursor": "<sha256(key)[:16] da última chave usada>",
  "keys": {
    "<sha256(key)[:16]>": {
      "ref": "…ilaz", "status": "ACTIVE",
      "cooldown_until": 0, "failures": 0,
      "total_requests": 12, "total_failures": 1,
      "last_error": "HTTP 429", "last_used_at": 1790422235.4,
      "usage": {"plan": "Researcher", "used": 782, "limit": 1000, "remaining": 218}
    }
  },
  "inflight": {"<hash>": [1790422235.4]}
}
```

- **Nunca contém material de chave** — cada credencial é identificada por
  `sha256(key)[:16]`; a `ref` visível são os últimos 4 caracteres.
- Escrita atómica (tmp + `os.replace`) sob `flock` — seguro com vários
  processos a pesquisar em paralelo. O merge cross-processo **não perde
  observações**: contadores acumulam por delta e o estado vence por severidade
  (`REVOKED` é absorvente — uma vista antiga não ressuscita uma chave morta).
- É isto que impede "tentar sempre em ordem e sempre do início as keys que não
  funcionam": óbitos (401) persistem, cooldowns atravessam invocações e o
  cursor retoma o round-robin onde parou.
- `status --reset-state` limpa o registo; `status` mostra o que lá está.

## Concorrência por conta (controlo próprio)

- Teto de requests **simultâneas** por conta: predefinição 2
  (`--max-inflight-per-key` / `TAVILY_MAX_INFLIGHT_PER_KEY`; `0` = sem teto).
- Slots in-flight são registados cross-processo (com TTL de 120 s contra
  processos mortos); uma conta no teto é **saltada**, não contada como falha.
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
