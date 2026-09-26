# Chaves Tavily: registo global, terminal e bans (Nível 3)

O pool reúne duas origens (deduplicadas por valor, nesta ordem): as **chaves
cadastradas** no registo global (`keys.json`) e as variáveis do terminal
`TAVILY_API_KEY_A..Z` / `TAVILY_API_KEY`.

## Registo global (recomendado — controlo por `keys`)

Uma chave cadastrada vale para **qualquer agente/terminal** — declare uma vez:

```bash
python3 scripts/tavily.py keys add "tvly-..." --label conta-A
python3 scripts/tavily.py keys add "tvly-..." --label conta-B
python3 scripts/tavily.py keys                          # list: estado, bans, próxima da rotação
```

- **Migrar do terminal sem expor o valor** (evita histórico de shell):
  `python3 scripts/tavily.py keys add --from-env TAVILY_API_KEY_A --label conta-A`.
- **Remover**: `keys remove conta-A` (só chaves do registo; as do terminal
  removem-se com `unset`).
- **Desativar/ativar sem apagar**: `keys disable "#2"` / `keys enable conta-B`
  — a chave desativada fica **fora da rotação em todos os agentes**.
- **Readmitir bans**: `keys unban --all` (ou `keys unban conta-A`).
- **Quem é a próxima**: `keys next` (não gasta nada).
- Seletores em qualquer ação: `#índice` (do `keys list`), etiqueta/nome,
  `…últimos4` da chave ou prefixo do hash.
- Localização: `$TAVILY_KEYS_FILE` ou `<state-dir>/keys.json`
  (predef. `~/.local/state/tavily-agent-skill/keys.json`). Ficheiro **0600**,
  fora do repo — é o único ficheiro que guarda material de chave; o script
  redige-o sempre na saída. Um `keys.json` corrompido é ignorado como vazio.

## Declaração por terminal (pool adicional)

```bash
# agrupamento de contas (round-robin estrito com recuperação automática)
export TAVILY_API_KEY_A="tvly-..."
export TAVILY_API_KEY_B="tvly-..."
export TAVILY_API_KEY_C="tvly-..."
export TAVILY_API_KEY_D="tvly-..."

# ou chave única
export TAVILY_API_KEY="tvly-..."
```

- O script aceita `TAVILY_API_KEY` e **qualquer** `TAVILY_API_KEY_<sufixo>`
  (`A`..`Z`, `_1`, `_minha`…), por ordem determinística — a ordem do
  round-robin é estável entre invocações.
- **Formato validado**: um valor com espaços, aspas (incluindo as tipográficas
  `"…"` de um documento colado), quebras de linha, caracteres fora de ASCII ou
  com menos de 8 caracteres é **ignorado** — nunca chega ao cabeçalho HTTP. O
  `status` mostra essa variável como `[FORMATO INVÁLIDO]` e cada pesquisa avisa
  em stderr (sem nunca mostrar o valor). O mesmo formato é exigido a
  `keys add`.
- **Deduplicação por valor**: o mesmo valor no registo e no terminal (ou em
  duas variáveis) conta UMA vez — o nome do registo é o canónico e os restantes
  viram notas `alias:`.
- Sem nenhuma chave, funciona em modo **keyless** (limites mais severos) e avisa
  em stderr.
- ⚠️ **Precisa de rotação?** Com só a chave única `TAVILY_API_KEY`, não há
  fallback: quando essa conta é banida, não há segunda hipótese. Com 2+ contas
  (no registo ou no terminal), o script rotaciona sozinho.
- Obter chaves: <https://app.tavily.com> (plano grátis: 1000 créditos/mês,
  repostos no 1.º dia do mês).

## Variáveis de controlo (opcionais)

```bash
export TAVILY_STATE_DIR="/caminho/p/estado"        # registos (predef.: ~/.local/state/tavily-agent-skill)
export TAVILY_KEYS_FILE="/caminho/p/keys.json"     # registo global de chaves (predef.: <state-dir>/keys.json)
export TAVILY_BAN_HOURS="24"                       # prazo do ban por falha, em horas (predef. 24)
export TAVILY_MAX_INFLIGHT_PER_KEY="2"             # máx. de requests SIMULTÂNEAS por conta (0 = sem teto)
```

Prioridades: teto de concorrência `--max-inflight-per-key` > `TAVILY_MAX_INFLIGHT_PER_KEY` > `2`;
ban `--ban-hours` > `TAVILY_BAN_HOURS` > `24 h`. Valores de ambiente inválidos
são ignorados (fica a predefinição).

O registo do pool (`pool-state.json`, 0600) guarda estado por hash de chave,
bans, cursor de round-robin, contadores e consumo — **nunca** o material da
chave. É o que faz a invocação seguinte saltar chaves banidas e **não
recomeçar sempre do início**. Limpar: `status --reset-state` (não toca no
`keys.json`).

## Bans (chave que falha fica 24 h de fora)

