# tavily-agent-skill

[![ci](https://github.com/frederico-kluser/tavily-agent-skill/actions/workflows/ci.yml/badge.svg)](https://github.com/frederico-kluser/tavily-agent-skill/actions/workflows/ci.yml)

> Pesquisa web para agentes de LLM via **API Tavily** com **rotação automática de chaves** — **round-robin estrito** (cada chamada troca de chave) e **ban de 24 h** para qualquer chave que falhe (429 rate limit, 432 cota, 401 chave morta, timeout, 5xx, rede): a chave falhada fica de fora e não é selecionável; o script puxa **outra chave e refaz a mesma request**. O agente que chama nunca vê o erro nem precisa de gerir nada. O controlo global é o comando **`keys`**.

Por cima disso, um **modo pesquisa profunda** focado só em qualidade: decomposição em sub-perguntas, subagentes em paralelo, rondas até a auditoria de lacunas fechar, fontes académicas, verificação adversarial e um **dossiê Markdown com FAQ em árvore** — com **escudo nativo contra injeção de prompts** em todo o texto que vem da web.

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
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py status               # estado do pool + saldo + próxima da rotação
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py keys                 # controlo global de chaves/bans
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py selftest             # verificar instalação (offline)
python3 ~/.agents/skills/tavily-agent-skill/scripts/tavily.py selftest --live      # + prova real (≈1 crédito/conta)
```

## Pesquisa profunda

```bash
T=~/.agents/skills/tavily-agent-skill/scripts/tavily.py
python3 $T research init "a pergunta principal"                                # dossiê com modelo de FAQ
python3 $T search "retrieval augmented generation survey" --preset academico --depth advanced --json
python3 $T search '"frase exata a confirmar"' --exact --start-date 2024-01-01   # filtros de data/frase
python3 $T extract https://arxiv.org/pdf/2402.14207 --query "o que procuro"     # texto integral (envelope com nonce)
python3 $T shield retorno-do-subagente.json                                     # escudo sobre qualquer texto
python3 $T research lint pesquisas/<dossie>.md                                  # erros + veredito CONTINUAR/PRONTO
```

O agente segue o protocolo de [`references/pesquisa-profunda.md`](references/pesquisa-profunda.md): brief → decomposição (perspetivas × facetas) → 1 subagente por pergunta, em paralelo → integração com níveis de fonte A–D e confiança tipo GRADE → auditoria de lacunas (checklist + crítico + `lint`) → nova ronda ou verificação adversarial (3 votos por afirmação central) → síntese por um único redator. Onde pesquisar: [`references/fontes-de-pesquisa.md`](references/fontes-de-pesquisa.md). Segurança: [`references/escudo-injecao.md`](references/escudo-injecao.md).

## Chaves — controlo global (`keys`)

```bash
python3 scripts/tavily.py keys add "tvly-..." --label conta-A   # cadastra UMA vez (vale para todos os agentes)
python3 scripts/tavily.py keys add --from-env TAVILY_API_KEY_A  # migrar do terminal sem expor o valor
python3 scripts/tavily.py keys                                  # list: estado, bans, próxima da rotação
python3 scripts/tavily.py keys disable "#2"                     # tira da rotação (global)
python3 scripts/tavily.py keys unban --all                      # readmite chaves banidas
```

Alternativa por terminal (entra também na rotação, deduplicado por valor):

```bash
export TAVILY_API_KEY_A="tvly-..."   # tantas contas quantas quiser (A..Z, ...)
export TAVILY_API_KEY_B="tvly-..."
# ou chave única: export TAVILY_API_KEY="tvly-..."
```

Sem chaves, opera em modo *keyless* (limites mais severos). Obter chaves: [app.tavily.com](https://app.tavily.com).

## O que torna esta skill diferente

- **Gestão de requests invisível**: 429/432/401/5xx/rede → a conta que falhou é **banida 24 h**, o script rotaciona e reemite a *mesma* request; se o pool todo estiver impedido, espera o ban mais curto que caiba em `--max-wait` e repete — sem rajadas (cada conta falha UMA vez por request) e com todas as esperas limitadas. Só há erro (instrutivo: `Erro:` + `Solução:`, exit 2) quando não há mesmo nenhuma alternativa — nunca um traceback.
- **Round-robin estrito**: cada chamada usa a **próxima** chave da vez (cursor global persistente); a chave da chamada anterior nunca se repete enquanto houver alternativas. O saldo estimado é apresentado no `status` mas **não** decide a ordem.
- **Ban de 24 h por falha**: uma chave que falhe fica **não selecionável** por 24 h (`--ban-hours` / `TAVILY_BAN_HOURS`), volta sozinha ao fim do prazo ou cedo com `keys unban`.
- **Controlo global (`keys`)**: chaves cadastradas num registo comum (`keys.json`, 0600, fora do repo) valem para qualquer agente/terminal — `add`/`remove`/`enable`/`disable`/`unban`/`next`. Env vars continuam a valer como pool adicional.
- **Registo interno persistente**: estado por hash (nunca material de chave), bans, cursor, créditos e consumo real (`/usage`) em `~/.local/state/tavily-agent-skill` — a invocação seguinte não recomeça do início nem re-tenta chaves banidas.
- **Saldo sempre fresco**: `status` refresca sozinho o saldo com mais de 60 min, sem rajadas contra o limite de `/usage` (máx. 1 consulta por conta a cada 6 min).
- **Concorrência controlada por conta**: teto de requests simultâneas (predef. 2, cross-processo) — contas no teto são saltadas; se todas ocuparem, espera e repete antes do keyless.
- **Pesquisa profunda nativa**: filtros de domínio/data/frase exata, presets académicos (`academico`, `saude`, `computacao`, `oficial`), `extract` de texto integral com orçamento justo entre fontes, e `research init|lint` — o `lint` apanha citações fantasma, fontes sem URL/DOI, nós órfãos, conclusão prematura e vetores de exfiltração, e diz se falta outra ronda.
- **Escudo anti-injeção**: remove texto invisível (tags Unicode, bidi, zero-width), neutraliza marcadores de papel (`<|im_start|>`, `[INST]`, `<tool_call>`…), deteta injeções EN/PT/ES (risco médio/alto por fonte), `--quarantine` retém fontes de risco alto e o `extract` usa envelopes com nonce inforjável. Calibrado sem falsos positivos em texto técnico comum.
- **Determinística**: `selftest` prova 120 cenários offline (Pass^k — mesma entrada, mesmo comportamento); corre na CI em Python 3.10, 3.12 e 3.14 a cada push.
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
├── scripts/tavily.py        # search | extract | research | shield | status | keys | selftest (stdlib)
└── references/
    ├── api.md               # contrato da API + rotação/bans/saldo + escudo
    ├── credenciais.md       # registo global de chaves + troubleshooting
    ├── pesquisa-profunda.md # protocolo do modo pesquisa profunda + modelo de FAQ
    ├── fontes-de-pesquisa.md# onde pesquisar: bases académicas, APIs, presets, snowballing
    ├── escudo-injecao.md    # proteção contra injeção de prompts (script + agentes)
    └── exemplo-dossie.md    # dossiê real e concluído (formato da FAQ preenchido)
```

## Licença

[MIT](LICENSE)
