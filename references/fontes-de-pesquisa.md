# Onde pesquisar — fontes académicas, oficiais e táticas (Nível 3)

Carregue este ficheiro na pesquisa profunda, quando precisar de literatura
científica, dados oficiais ou normas, ou de verificar se uma citação existe.

## 1. Estratégia em três camadas

| Camada | Para quê | Como |
| --- | --- | --- |
| **Descoberta** | encontrar o que existe | `tavily.py search` com `--preset` ou `--include-domains`, `--depth advanced`, `--json` |
| **Leitura** | ler a fonte primária e confirmar citações | `tavily.py extract URL --query "…"` (texto integral, envelope com nonce) |
| **Metadados e grafo de citações** | confirmar DOI, autores e ano, retratações; *snowballing* | APIs académicas abertas (§4), pela ferramenta HTTP do agente ou por `extract` |

As APIs académicas **não** passam pelo script: a rede do script vai só para
`api.tavily.com`. E **não se paralelizam**. Os limites delas contam **todas
as máquinas e processos** do utilizador juntos: o arXiv proíbe
expressamente contorná-los, e o pool anónimo da Semantic Scholar é global.
Por isso a pesquisa profunda concentra todas estas chamadas num único papel
em série, o **bibliotecário** (`pesquisa-profunda.md` §2). Os investigadores
paralelos usam só a Tavily, que tem controlo de concorrência próprio. Com a
fonte saturada, recorra a outra que se sobreponha (metadados do arXiv também
estão no OpenAlex, na Semantic Scholar e no Crossref) em vez de insistir.

Chame-as de uma de duas formas:

- **Ferramenta HTTP do próprio agente** (WebFetch, `curl`…). É o caminho
  preferido: grátis e com JSON completo. Respeite os limites de cada API
  (§4).
- **`tavily.py extract <URL-da-API> --format text`**, quando o agente não tem
  HTTP. Funciona com Crossref e OpenAlex (testado em 2026-09), mas o extrator
  **remove URLs e alguns *arrays* do JSON**: serve para confirmar existência,
  título e ano, não para metadados completos. A Semantic Scholar recusa este
  caminho («Failed to fetch url»). Custa 1 crédito por cada 5 URLs.

## 2. Presets de domínios do script

`--preset` restringe a pesquisa a listas curadas (repetível e combinável com
`--include-domains`). `--prefer-domains` **prioriza** esses domínios em vez de
restringir: útil quando a cobertura é incerta.

| Preset | Para | Domínios (resumo) |
| --- | --- | --- |
| `academico` | literatura científica geral | arXiv, Semantic Scholar, OpenAlex, CORE, DOAJ, PubMed/PMC, Europe PMC, SciELO, BDTD, RCAAP, HAL, Zenodo, OSF, Nature, Science, Cell, PNAS, Annual Reviews, Springer, Wiley, ScienceDirect, T&F, SAGE, Cambridge, OUP, JSTOR, PLOS, Frontiers, bioRxiv, medRxiv, SSRN, NBER, RePEc, IEEE Xplore, ACM DL, ACL Anthology, OpenReview, NeurIPS, PMLR, JMLR, BMJ, Lancet, NEJM, JAMA, Cochrane, IOP, APS, ACS, eScholarship |
| `saude` | medicina e saúde pública | PubMed/PMC, Europe PMC, Cochrane, WHO, CDC, NIH, ECDC, NICE, ClinicalTrials.gov, FDA, EMA, NEJM, Lancet, BMJ, JAMA, Nature, medRxiv, BVS/LILACS, SciELO, Annals, AHA, PLOS |
| `computacao` | informática, IA e engenharia de software | arXiv, ACL Anthology, OpenReview, NeurIPS, PMLR, JMLR, ACM DL, IEEE Xplore, USENIX, DBLP, Semantic Scholar, CVF, AAAI, IJCAI, VLDB, Springer, ScienceDirect, Hugging Face |
| `oficial` | estatística, normas, governo | WHO, OECD, World Bank, IMF, ONU, UNESCO, OIT, UE (europa.eu), IBGE, gov.br, IPEA, BCB, INE, Pordata, US Census, BLS, NIST, ISO, IETF/RFC Editor, W3C, ITU, data.gov, gov.uk, ONS |

A lista exata está em `DOMAIN_PRESETS` no `scripts/tavily.py`. Para uma área
sem preset, monte a sua com `--include-domains` (máx. 300 domínios; só
domínios, sem caminhos nem curingas).

## 3. Táticas de pesquisa que funcionam

- **Revisões primeiro**: `"<tema>" systematic review` ou `meta-analysis`,
  `--preset academico`. Uma boa revisão dá o mapa e as referências (ponto de
  partida do *snowballing*).
- **Amplo → estreito**: termos gerais primeiro. Depois os termos técnicos,
  autores e métodos que aparecerem.
- **Contenção no clique**: percorra a lista inteira de resultados antes de
  abrir algum. Prefira a fonte primária ao agregador.
- **Frase exata**: `search '"frase exata"' --exact` confirma de onde vem uma
  citação e encontra as outras fontes que a repetem.
