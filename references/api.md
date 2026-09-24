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
para cada passagem (máx. 6):
    repetir (nº de chaves + 1) vezes:        # +1 = contingência keyless
        escolher próxima credencial ACTIVE   # round-robin + reviver cooldowns
        enviar a MESMA request
        sucesso → devolver resultado
        401 → REVOKED e continuar           # tudo isto é invisível
        429 → cooldown e continuar
        432/433 → suspensão mensal e continuar
        5xx/rede/timeout → continuar
        400/403/outro → erro instrutivo (terminal)
    se pool inteiro impedido:
        esperar o cooldown mais curto (até --max-wait) e repetir a MESMA request
senão: erro instrutivo (quantas tentativas, o que fazer)
```

Cooldown quando a API não manda `Retry-After`:

```
T = min(60 s, 0.5 s × 2^k) + δ        k = falhas consecutivas da chave
                                      δ ∈ [0, 0.5 s) — jitter anti-ressaturação
```

## Recuperação de chaves

- **429**: a chave volta a `ACTIVE` assim que o cooldown expira (dentro da
  própria invocação ou na seguinte).
- **432/433**: a Tavily repõe as cotas no primeiro instante do primeiro dia do
  mês civil (UTC) — a chave volta a ficar utilizável nessa altura.
- **401**: permanece fora do pool durante a invocação (não há razão para
  insistir); na invocação seguinte é tentada outra vez (o estado não persiste
  em disco, por desenho).

## Orçamento de contexto

A saída é truncada a 50 KB por invocação (`--max-bytes`), com corte em limites
de ponto de código UTF-8 (nunca parte caracteres). `meta.truncated: true` no
JSON indica que houve truncagem.
