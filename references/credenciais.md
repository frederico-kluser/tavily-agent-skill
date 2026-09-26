# Chaves Tavily no terminal (Nível 3)

O pool usa o formato `TAVILY_API_KEY_A..D` — o padrão para agrupamentos de
contas com rotação automática.

## Declaração

```bash
# agrupamento de contas (recomendado — round-robin com recuperação automática)
export TAVILY_API_KEY_A="tvly-..."
export TAVILY_API_KEY_B="tvly-..."
export TAVILY_API_KEY_C="tvly-..."
export TAVILY_API_KEY_D="tvly-..."

# ou chave única
export TAVILY_API_KEY="tvly-..."
```

- O script aceita `TAVILY_API_KEY` e **qualquer** `TAVILY_API_KEY_<sufixo>`
  (`A`..`Z`, `_1`, `_minha`…), por ordem determinística — a ordem do round-robin
  é estável entre invocações.
- **Formato validado**: um valor com espaços, aspas (incluindo as tipográficas
  `"…"` de um documento colado), quebras de linha, caracteres fora de ASCII ou
  com menos de 8 caracteres é **ignorado** — nunca chega ao cabeçalho HTTP. O
  `status` mostra essa variável como `[FORMATO INVÁLIDO]` e cada pesquisa avisa
  em stderr (sem nunca mostrar o valor).
- **Deduplicação por valor**: variáveis com a mesma chave são uma só conta. Se
  `TAVILY_API_KEY` for um alias de compatibilidade de `TAVILY_API_KEY_A` (mesmo
  valor), o pool NÃO duplica e o `status` mostra o nome canónico
  `TAVILY_API_KEY_A` com a nota `alias: TAVILY_API_KEY`. Uma `TAVILY_API_KEY`
  com valor próprio é uma conta extra (a primeira do pool).
- Sem nenhuma chave, funciona em modo **keyless** (limites mais severos) e avisa
  em stderr.
- ⚠️ **Declare sempre o pool `A..D` quando quiser rotação.** Com só a chave
  única `TAVILY_API_KEY`, não há fallback: quando essa conta atinge o limite,
  não há segunda hipótese. Com 2+ contas, o script rotaciona sozinho.
- Obter chaves: <https://app.tavily.com> (plano grátis: 1000 créditos/mês,
  repostos no 1.º dia do mês).

## Variáveis de controlo (opcionais)

```bash
export TAVILY_STATE_DIR="/caminho/p/estado"        # registo do pool (predef.: ~/.local/state/tavily-agent-skill)
export TAVILY_MAX_INFLIGHT_PER_KEY="2"             # máx. de requests SIMULTÂNEAS por conta (0 = sem teto)
```

Prioridade do teto de concorrência: `--max-inflight-per-key` (CLI) >
`TAVILY_MAX_INFLIGHT_PER_KEY` > predefinição `2`. Um valor de ambiente inválido
é ignorado (fica a predefinição).

O registo (`pool-state.json`, 0600) guarda estado por chave, cursor de
round-robin, contadores e consumo — identificando cada credencial por hash
`sha256` (nunca o material da chave). É o que faz a invocação seguinte saltar
chaves mortas e **não recomeçar sempre do início**. Limpar: `status --reset-state`.

## Persistir no shell (cuidado)

```bash
# ~/.zshrc ou ~/.bashrc — só se a máquina for pessoal
export TAVILY_API_KEY_A="tvly-..."
```

Melhor em máquinas partilhadas: `direnv` (`.envrc` fora do git) ou um gestor de
segredos. **Nunca** commite chaves em repositórios; nunca as ponha em ficheiros
do projeto. As duas primeiras chaves `tvly-dev-…` usadas nos testes locais deste
repositório não estão em lado nenhum do código — só no teu terminal.

## Troubleshooting

| Sintoma | Causa | O que fazer |
| --- | --- | --- |
| `status` mostra 0 chaves | variáveis não exportadas nesta shell | `export …` ou reabre o terminal |
| `status` mostra `[FORMATO INVÁLIDO]` | valor colado com aspas, espaços, "Bearer ", quebra de linha ou caracteres não-ASCII | `export TAVILY_API_KEY_X="tvly-…"` só com a chave (sem aspas tipográficas) |
| `status` mostra `alias: TAVILY_API_KEY` | `TAVILY_API_KEY` tem o mesmo valor de uma `_A..` | normal — é a mesma conta, contada uma vez |
| `status` demora uns segundos | refrescou sozinho o saldo com > 60 min via `/usage` | normal; `status --no-refresh` para modo offline |
| `status` diz "sem registo persistente não há refrescamento automático" | registo indisponível (diretoria só de leitura/sandbox) | `status --check`, ou `TAVILY_STATE_DIR` gravável |
| saldo no `status` diferente do painel Tavily | estimativa = último `/usage` − créditos gastos desde então | `status --check` para a leitura exata |
| uma conta atinge o limite e as chamadas falham | chave única não tem rotação de fallback | declarar 2+ contas (`_A.._D`) — o script rotaciona sozinho |
| `401` em `status --check` | chave copiada mal ou revogada | gerar nova em app.tavily.com |
| `429` repetido | plano grátis: 100 RPM | mais contas (A..D) — o script já rotaciona |
| `429` com pesquisas paralelas | rajadas simultâneas na mesma conta | baixar o teto (`TAVILY_MAX_INFLIGHT_PER_KEY=1`) ou mais contas |
| chave morta continua a ser tentada ou o pool "recomeça sempre em A" | registo desativado (`--no-state`) ou limpo | deixar o registo ativo; `status` mostra cursor e contadores |
| `status --check` diz "rate limit do endpoint /usage" | `/usage` tem teto próprio (10 req/10 min) | esperar um minuto e repetir |
| validar as contas de ponta a ponta | — | `selftest --live` (1 pesquisa `ultra-fast` + 1 `/usage` por conta, ≈1 crédito cada; variáveis com formato inválido contam como FAIL) |
| estado do registo estranho/obsoleto | registo dessincronizado da realidade | `python3 scripts/tavily.py status --reset-state` |
| `432` | cota mensal esgotada | espera pelo 1.º do mês ou declara outra conta |
| tudo falha com "pool: …" | todas suspensas | `--max-wait 60` e repetir, ou novas chaves |
| resultados vazios | consulta demasiado específica | simplificar; `--topic general`; `--depth basic` |

## Segurança

- O script **redige** qualquer material de chave na saída (canário por valor);
  mesmo que a API ecoe uma credencial, não passa para o contexto do agente.
- `status` mostra apenas nomes de variáveis e refs mascaradas (`…últimos4`).
- O diagnóstico `--verbose`, as mensagens `Erro:`/`Solução:` e o `selftest --live`
  também passam pela redação — nunca imprimem segredos.
- Rede: apenas `api.tavily.com` (egress mínimo).