- **Janela temporal**: `--start-date AAAA-MM-DD` para o estado da arte;
  `--time-range month --topic news` para atualidade.
- **Excluir ruído**: `--exclude-domains` para *content farms*, agregadores
  ou o próprio site já lido (quando procura cobertura independente).
- **Idiomas**: repita as consultas centrais em inglês e no idioma local.
  SciELO, BDTD e RCAAP cobrem a produção lusófona.
- **PDFs do arXiv**: extraia `https://arxiv.org/pdf/<id>` (ou
  `https://arxiv.org/html/<id>` quando existir). A página `abs` é quase só
  navegação.

## 4. APIs académicas abertas (metadados, citações e verificação)

✔ = limites e comportamento confirmados por verificação adversarial
(2026-09). As restantes linhas vêm da documentação pública conhecida:
confirme-a antes de uso intensivo. Leia sempre os limites nos **cabeçalhos
das respostas** (`X-RateLimit-*`, `x-concurrency-limit`…), porque mudaram
várias vezes em 2025–2026.

| Base | Cobre | Endpoint de exemplo | Acesso e limites |
| --- | --- | --- | --- |
| **OpenAlex** ✔ | ~todas as áreas; obras, autores, instituições, citações | `https://api.openalex.org/works?search=retrieval%20augmented&api_key=CHAVE` · `…/works/doi:10.18653/v1/2024.naacl-long.347` | consultas básicas **sem chave**, mas a pesquisa anónima já esteve suspensa (503): use a **chave gratuita** (10× o orçamento sem chave; `api_key=` ou `Authorization: Bearer`). Orçamento diário em USD nos cabeçalhos `X-RateLimit-*-USD`. **429** ao exceder o orçamento ou acima de 100 req/s. O `mailto=` já não conta |
| **Crossref** ✔ | DOIs de quase todas as editoras | `https://api.crossref.org/works?query.bibliographic=STORM+Wikipedia&rows=5&mailto=VOCE@EXEMPLO` · `…/works/<DOI>` | grátis, sem registo; `mailto=` (ou User-Agent com e-mail) põe-no no pool «polite», o certo para verificações. **Não fixe números**: leia `x-rate-limit-*` e `x-concurrency-limit` em cada resposta; 429 → abrande. Guarde em cache |
| **Semantic Scholar** ✔ | ~214 M artigos, 2,5 mil M citações; forte em computação e biomédica | `https://api.semanticscholar.org/graph/v1/paper/search?query=…&fields=title,year,externalIds,citationCount` · `…/paper/arXiv:2402.14207/references` · `…/citations` | a maioria sem chave, mas os anónimos partilham um só pool global (1000 req/s para **todos**) e sofrem *throttling* (429/500). A chave gratuita (formulário, por e-mail) dá **1 req/s** em todos os endpoints: ritmo de 1 req/s somando todos os agentes, e retry no 429 |
| **arXiv** ✔ | *preprints* de física, matemática, CS e estatística | `https://export.arxiv.org/api/query?search_query=all:%22retrieval%20augmented%22&max_results=10` (Atom XML) | **1 pedido a cada 3 s, uma só ligação, somando todas as máquinas**. Contornar (por exemplo com mais máquinas ou subagentes) é proibido e dá bloqueio. Em 2026 a aplicação ficou mais estrita (429/503 mesmo a 3–4 s). Prefira os metadados do OpenAlex ou da Semantic Scholar e leia o PDF com `tavily.py extract`. Metadados CC0 |
| **PubMed (E-utilities)** | biomedicina (MEDLINE) | `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=…&retmode=json` → `esummary.fcgi?db=pubmed&id=…` | grátis; ~3 req/s sem chave, ~10 com chave NCBI |
| **Europe PMC** | PubMed + PMC + *preprints*; texto integral OA | `https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=…&format=json` · `…/rest/MED/<PMID>/citations` · `…/references` | grátis, sem chave |
| **Unpaywall** | versão de acesso aberto de um DOI | `https://api.unpaywall.org/v2/<DOI>?email=VOCE@EXEMPLO` | grátis com e-mail |
| **CORE** | texto integral de repositórios abertos | `https://api.core.ac.uk/v3/search/works?q=…` | chave gratuita |
| **DOAJ** | revistas e artigos de acesso aberto revistos por pares | `https://doaj.org/api/search/articles/<consulta>` | grátis; útil para confirmar se uma revista é legítima |
| **DBLP** | bibliografia de informática | `https://dblp.org/search/publ/api?q=…&format=json` | grátis |
| **bioRxiv / medRxiv** | *preprints* de biologia e medicina | `https://api.biorxiv.org/details/biorxiv/<DOI>` · `…/pubs/biorxiv/<intervalo>` (versão publicada) | grátis |
| **OpenReview** | artigos e **revisões** de conferências de ML | `https://api2.openreview.net/notes?content.venueid=…` | grátis |
| **SciELO** | produção ibero-americana (PT/ES) revista por pares | pesquisa: `https://search.scielo.org/?q=…`; via script: `--preset academico` ou `--include-domains scielo.org,scielo.br` | grátis |
| **Hugging Face Papers** | artigos de ML em destaque, com código | `https://huggingface.co/api/papers/<arXiv-id>` · `https://huggingface.co/papers` | grátis; ocupa o lugar do *Papers with Code*, descontinuado |

