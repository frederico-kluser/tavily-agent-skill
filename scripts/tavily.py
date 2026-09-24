#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tavily.py — pesquisa web Tavily com rotação AUTOMÁTICA e transparente de chaves.

Contrato com o agente que invoca:
  * exit 0  → apenas o resultado no stdout; erros de rotação NUNCA aparecem;
  * exit ≠ 0 → mensagem instrutiva no formato `Erro: …` / `Solução: …`.

Gestão de requests (o ponto central): se uma request morrer — limite de taxa
(429), cota esgotada (432/433), chave inválida (401), timeout, erro de servidor
(5xx) ou falha de rede — o script marca a credencial, escolhe OUTRA chave e
refaz a MESMA request. Se todo o pool estiver impedido, espera pelo cooldown
mais curto (até --max-wait) e repete; só depois disso desiste com erro
instrutivo. A contingência keyless (X-Tavily-Access-Mode) entra como penúltimo
recurso, tal como no plugin DSH.

Chaves (formato partilhado com o plugin dsh-tavily-resilient-search):
  TAVILY_API_KEY          chave única (opcional)
  TAVILY_API_KEY_A..Z     agrupamento de contas (round-robin determinístico)

Apenas stdlib. Estado efémero por invocação (nada é escrito em disco).
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.tavily.com/search"
MAX_RESULTS_CAP = 10
DEFAULT_TIMEOUT = 25.0
DEFAULT_MAX_WAIT = 30.0
DEFAULT_MAX_BYTES = 50 * 1024
COOLDOWN_BASE_S = 0.5
COOLDOWN_MAX_S = 60.0
COOLDOWN_JITTER_S = 0.5
MAX_PASSES = 6

VERBOSITY = False


def logv(message: str) -> None:
    """Diagnóstico de rotação em stderr — silencioso por omissão (ver --verbose)."""
    if VERBOSITY:
        print(f"[tavily] {message}", file=sys.stderr)


class SkillError(Exception):
    """Erro instrutivo: o texto diz ao agente exatamente o que fazer a seguir."""

    def __init__(self, problem: str, solution: str) -> None:
        super().__init__(f"Erro: {problem}\nSolução: {solution}")
        self.problem = problem
        self.solution = solution


# --------------------------------------------------------------------------
# credenciais
# --------------------------------------------------------------------------

@dataclass
class KeyMeta:
    env_name: str
    key: str
    status: str = "ACTIVE"  # ACTIVE | RATE_LIMITED | QUOTA_EXHAUSTED | REVOKED
    cooldown_until: float = 0.0
    failures: int = 0
    total_requests: int = 0

    @property
    def ref(self) -> str:
        """Identificação sem material de segredo (apenas os últimos 4)."""
        return "…" + (self.key[-4:] if len(self.key) >= 4 else "····")


def load_keys(env: dict[str, str] | None = None) -> list[KeyMeta]:
    """Lê o pool a partir do terminal, em ordem determinística (Pass^k-friendly)."""
    env = os.environ if env is None else env
    pool: list[KeyMeta] = []
    single = (env.get("TAVILY_API_KEY") or "").strip()
    if single:
        pool.append(KeyMeta("TAVILY_API_KEY", single))
    for name in sorted(k for k in env if k.startswith("TAVILY_API_KEY_")):
        value = (env[name] or "").strip()
        if value and all(value != k.key for k in pool):
            pool.append(KeyMeta(name, value))
    return pool


def redact(text: str, pool: list[KeyMeta]) -> str:
    """Canário de segredos por valor: material de chave nunca sai em claro."""
    out = text
    for k in pool:
        if len(k.key) >= 4 and k.key in out:
            out = out.replace(k.key, "[REDACTED]")
    return out


# --------------------------------------------------------------------------
# máquina de estados do pool
# --------------------------------------------------------------------------

def revive(pool: list[KeyMeta], now: float) -> None:
    """Recuperação lazy: cooldowns expirados voltam a ACTIVE (REVOKED nunca)."""
    for k in pool:
        if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and now >= k.cooldown_until:
            k.status, k.cooldown_until, k.failures = "ACTIVE", 0.0, 0