- Qualquer falha (429, 401, 432/433, 5xx, timeout, rede) **ban a chave por
  24 h**: não é selecionável em processo nenhum até o prazo passar. O
  `Retry-After` da API é ignorado — o prazo é sempre o ban.
- Passado o prazo, volta sozinha; para antecipar: `keys unban --all`.
- Ban ≠ desativado: o **ban expira sozinho**; a **desativação** (`keys disable`)
  só acaba com `keys enable`.
- Pedidos que falham não gastam créditos — uma re-tentativa diária de uma chave
  morta não custa nada.

## Persistir no shell (cuidado)

```bash
# ~/.zshrc ou ~/.bashrc — só se a máquina for pessoal
export TAVILY_API_KEY_A="tvly-..."
```

Melhor em máquinas partilhadas: cadastrar no registo global (`keys add`) ou um
gestor de segredos. **Nunca** commite chaves em repositórios; nunca as ponha em
ficheiros do projeto. As chaves `tvly-dev-…` usadas nos testes locais deste
repositório não estão em lado nenhum do código — só no teu terminal.

## Troubleshooting

| Sintoma | Causa | O que fazer |
| --- | --- | --- |
| `status`/`keys list` mostra 0 chaves | nada cadastrado nem exportado | `keys add "tvly-…" --label conta-A` ou `export …` |
| `status` mostra `[FORMATO INVÁLIDO]` | valor colado com aspas, espaços, "Bearer ", quebra de linha ou caracteres não-ASCII | `export TAVILY_API_KEY_X="tvly-…"` só com a chave (sem aspas tipográficas) |
| `status` mostra `alias: …` | o mesmo valor está no registo e no terminal | normal — é a mesma conta, contada uma vez |
| `keys list` mostra `fora=23h` | essa chave falhou e está **banida** | esperar o prazo ou `keys unban <seletor>` |
| `keys list` mostra `DESATIVADA` | `keys disable` aplicado | `keys enable <seletor>` para devolver à rotação |
| `status` demora uns segundos | refrescou sozinho o saldo com > 60 min via `/usage` | normal; `status --no-refresh` para modo offline |
| `status` diz "sem registo persistente não há refrescamento automático" | registo indisponível (diretoria só de leitura/sandbox) | `status --check`, ou `TAVILY_STATE_DIR` gravável |
| saldo no `status` diferente do painel Tavily | estimativa = último `/usage` − créditos gastos desde então | `status --check` para a leitura exata |
| uma conta é banida e as chamadas seguem | comportamento correto: ban 24 h + rotação | nada — a próxima chave serve; ver `keys list` |
| todas as chaves banidas ("todas as alternativas falharam") | pool pequeno + muitas falhas seguidas | `keys unban --all` (se recuperaram), `keys add` mais contas, ou esperar o ban |
| `401` em `status --check` | chave copiada mal ou revogada | gerar nova em app.tavily.com; `keys remove` a antiga |
| `429` repetido | plano grátis: 100 RPM | mais contas (`keys add`) — o script já rotaciona e bane a que falhou |
| `429` com pesquisas paralelas | rajadas simultâneas na mesma conta | baixar o teto (`TAVILY_MAX_INFLIGHT_PER_KEY=1`) ou mais contas |
| chave banida continua a ser tentada ou o pool "recomeça sempre em A" | registo do pool desativado (`--no-state`) ou limpo | deixar o registo ativo; `keys list` mostra bans e cursor |
| `status --check` diz "rate limit do endpoint /usage" | `/usage` tem teto próprio (10 req/10 min) | esperar um minuto e repetir |
| validar as contas de ponta a ponta | — | `selftest --live` (1 pesquisa `ultra-fast` + 1 `/usage` por conta, ≈1 crédito cada; variáveis com formato inválido contam como FAIL) |
| estado do registo estranho/obsoleto | registo dessincronizado da realidade | `python3 scripts/tavily.py status --reset-state` |
| `432` | cota mensal esgotada | espera pelo 1.º do mês ou `keys add` outra conta |
| resultados vazios | consulta demasiado específica | simplificar; `--topic general`; `--depth basic` |

## Segurança

- O script **redige** qualquer material de chave na saída (canário por valor);
  mesmo que a API ecoe uma credencial, não passa para o contexto do agente.
- `status`, `keys list` e `keys next` mostram apenas nomes/etiquetas e refs
  mascaradas (`…últimos4`).
- `keys add --from-env VAR` evita passar o valor pela linha de comandos
  (histórico/processos); o valor redige-se em qualquer erro.
- O `keys.json` é gravado atomicamente a **0600** em diretoria **0700**, fora
  do repo; o registo do pool nunca contém material de chave (só hashes).
- O diagnóstico `--verbose`, as mensagens `Erro:`/`Solução:` e o `selftest --live`
  também passam pela redação — nunca imprimem segredos.
- Rede: apenas `api.tavily.com` (egress mínimo).
