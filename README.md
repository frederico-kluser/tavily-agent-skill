# tavily-agent-skill

> Pesquisa web para agentes de LLM via **API Tavily** com **rotação automática de chaves** — se uma request morrer (429 rate limit, 432 cota, 401 chave morta, timeout, 5xx, rede), o script puxa **outra chave e refaz a mesma request**. O agente que chama nunca vê o erro nem precisa de gerir nada.

Agent Skill (spec aberta) que funciona em **qualquer agente/terminal**, dentro ou fora do DeepSeek Harness — sem plugin, sem dependências (Python stdlib).

## Instalar (global, via symlink)

```bash
git clone https://github.com/frederico-kluser/tavily-agent-skill.git
ln -s "$(pwd)/tavily-agent-skill" ~/.agents/skills/tavily-agent-skill
```

## Usar

```bash
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py search "o que diz a web sobre X"
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py search "..." --json   # para citar
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py status               # estado do pool
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py selftest             # verificar instalação
```

## Chaves — mesmo formato do plugin DSH

```bash
export TAVILY_API_KEY_A="tvly-..."   # tantas contas quantas quiser (A..D, ...)
export TAVILY_API_KEY_B="tvly-..."
# ou chave única: export TAVILY_API_KEY="tvly-..."
```

Sem chaves, opera em modo *keyless* (limites mais severos). Obter chaves: [app.tavily.com](https://app.tavily.com).

## O que torna esta skill diferente

- **Gestão de requests invisível**: 429/432/401/5xx/rede → marca a credencial, rotaciona e reemite a *mesma* request; se o pool todo estiver impedido, espera o cooldown mais curto e repete. Só há erro (instrutivo: `Erro:` + `Solução:`, exit 2) quando não há mesmo nenhuma alternativa.
- **Determinística**: `selftest` prova 11 cenários da máquina de rotação offline (Pass^k — mesma entrada, mesmo comportamento).
- **Segura**: segredos redigidos por valor na saída; conteúdo web tratado como dado não-confiável (anti injeção indireta); egress só para `api.tavily.com`.
- **Económica**: divulgação progressiva (SKILL.md enxuto; `references/` só quando preciso) e saída limitada a 50 KB por invocação.

## Estrutura

```
tavily-agent-skill/
├── SKILL.md                 # ponto de entrada (frontmatter + contrato do script)
├── scripts/tavily.py        # search | status | selftest (Python, stdlib apenas)
└── references/
    ├── api.md               # contrato da API + algoritmo de rotação/cooldowns
    └── credenciais.md       # formato das chaves + troubleshooting
```

## Licença

[MIT](LICENSE)