Sem API oficial (não raspe): **Google Scholar**, ResearchGate, Academia.edu.
Use-os só como pistas e confirme sempre numa das bases acima.

### Fontes lusófonas e oficiais úteis

- **Brasil**: SciELO, BDTD (teses e dissertações, `bdtd.ibict.br`), BVS/LILACS
  (`bvsalud.org`), Portal de Periódicos CAPES (assinatura), IBGE, IPEA, BCB,
  `gov.br` (legislação e dados).
- **Portugal**: RCAAP (repositórios científicos, `rcaap.pt`), INE, Pordata,
  Diário da República (`dre.pt`).
- **Internacional**: OECD, World Bank, WHO, Eurostat (`europa.eu`), normas
  ISO, IETF (RFC) e W3C.

## 5. *Snowballing* de citações (depois de encontrar 2–3 artigos-chave)

Faça-o pelo **bibliotecário**, em série.

1. **Para trás** (o que o artigo cita): Semantic Scholar
   `/paper/<id>/references` ✔, o campo `referenced_works` do OpenAlex, ou
   Europe PMC `/references`.
2. **Para a frente** (quem o cita depois): Semantic Scholar
   `/paper/<id>/citations` ✔, OpenAlex `works?filter=cites:<W-id>`, ou Europe
   PMC `/citations`. É assim que encontra réplicas, críticas e atualizações.
3. **Pare** quando as novas iterações só devolverem obras que já estão nas
   Fontes: saturação local.
4. Cada obra nova entra como fonte candidata e é **lida** (`extract`) antes de
   sustentar alguma afirmação.

Identificadores aceites pela Semantic Scholar: `DOI:10.…`, `arXiv:2402.14207`,
`PMID:…`, `CorpusId:…`.

## 6. Verificar que uma citação é real (obrigatório para fontes centrais)

1. **DOI resolve**: `https://api.crossref.org/works/<DOI>` (ou
   `https://api.openalex.org/works/doi:<DOI>`) devolve um registo.
2. **Metadados batem certo** ✔: título, 1.º autor, ano, veículo e
   volume/páginas. Existir não chega. Nas referências **reais** citadas por
   LLMs sem pesquisa, 24–43 % tinham erros substantivos, sobretudo em
   volume, páginas e ano (Walters e Wilder, 2023).
3. **Retratação** ✔: o Crossref inclui os dados da Retraction Watch. No
   registo do **artigo citado** (`/works/<DOI>`), leia o *array*
   `updated-by`: cada entrada é uma retratação, correção ou nota, e o campo
   `source` diz se vem de `publisher` ou de `retraction-watch`. As notas de
   retratação têm o campo inverso, `update-to`. Em alternativa, consulte
   `/works?filter=updates:<DOI>`. A lista completa está em
   `/v1/works?filter=update-type:retraction`.
4. ***Preprint* ou publicado?** Se a fonte é um *preprint*, procure a versão
   publicada: bioRxiv `/pubs`, ou o mesmo título no OpenAlex ou na Semantic
   Scholar. A versão revista por pares prevalece.
5. **Revista legítima?** Confirme-a no DOAJ (acesso aberto) ou pelo ISSN. Uma
   revista real não prova que o artigo exista: nas citações inventadas, a
   revista costuma ser real.
6. **O link não prova nada**: as referências inventadas trazem links com mais
   frequência do que as reais.
7. **Ferramentas automáticas só fazem triagem** ✔: os verificadores de
   referências apanham a maioria das inventadas, mas com muitos falsos
   positivos. Num teste com 104 referências, o melhor apanhou 32 das 33
   problemáticas e deu 36 falsos alarmes. Cada alerta é confirmado à mão na
   fonte: DOI, título, autores e veículo.

## 7. Referências desta secção

- arXiv, *API Terms of Use* — https://info.arxiv.org/help/api/tou.html
- Semantic Scholar API — https://www.semanticscholar.org/product/api
- OpenAlex, autenticação e custos — https://help.openalex.org/guides/authentication
- Crossref, acesso à REST API — https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/
- Crossref, Retraction Watch — https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/
- Walters & Wilder, *Sci Rep* 2023 (referências inventadas) — https://doi.org/10.1038/s41598-023-41032-5
- Chelli et al., *JMIR* 2024 — https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11153973/
- Badalova & Mayr, verificadores de referências (preprint, 2026) — https://arxiv.org/abs/2607.22693

## 8. Níveis de fonte (resumo — detalhe em `pesquisa-profunda.md` §5.2)

**A**: revisão sistemática, artigo revisto por pares, estatística ou norma
oficial, documentação primária. **B**: *preprint* identificável, relatório
técnico, livro académico, blogue de engenharia oficial com dados. **C**:
imprensa de referência, blogue especializado com fontes. **D**: fórum, SEO,
marketing, sem autor, ou sinalizada pelo escudo. Uma fonte D nunca sustenta
sozinha uma afirmação central.