def pick_key(pool: list[KeyMeta], cursor: int, now: float) -> tuple[KeyMeta | None, int]:
    """Round-robin sobre as ACTIVE; None → contingência keyless."""
    revive(pool, now)
    n = len(pool)
    for i in range(n):
        idx = (cursor + i) % n
        if pool[idx].status == "ACTIVE":
            return pool[idx], idx + 1
    return None, cursor


def mark_rate_limited(k: KeyMeta, retry_after_s: float | None, now: float, rng) -> None:
    k.status = "RATE_LIMITED"
    k.failures += 1
    if retry_after_s is not None:
        delay = max(0.0, retry_after_s)
    else:
        delay = min(COOLDOWN_MAX_S, COOLDOWN_BASE_S * (2 ** k.failures)) + rng() * COOLDOWN_JITTER_S
    k.cooldown_until = now + delay


def mark_quota_exhausted(k: KeyMeta, now: float) -> None:
    """432/433: a Tavily repõe cotas no 1.º dia do mês civil seguinte (UTC)."""
    k.status = "QUOTA_EXHAUSTED"
    k.failures += 1
    t = time.gmtime(now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    k.cooldown_until = float(calendar.timegm((year, month, 1, 0, 0, 0)))


def mark_revoked(k: KeyMeta) -> None:
    k.status = "REVOKED"


# --------------------------------------------------------------------------
# transporte HTTP (injetável para o selftest)
# --------------------------------------------------------------------------

def classify(status: int) -> str:
    if 200 <= status < 300:
        return "success"
    if status == 400:
        return "invalid-request"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate-limited"
    if status == 432:
        return "quota-exhausted"
    if status == 433:
        return "paygo-exhausted"
    if 500 <= status < 600:
        return "transient"
    return "unexpected"


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Retry-After em segundos (delta inteiro ou data HTTP)."""
    if not value:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        return max(0.0, calendar.timegm(time.strptime(text, "%a, %d %b %Y %H:%M:%S %Z")) - now)
    except ValueError:
        return None


def http_post_json(url: str, headers: dict[str, str], body: dict, timeout: float):
    """Devolve (status:int, payload:dict|None, retry_after:str|None) ou levanta OSError."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            return response.status, payload, response.headers.get("Retry-After")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return exc.code, payload, exc.headers.get("Retry-After") if exc.headers else None


def error_detail(payload) -> str:
    detail = (payload or {}).get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return detail["error"]
    if isinstance(detail, str):
        return detail
    return ""


# --------------------------------------------------------------------------
# orçamento de contexto e normalização
# --------------------------------------------------------------------------

def truncate_utf8(text: str, budget: int) -> tuple[str, bool]:
    """Trunca em limite de ponto de código (nunca parte um code point UTF-8)."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, False
    cut = raw[:budget].decode("utf-8", "ignore")
    return cut, True


def apply_budget(result: dict, max_bytes: int) -> dict:
    """Orçamento PARTILHADO (resposta + fontes) — salvaguarda de contexto."""
    remaining = max_bytes
    truncated = False
    answer = result.get("answer")
    if isinstance(answer, str) and answer:
        new, cut = truncate_utf8(answer, remaining)
        remaining -= len(new.encode("utf-8"))
        truncated = truncated or cut
        result["answer"] = new if new else None
    for item in result.get("results", []):
        new, cut = truncate_utf8(str(item.get("content", "")), remaining)
        remaining -= len(new.encode("utf-8"))
        truncated = truncated or cut
        item["content"] = new
    result["meta"]["truncated"] = truncated
    return result


def redact_result(result: dict, pool: list[KeyMeta]) -> dict:
    """Redação por valor no valor canónico (defesa em profundidade)."""
    result["query"] = redact(str(result.get("query", "")), pool)
    if isinstance(result.get("answer"), str):
        result["answer"] = redact(result["answer"], pool)
    for item in result.get("results", []):
        for field_name in ("title", "url", "content"):
            item[field_name] = redact(str(item.get(field_name, "")), pool)
    return result


# --------------------------------------------------------------------------
# o ciclo transparente de gestão de requests
# --------------------------------------------------------------------------

def run_search(
    query: str,
    *,
    depth: str = "basic",
    max_results: int = 5,
    topic: str = "general",
    include_answer: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
    max_wait: float = DEFAULT_MAX_WAIT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    pool: list[KeyMeta] | None = None,
    post=None,
    sleep=time.sleep,
    rng=random.random,
    now=time.time,
) -> dict:
    """
    Executa a pesquisa DONDE o chamador nunca vê falhas transitórias:
    qualquer morte de request (429/432/433/401/5xx/rede/timeout) provoca
    rotação de credencial e reemissão da MESMA request.
    """
    if pool is None:
        pool = load_keys()
    if post is None:
        post = http_post_json

    query = (query or "").strip()
    if not query:
        raise SkillError(
            "a consulta está vazia.",
            "passe o texto a pesquisar entre aspas: tavily.py search \"o seu tema\".",
        )
    if len(query) > 4000:
        raise SkillError(
            f"a consulta tem {len(query)} caracteres (limite da API: 4000).",
            "resuma a consulta para menos de 1500 caracteres (recomendado) e repita.",
        )

    body = {
        "query": query,
        "search_depth": depth,
        "max_results": max(1, min(int(max_results), MAX_RESULTS_CAP)),
        "topic": topic,
        "include_answer": bool(include_answer),
        "chunks_per_source": 3,
    }

    attempts = 0
    keyless_used = False
    cursor = 0
    waited = 0.0

    for _pass in range(MAX_PASSES):
        for _slot in range(len(pool) + 1):  # +1 = contingência keyless
            attempts += 1
            key, cursor = pick_key(pool, cursor, now())
            headers = {"Content-Type": "application/json"}
            if key is None:
                headers["X-Tavily-Access-Mode"] = "keyless"
                keyless_used = True
                label = "keyless"
            else:
                headers["Authorization"] = "Bearer " + key.key
                key.total_requests += 1
                label = key.ref

            try:
                status, payload, retry_after = post(API_URL, headers, body, timeout)
            except OSError as exc:
                logv(f"falha de rede/timeout com {label}: {exc} — a rotacionar")
                continue  # transiente: a MESMA request segue para a próxima chave

            kind = classify(status)
            if kind == "success":
                result = normalize(payload, query)
                result["meta"] = {
                    "attempts": attempts,
                    "keyless_used": keyless_used,
                    "truncated": False,
                    "keys_total": len(pool),
                }
                return apply_budget(redact_result(result, pool), max_bytes)

            if kind == "invalid-request":
                detail = error_detail(payload) or f"HTTP {status}"
                raise SkillError(
                    f"a API rejeitou a consulta ({detail}).",
                    "simplifique a consulta (sem caracteres especiais) ou use --depth basic.",
                )
            if kind == "forbidden":
                raise SkillError(
                    f"acesso interdito pela API (HTTP {status}).",
                    "o recurso não está disponível para esta conta — tente outra consulta ou outra conta Tavily.",
                )
            if kind == "unexpected":
                raise SkillError(
                    f"resposta inesperada da API (HTTP {status}: {error_detail(payload) or 'sem detalhe'}).",
                    "repita a invocação; se persistir, corra `tavily.py status --check` para auditar as chaves.",
                )
            if key is None:
                logv(f"contingência keyless respondeu HTTP {status} — a continuar")
                continue
            if kind == "unauthorized":
                logv(f"chave {key.ref} inválida (401) — removida do pool e a rotacionar")
                mark_revoked(key)
                continue
            if kind == "rate-limited":
                wait = parse_retry_after(retry_after, now())
                logv(f"chave {key.ref} em rate limit (429) — a rotacionar")
                mark_rate_limited(key, wait, now(), rng)
                continue
            if kind in ("quota-exhausted", "paygo-exhausted"):
                logv(f"chave {key.ref} sem cota (HTTP {status}) — suspensa e a rotacionar")
                mark_quota_exhausted(key, now())
                continue
            logv(f"instabilidade transitiva (HTTP {status}) com {key.ref} — a rotacionar")

        # pool inteiro impedido nesta passagem: espera pelo cooldown mais curto
        # e repete a MESMA request (transparência total para quem chama).
        remaining = min((k.cooldown_until - now() for k in pool), default=0.0)
        if remaining > 0 and waited + remaining <= max_wait:
            pause = remaining + rng() * COOLDOWN_JITTER_S
            logv(f"todas as chaves em arrefecimento — a aguardar {pause:.1f}s e a repetir a mesma request")
            sleep(pause)
            waited += pause
            continue
        break

    masked = ", ".join(k.ref for k in pool) or "nenhuma"
    raise SkillError(
        f"todas as alternativas falharam ({attempts} tentativas; pool: {masked}; contingência keyless incluída).",
        f"aguarde ~{max(1, int(remaining))}s e repita a MESMA invocação, ou declare mais chaves "
        f"(export TAVILY_API_KEY_E=\"tvly-…\") e repita. `tavily.py status --check` audita o pool.",
    )


def normalize(payload, query: str) -> dict:
    data = payload if isinstance(payload, dict) else {}
    results = []
    for item in data.get("results") or []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title", "")),
                "url": str(item.get("url", "")),
                "content": str(item.get("content", "")),
                "score": float(item.get("score") or 0.0),
            }
        )
    answer = data.get("answer")
    return {
        "query": str(data.get("query") or query),
        "answer": answer if isinstance(answer, str) else None,
        "results": results,
        "response_time": data.get("response_time"),
    }


# --------------------------------------------------------------------------
# apresentação
# --------------------------------------------------------------------------

def render_text(result: dict, pool: list[KeyMeta]) -> str:
    lines: list[str] = []
    if result.get("answer"):
        lines.append(result["answer"].rstrip())
        lines.append("")
    results = result.get("results", [])
    if results:
        lines.append("Fontes:")
        for i, item in enumerate(results, 1):
            lines.append(f"{i}. {item['title']} — {item['url']}")
            snippet = " ".join(item["content"].split())
            if snippet:
                lines.append(f"   {snippet[:300]}")
    elif not result.get("answer"):
        lines.append("Sem resultados para esta consulta.")
    meta = result.get("meta", {})
    flags = []
    if meta.get("truncated"):
        flags.append("texto truncado no orçamento de contexto")
    if meta.get("keyless_used"):
        flags.append("usado modo keyless")
    if flags:
        lines.append("")
        lines.append("(" + "; ".join(flags) + ")")
    return redact("\n".join(lines) + "\n", pool)


# --------------------------------------------------------------------------
# status e selftest
# --------------------------------------------------------------------------

def cmd_status(check: bool, timeout: float) -> int:
    pool = load_keys()
    print(f"Pool Tavily: {len(pool)} credencial(is)" + ("" if pool else " — modo keyless puro"))
    for k in pool:
        print(f"  {k.env_name:<20} {k.ref}  [{k.status}]")
    if not pool:
        print("\nNenhuma chave no terminal. Declaração (formato do plugin DSH):")
        print('  export TAVILY_API_KEY_A="tvly-..."')
        print('  export TAVILY_API_KEY_B="tvly-..."')
        print("Sem chaves o script opera em modo keyless (limites mais severos).")
        return 0
    if check:
        print("\nVerificação ao vivo (gasta 1 crédito por chave)…")
        for k in pool:
            try:
                status, _payload, _ra = http_post_json(
                    API_URL,
                    {"Content-Type": "application/json", "Authorization": "Bearer " + k.key},
                    {"query": "ping", "search_depth": "ultra-fast", "max_results": 1, "topic": "general",
                     "include_answer": False, "chunks_per_source": 1},
                    timeout,
                )
                verdict = "VÁLIDA" if classify(status) == "success" else f"HTTP {status}"
            except OSError as exc:
                verdict = f"sem resposta ({exc})"
            print(f"  {k.env_name:<20} {k.ref}  → {verdict}")
        print("\nNotas: 401 = revogada · 429 = rate limit (recupera sozinha) · 432 = cota (repõe no 1.º do mês).")
    return 0


def cmd_selftest() -> int:
    """Prova determinística da máquina de rotação (offline, sem chaves reais)."""
    failures = 0

    def scenario(name: str, fn) -> None:
        nonlocal failures
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")

    def make_pool(*suffixes: str) -> list[KeyMeta]:
        return [KeyMeta(f"TAVILY_API_KEY_{s}", f"tvly-KEY-{s}-{'x' * 20}{s}") for s in suffixes]

    def transport(script):
        calls = []

        def post(_url, headers, body, _timeout):
            calls.append({"headers": dict(headers), "body": dict(body)})
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        return post, calls

    OK = (200, {"query": "q", "results": [{"title": "t", "url": "u", "content": "c", "score": 1}], "answer": "a"}, None)

    def s_rotaciona_apos_401():
        pool = make_pool("A", "B")
        post, calls = transport([(401, {"detail": {"error": "bad"}}, None), OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        assert result["meta"]["attempts"] == 2, "devia tentar duas vezes"
        assert pool[0].status == "REVOKED" and pool[1].status == "ACTIVE"
        assert calls[1]["headers"]["Authorization"].endswith("B"), "a 2.ª tentativa devia usar a chave B"

    def s_rotaciona_apos_429():
        pool = make_pool("A", "B")
        post, _c = transport([(429, {}, "2"), OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0)
        assert result["meta"]["attempts"] == 2
        assert pool[0].status == "RATE_LIMITED" and pool[0].cooldown_until == 2.0

    def s_quota_ate_mes_seguinte():
        pool = make_pool("A", "B")
        post, _c = transport([(432, {}, None), OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0)
        assert pool[0].status == "QUOTA_EXHAUSTED"
        assert pool[0].cooldown_until > 0, "devia suspender até ao mês seguinte"

    def s_keyless_quando_pool_morto():
        pool = make_pool("A")
        post, calls = transport([(429, {}, "60"), OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        assert result["meta"]["keyless_used"] is True
        assert calls[1]["headers"].get("X-Tavily-Access-Mode") == "keyless"
        assert "Authorization" not in calls[1]["headers"], "keyless não pode levar credencial"

    def s_espera_e_repete_a_mesma_request():
        pool = make_pool("A")
        clock = [0.0]
        sleeps: list[float] = []
        script = [(429, {}, "1"), (503, {}, None), OK]

        def post(_url, headers, body, _t):
            step = script.pop(0)
            assert body["query"] == "q", "a request repetida tem de ser a MESMA"
            return step

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds  # o relógio avança: a chave A recupera

        result = run_search(
            "q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0], max_wait=30
        )
        assert result["meta"]["attempts"] == 3
        assert sleeps and sleeps[0] >= 1.0, "devia esperar o cooldown e repetir"
        assert pool[0].status == "ACTIVE", "a chave A devia ter recuperado e servido a repetição"

    def s_400_terminal_sem_rotacao():
        pool = make_pool("A", "B")
        post, calls = transport([(400, {"detail": {"error": "malformed"}}, None)])
        try:
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
            raise AssertionError("devia falhar")
        except SkillError as exc:
            assert "Solução:" in str(exc), "erro tem de ser instrutivo"
        assert len(calls) == 1, "400 não deve gastar mais chaves"

    def s_rede_morre_e_rotaciona():
        pool = make_pool("A", "B")
        post, _c = transport([OSError("ECONNRESET"), OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        assert result["meta"]["attempts"] == 2

    def s_redacao_de_segredos():
        pool = make_pool("A")
        leaky = (200, {"query": "q", "results": [{"title": "t", "url": "u", "content": f"eco {pool[0].key}", "score": 1}]}, None)
        post, _c = transport([leaky])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        assert pool[0].key not in json.dumps(result), "a chave não pode vazar na saída"

    def s_truncagem_no_orcamento():
        pool = make_pool("A")
        bomb = (200, {"query": "q", "results": [{"title": "t", "url": "u", "content": "x" * 200_000, "score": 1}]}, None)
        post, _c = transport([bomb])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, max_bytes=1024)
        assert result["meta"]["truncated"] is True
        assert len(result["results"][0]["content"].encode()) <= 1024

    def s_rotacao_deterministica_na_request():
        pool = make_pool("A", "B", "C")
        post, calls = transport([(500, {}, None), (503, {}, None), OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        used = [c["headers"]["Authorization"][-1] for c in calls]
        assert used == ["A", "B", "C"], f"ordem de rotação {used} não é A→B→C"
        assert result["meta"]["attempts"] == 3

    def s_determinismo_pass_k():
        def once() -> list[str]:
            pool = make_pool("A", "B")
            post, calls = transport([(429, {}, "5"), OK])
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0)
            return [c["headers"].get("Authorization", "keyless")[-1] for c in calls]

        assert once() == once(), "execuções idênticas devem ter o MESMO comportamento (Pass^k)"

    print("selftest tavily.py — máquina de rotação (offline):")
    scenarios = [
        ("rotação após 401 (chave morta → próxima e MESMA request)", s_rotaciona_apos_401),
        ("rotação após 429 com Retry-After", s_rotaciona_apos_429),
        ("432 suspende até ao mês seguinte", s_quota_ate_mes_seguinte),
        ("pool morto → contingência keyless sem credencial", s_keyless_quando_pool_morto),
        ("pool em cooldown → espera e repete a MESMA request", s_espera_e_repete_a_mesma_request),
        ("400 é terminal (sem gastar chaves) e instrutivo", s_400_terminal_sem_rotacao),
        ("falha de rede morre e rotaciona", s_rede_morre_e_rotaciona),
        ("segredos redigidos na saída", s_redacao_de_segredos),
        ("truncagem no orçamento de contexto", s_truncagem_no_orcamento),
        ("rotação dentro da request é A→B→C", s_rotacao_deterministica_na_request),
        ("determinismo Pass^k: mesma entrada, mesmo comportamento", s_determinismo_pass_k),
    ]
    for name, fn in scenarios:
        scenario(name, fn)
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} cenários OK")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    global VERBOSITY
    parser = argparse.ArgumentParser(
        prog="tavily.py",
        description="Pesquisa web Tavily com rotação automática de chaves (o chamador nunca vê erros de rotação).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="pesquisa na web com rotação transparente")
    p_search.add_argument("query", help="texto a pesquisar (recomendado < 1500 caracteres)")
    p_search.add_argument("--json", action="store_true", help="saída JSON estruturada (para citar)")
    p_search.add_argument("--depth", default="basic", choices=["ultra-fast", "fast", "basic", "advanced"])
    p_search.add_argument("--max-results", type=int, default=5)
    p_search.add_argument("--topic", default="general", choices=["general", "news"])
    p_search.add_argument("--no-answer", action="store_true", help="não pedir a resposta sumarizada")
    p_search.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout por tentativa (s)")
    p_search.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT, help="espera máx. por cooldown antes de desistir (s)")
    p_search.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="orçamento de texto na saída")
    p_search.add_argument("--verbose", action="store_true", help="traços de rotação em stderr")

    p_status = sub.add_parser("status", help="estado do pool de chaves (sem segredos)")
    p_status.add_argument("--check", action="store_true", help="validação ao vivo (gasta 1 crédito/chave)")
    p_status.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    sub.add_parser("selftest", help="verificação determinística da máquina de rotação (offline)")

    args = parser.parse_args(argv)

    try:
        if args.command == "selftest":
            return cmd_selftest()
        if args.command == "status":
            return cmd_status(args.check, args.timeout)

        VERBOSITY = bool(args.verbose)
        pool = load_keys()
        result = run_search(
            args.query,
            depth=args.depth,
            max_results=args.max_results,
            topic=args.topic,
            include_answer=not args.no_answer,
            timeout=args.timeout,
            max_wait=args.max_wait,
            max_bytes=args.max_bytes,
            pool=pool,
        )
        if args.json:
            print(redact(json.dumps(result, ensure_ascii=False, indent=2), pool))
        else:
            sys.stdout.write(render_text(result, pool))
        return 0
    except SkillError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Erro: interrompido pelo utilizador.\nSolução: invoque novamente quando quiser.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
