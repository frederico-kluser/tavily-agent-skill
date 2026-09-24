# Chaves Tavily no terminal (Nível 3)

O formato é **idêntico ao do plugin DSH** (`dsh-tavily-resilient-search`), para
poder alternar entre ambos sem mudar de hábito.

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
- Obter chaves: <https://app.tavily.com> (plano grátis: 1000 créditos/mês,
  repostos no 1.º dia do mês).

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
| `401` em `status --check` | chave copiada mal ou revogada | gerar nova em app.tavily.com |
| `429` repetido | plano grátis: 100 RPM | mais contas (A..D) — o script já rotaciona |
| `432` | cota mensal esgotada | espera pelo 1.º do mês ou declara outra conta |
| tudo falha com "pool: …" | todas suspensas | `--max-wait 60` e repetir, ou novas chaves |
| resultados vazios | consulta demasiado específica | simplificar; `--topic general`; `--depth basic` |

## Segurança

- O script **redige** qualquer material de chave na saída (canário por valor);
  mesmo que a API ecoe uma credencial, não passa para o contexto do agente.
- `status` mostra apenas nomes de variáveis e refs mascaradas (`…últimos4`).
- O diagnóstico `--verbose` também nunca imprime segredos.
- Rede: apenas `api.tavily.com` (egress mínimo).
