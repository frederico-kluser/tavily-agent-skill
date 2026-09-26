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
| uma conta atinge o limite e as chamadas falham | chave única não tem rotação de fallback | declarar 2+ contas (`_A.._D`) — o script rotaciona sozinho |
| `401` em `status --check` | chave copiada mal ou revogada | gerar nova em app.tavily.com |
| `429` repetido | plano grátis: 100 RPM | mais contas (A..D) — o script já rotaciona |
| `429` com pesquisas paralelas | rajadas simultâneas na mesma conta | baixar o teto (`TAVILY_MAX_INFLIGHT_PER_KEY=1`) ou mais contas |
| chave morta continua a ser tentada ou o pool "recomeça sempre em A" | registo desativado (`--no-state`) ou limpo | deixar o registo ativo; `status` mostra cursor e contadores |
| `status --check` diz "rate limit do endpoint /usage" | `/usage` tem teto próprio (10 req/10 min) | esperar um minuto e repetir |
| estado do registo estranho/obsoleto | registo dessincronizado da realidade | `python3 scripts/tavily.py status --reset-state` |
| `432` | cota mensal esgotada | espera pelo 1.º do mês ou declara outra conta |
| tudo falha com "pool: …" | todas suspensas | `--max-wait 60` e repetir, ou novas chaves |
| resultados vazios | consulta demasiado específica | simplificar; `--topic general`; `--depth basic` |

## Segurança

- O script **redige** qualquer material de chave na saída (canário por valor);
  mesmo que a API ecoe uma credencial, não passa para o contexto do agente.
- `status` mostra apenas nomes de variáveis e refs mascaradas (`…últimos4`).
- O diagnóstico `--verbose` também nunca imprime segredos.
- Rede: apenas `api.tavily.com` (egress mínimo).
