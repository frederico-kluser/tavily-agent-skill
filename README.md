# tavily-agent-skill

[![ci](https://github.com/frederico-kluser/tavily-agent-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/frederico-kluser/tavily-agent-skill/actions/workflows/ci.yml)

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
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py status               # estado do pool + saldo
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py selftest             # verificar instalação (offline)
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py selftest --live      # + prova real (≈1 crédito/conta)
```

## Chaves — pool com rotação automática

```bash
export TAVILY_API_KEY_A="tvly-..."   # tantas contas quantas quiser (A..D, ...)
export TAVILY_API_KEY_B="tvly-..."
# ou chave única: export TAVILY_API_KEY="tvly-..."
```

Sem chaves, opera em modo *keyless* (limites mais severos). Obter chaves: [app.tavily.com](https://app.tavily.com).

## O que torna esta skill diferente

- **Gestão de requests invisível**: 429/432/401/5xx/rede → marca a credencial, rotaciona e reemite a *mesma* request; se o pool todo estiver impedido, espera o cooldown mais curto e repete — sem rajadas (cada conta no máx. 1 pedido por passagem, 2 falhas transitórias por invocação) e com todas as esperas limitadas por `--max-wait`. Só há erro (instrutivo: `Erro:` + `Solução:`, exit 2) quando não há mesmo nenhuma alternativa — nunca um traceback.
- **Rotação orientada ao saldo**: entre contas vivas, serve primeiro a com mais créditos restantes (último `/usage` menos os créditos reais gastos desde então, via `include_usage`); saldos iguais ou desconhecidos seguem em round-robin.
- **Registo interno persistente**: estado por chave, cursor de round-robin, créditos gastos e consumo real (`/usage`) guardados em `~/.local/state/tavily-agent-skill` — a invocação seguinte não recomeça do início nem re-tenta chaves mortas. Sem material de chave no disco (só hashes, 0600).
- **Saldo sempre fresco**: `status` refresca sozinho o saldo com mais de 60 min, sem rajadas contra o limite de `/usage` (máx. 1 consulta por conta a cada 6 min).
- **Concorrência controlada por conta**: teto de requests simultâneas (predef. 2, cross-processo) — contas no teto são saltadas; se todas ocuparem, espera e repete antes do keyless.
- **Determinística**: `selftest` prova 96 cenários offline (Pass^k — mesma entrada, mesmo comportamento); corre na CI em Python 3.10, 3.12 e 3.14 a cada push.
- **Segura**: segredos redigidos por valor em toda a saída (incluindo `--json` e `--verbose`); conteúdo web tratado como dado não-confiável (anti injeção indireta); egress só para `api.tavily.com`.
- **Económica**: divulgação progressiva (SKILL.md enxuto; `references/` só quando preciso) e saída limitada a 50 KB por invocação; `status --check` valida sem gastar créditos.

## Estrutura

```
tavily-agent-skill/
├── SKILL.md                 # ponto de entrada (frontmatter + contrato do script)
├── README.md                # este ficheiro
├── CHANGELOG.md             # histórico de versões
├── LICENSE                  # MIT
├── .github/workflows/ci.yml # selftest + py_compile em cada push/PR
├── scripts/tavily.py        # search | status | selftest (Python, stdlib apenas)
└── references/
    ├── api.md               # contrato da API + algoritmo de rotação/cooldowns/saldo
    └── credenciais.md       # formato das chaves + troubleshooting
```

## Licença

[MIT](LICENSE)
