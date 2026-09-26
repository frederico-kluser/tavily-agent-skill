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
recurso.

Controlos de pool (persistentes entre invocações):
  * REGISTO INTERNO por chave (`pool-state.json`) — estado (ACTIVE/…), cooldowns,
    contadores, último erro, consumo (/usage) e cursor de round-robin. A próxima
    invocação NÃO recomeça do início nem re-tenta chaves mortas: retoma onde
    ficou e salta o que já se provou não funcionar.
  * LIMITE DE REQUESTS SIMULTÂNEAS por conta (in-flight cross-processo) com
    espera e retry; teto por `--max-inflight-per-key` / TAVILY_MAX_INFLIGHT_PER_KEY.
  * `status --check` valida ao vivo via endpoint /usage (NÃO gasta créditos de
    pesquisa; limite próprio de 10 req/10 min) e regista usados/limite/restantes.

Limites da API Tavily (medidos em 2026-09, ver references/api.md): 100 RPM por
chave dev / 1000 RPM produção nos endpoints normais; excesso → 429 com
Retry-After; 432/433 = cota do plano; 401/403 = chave inválida.

Chaves (pool com rotação automática):
  TAVILY_API_KEY          chave única (opcional)
  TAVILY_API_KEY_A..Z     agrupamento de contas (round-robin determinístico)

Apenas stdlib. O registo persiste APENAS metadados opacos (identificação por
sha256 da chave, nunca o material) em $TAVILY_STATE_DIR ou
$XDG_STATE_HOME/tavily-agent-skill — use `--no-state` para o modo efémero.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import random
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

try:  # pragma: no cover — fcntl existe em POSIX; sem ele degrada para lockless
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

API_URL = "https://api.tavily.com/search"
USAGE_URL = "https://api.tavily.com/usage"
MAX_RESULTS_CAP = 10
DEFAULT_TIMEOUT = 25.0
DEFAULT_MAX_WAIT = 30.0
DEFAULT_MAX_BYTES = 50 * 1024
DEFAULT_MAX_INFLIGHT = 2
COOLDOWN_BASE_S = 0.5
COOLDOWN_MAX_S = 60.0
COOLDOWN_JITTER_S = 0.5
CONCURRENCY_RETRY_S = 0.5
INFLIGHT_TTL_S = 120.0
MAX_PASSES = 6
STATE_VERSION = 1

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
    total_failures: int = 0
    last_error: str = ""
    last_used_at: float = 0.0
    usage: dict | None = field(default=None)
    # base contabilística para merge cross-processo (delta = atual − base)
    base_requests: int = 0
    base_failures: int = 0

    @property
    def ref(self) -> str:
        """Identificação sem material de segredo (apenas os últimos 4)."""
        return "…" + (self.key[-4:] if len(self.key) >= 4 else "····")

    @property
    def hash_id(self) -> str:
        """Identidade estável do registo — derivada da chave, nunca a revela."""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]


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
# registo persistente do pool (estado por chave + cursor + concorrência)
# --------------------------------------------------------------------------

def default_state_dir() -> str:
    env_dir = (os.environ.get("TAVILY_STATE_DIR") or "").strip()
    if env_dir:
        return env_dir
    base = (os.environ.get("XDG_STATE_HOME") or "").strip() or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "tavily-agent-skill")


STATUS_SEVERITY = {"ACTIVE": 0, "RATE_LIMITED": 1, "QUOTA_EXHAUSTED": 2, "REVOKED": 3}


def _merge_entry(existing: dict, incoming: dict, *, delta_requests: int = 0, delta_failures: int = 0) -> dict:
    """Merge cross-processo de registos por chave (sem lost updates):

    * contadores: existente + delta desta invocação (nunca perdem incrementos);
    * estado: vence a maior severidade (REVOKED é absorvente — uma vista antiga
      que não viu o 401 não ressuscita a chave);
    * cooldown: prevalece o mais longo;
    * resto (último erro/uso/consumo): o registo mais recente.
    """
    merged = dict(incoming)
    if STATUS_SEVERITY.get(str(existing.get("status")), 0) > STATUS_SEVERITY.get(str(incoming.get("status")), 0):
        merged["status"] = existing.get("status")
    merged["cooldown_until"] = max(float(existing.get("cooldown_until") or 0.0),
                                   float(incoming.get("cooldown_until") or 0.0))
    merged["total_requests"] = int(existing.get("total_requests") or 0) + max(0, delta_requests)
    merged["total_failures"] = int(existing.get("total_failures") or 0) + max(0, delta_failures)
    merged["failures"] = max(int(existing.get("failures") or 0), int(incoming.get("failures") or 0))
    merged["last_used_at"] = max(float(existing.get("last_used_at") or 0.0),
                                 float(incoming.get("last_used_at") or 0.0))
    if float(existing.get("updated_at") or 0.0) > float(incoming.get("updated_at") or 0.0):
        merged["last_error"] = existing.get("last_error") or merged.get("last_error") or ""
        if isinstance(existing.get("usage"), dict):
            merged["usage"] = existing["usage"]
    return merged


class _FileLock:
    """flock exclusivo cross-processo (no-op quando indisponível ou em memória)."""
    def __init__(self, path: str | None):
        self.path = path
        self.handle = None

    def __enter__(self):
        if self.path and fcntl is not None:
            handle = None
            try:
                os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
                handle = open(self.path, "a+")
                os.chmod(self.path, 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self.handle = handle
            except OSError:
                if handle is not None:
                    handle.close()
                self.handle = None  # sem lock — melhor esforço, nunca parte a pesquisa
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class PoolState:
    """Registo interno do pool: estado por chave, cursor de round-robin e slots
    in-flight (limite de requests simultâneas por conta).

    Persiste entre invocações para que a próxima chamada NÃO recomece do início
    nem re-tente chaves que já se provaram mortas/sem cota. Identifica cada
    credencial por sha256(key)[:16] — o ficheiro NUNCA contém material de
    segredo. `dirpath=None` → modo memória (selftest / `--no-state`).
    """

    def __init__(self, dirpath: str | None = None):
        self.dirpath = dirpath
        self.degraded = False
        self.data: dict = {"version": STATE_VERSION, "cursor": None, "keys": {}, "inflight": {}}

    @classmethod
    def memory(cls) -> "PoolState":
        return cls(None)

    @classmethod
    def open(cls, dirpath: str | None = None) -> "PoolState":
        state = cls((dirpath or "").strip() or default_state_dir())
        state._load()
        return state

    # -- ficheiro ----------------------------------------------------------
    @property
    def _file(self) -> str:
        return os.path.join(self.dirpath or "", "pool-state.json")

    def describe(self) -> str:
        if self.degraded:
            return "indisponível (degradado para modo memória)"
        return self._file if self.dirpath else "modo memória (não persistente)"

    def _lock(self) -> _FileLock:
        return _FileLock(os.path.join(self.dirpath, "pool-state.lock") if self.dirpath else None)

    def _load(self) -> None:
        if not self.dirpath:
            return
        try:
            with open(self._file, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return  # ausente/corrompido → começa limpo
        if isinstance(data, dict) and data.get("version") == STATE_VERSION:
            self.data = {
                "version": STATE_VERSION,
                "cursor": data.get("cursor") if isinstance(data.get("cursor"), str) else None,
                "keys": data.get("keys") if isinstance(data.get("keys"), dict) else {},
                "inflight": data.get("inflight") if isinstance(data.get("inflight"), dict) else {},
            }

    def _save(self) -> None:
        if not self.dirpath:
            return
        try:
            os.makedirs(self.dirpath, mode=0o700, exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._file)
        except OSError as exc:
            # persistência é um BÓNUS: nunca pode partir uma pesquisa
            self.degraded = True
            self.dirpath = None
            logv(f"registo do pool indisponível ({exc}) — a continuar em modo memória")

    # -- sincronização com o pool ------------------------------------------
    def apply(self, pool: list[KeyMeta]) -> None:
        """Restaura o estado registado — chaves mortas/sem cota não são re-tentadas."""
        entries = self.data.get("keys") or {}
        for k in pool:
            entry = entries.get(k.hash_id)
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status in ("ACTIVE", "RATE_LIMITED", "QUOTA_EXHAUSTED", "REVOKED"):
                k.status = status
            k.cooldown_until = float(entry.get("cooldown_until") or 0.0)
            k.failures = int(entry.get("failures") or 0)
            k.total_requests = int(entry.get("total_requests") or 0)
            k.total_failures = int(entry.get("total_failures") or 0)
            k.last_error = str(entry.get("last_error") or "")
            k.last_used_at = float(entry.get("last_used_at") or 0.0)
            if isinstance(entry.get("usage"), dict):
                k.usage = entry["usage"]
            k.base_requests, k.base_failures = k.total_requests, k.total_failures

    def start_cursor(self, pool: list[KeyMeta]) -> int:
        """Retoma o round-robin DEPOIS da última chave usada (nunca sempre do início)."""
        cursor_id = self.data.get("cursor")
        if not cursor_id or not pool:
            return 0
        for i, k in enumerate(pool):
            if k.hash_id == cursor_id:
                return (i + 1) % len(pool)
        return 0

    def snapshot(self, pool: list[KeyMeta], cursor_hash: str | None = None, live: bool = False) -> None:
        """Regista o estado do pool com MERGE cross-processo (nunca perde
        observações alheias): contadores acumulam por delta, estados seguem a
        severidade (REVOKED é absorvente) e `live=True` (verificação ao vivo)
        substitui o estado com autoridade."""
        with self._lock():
            self._load()
            entries = self.data.setdefault("keys", {})
            for k in pool:
                incoming = {
                    "ref": k.ref,
                    "status": k.status,
                    "cooldown_until": k.cooldown_until,
                    "failures": k.failures,
                    "total_requests": k.total_requests,
                    "total_failures": k.total_failures,
                    "last_error": k.last_error,
                    "last_used_at": k.last_used_at,
                    "usage": k.usage,
                    "updated_at": time.time(),
                }
                existing = entries.get(k.hash_id)
                if isinstance(existing, dict) and not live:
                    incoming = _merge_entry(existing, incoming,
                                            delta_requests=k.total_requests - k.base_requests,
                                            delta_failures=k.total_failures - k.base_failures)
                entries[k.hash_id] = incoming
                k.base_requests, k.base_failures = k.total_requests, k.total_failures
            if cursor_hash:
                self.data["cursor"] = cursor_hash
            self._save()

    def reset(self) -> None:
        """Limpa o registo (recuperação: volta a considerar todas as chaves)."""
        self.data = {"version": STATE_VERSION, "cursor": None, "keys": {}, "inflight": {}}
        with self._lock():
            self._save()

    # -- concorrência: slots in-flight por conta ---------------------------
    def _prune_inflight(self, now: float) -> None:
        inflight = self.data.setdefault("inflight", {})
        for key_id in list(inflight):
            fresh = [ts for ts in inflight[key_id] if now - ts <= INFLIGHT_TTL_S]
            if fresh:
                inflight[key_id] = fresh
            else:
                del inflight[key_id]

    def try_acquire(self, key: KeyMeta, limit: int, now: float) -> bool:
        """Reserva um slot in-flight para a conta (limit<=0 → sem teto)."""
        if limit <= 0:
            return True
        with self._lock():
            self._load()
            self._prune_inflight(now)
            slots = self.data.setdefault("inflight", {}).setdefault(key.hash_id, [])
            if len(slots) >= limit:
                self._save()
                return False
            slots.append(now)
            self._save()
            return True

    def release(self, key: KeyMeta) -> None:
        """Liberta o slot in-flight mais antigo da conta."""
        with self._lock():
            self._load()
            slots = self.data.get("inflight", {}).get(key.hash_id)
            if slots:
                slots.pop(0)
                self._save()


# --------------------------------------------------------------------------
# máquina de estados do pool
# --------------------------------------------------------------------------

def revive(pool: list[KeyMeta], now: float) -> None:
    """Recuperação lazy: cooldowns expirados voltam a ACTIVE (REVOKED nunca)."""
    for k in pool:
        if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and now >= k.cooldown_until:
            k.status, k.cooldown_until, k.failures = "ACTIVE", 0.0, 0


def pick_key(pool: list[KeyMeta], cursor: int, now: float, skip: frozenset | set = frozenset()) -> tuple[KeyMeta | None, int]:
    """Round-robin sobre as ACTIVE (fora de `skip`); None → contingência keyless."""
    revive(pool, now)
    n = len(pool)
    for i in range(n):
        idx = (cursor + i) % n
        if pool[idx].status == "ACTIVE" and pool[idx].hash_id not in skip:
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


def http_get_json(url: str, headers: dict[str, str], timeout: float):
    """Devolve (status:int, payload:dict|None) ou levanta OSError."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = None
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return exc.code, payload


def error_detail(payload) -> str:
    detail = (payload or {}).get("detail")
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return detail["error"]
    if isinstance(detail, str):
        return detail
    return ""


def parse_usage(payload) -> dict:
    """Extrai o consumo real da resposta de GET /usage (por conta e por chave)."""
    data = payload if isinstance(payload, dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    key_info = data.get("key") if isinstance(data.get("key"), dict) else {}
    plan_limit = account.get("plan_limit")
    plan_usage = int(account.get("plan_usage") or 0)
    limit = None if plan_limit is None else int(plan_limit)
    return {
        "plan": str(account.get("current_plan") or "?"),
        "used": plan_usage,
        "limit": limit,
        "remaining": None if limit is None else max(0, limit - plan_usage),
        "key_used": int(key_info.get("usage") or 0),
        "checked_at": time.time(),
    }


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
    state: PoolState | None = None,
    max_inflight: int = DEFAULT_MAX_INFLIGHT,
    post=None,
    sleep=time.sleep,
    rng=random.random,
    now=time.time,
) -> dict:
    """
    Executa a pesquisa DONDE o chamador nunca vê falhas transitórias:
    qualquer morte de request (429/432/433/401/5xx/rede/timeout) provoca
    rotação de credencial e reemissão da MESMA request.

    `state` é o registo interno do pool: aplica cooldowns/óbitos de invocações
    anteriores, retoma o round-robin onde ficou e limita requests simultâneas
    por conta (`max_inflight`; 0 = sem teto). Contas no teto são saltadas para
    a próxima; se TODAS estiverem ocupadas, espera (até `max_wait`) e repete —
    só depois recorre à contingência keyless.
    """
    if pool is None:
        pool = load_keys()
    if state is None:
        state = PoolState.memory()
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

    state.apply(pool)
    cursor = state.start_cursor(pool)

    attempts = 0
    keyless_used = False
    waited = 0.0

    for _pass in range(MAX_PASSES):
        busy: set[str] = set()
        keyless_this_pass = False
        remaining = 0.0
        for _slot in range(4 * (len(pool) + 1) + 8):
            key, cursor = pick_key(pool, cursor, now(), busy)

            # teto de concorrência por conta: salta para a próxima credencial
            if key is not None and not state.try_acquire(key, max_inflight, now()):
                busy.add(key.hash_id)
                logv(f"conta {key.ref} no teto de concorrência ({max_inflight} por conta) — a saltar para a próxima")
                continue

            # todas as contas VIVAS ocupadas em simultâneo: espera e repete
            if key is None and any(k.status == "ACTIVE" for k in pool) and not keyless_this_pass:
                pause = CONCURRENCY_RETRY_S + rng() * COOLDOWN_JITTER_S
                if waited + pause <= max_wait:
                    logv(f"todas as contas no teto de concorrência — a aguardar {pause:.1f}s e a repetir a mesma request")
                    sleep(pause)
                    waited += pause
                    busy.clear()
                    continue
                logv("orçamento de espera esgotado com contas ocupadas — a usar contingência keyless")

            if key is None and keyless_this_pass:
                break  # passo esgotado: segue para a espera de cooldown

            attempts += 1
            headers = {"Content-Type": "application/json"}
            if key is None:
                headers["X-Tavily-Access-Mode"] = "keyless"
                keyless_used = True
                keyless_this_pass = True
                label = "keyless"
            else:
                headers["Authorization"] = "Bearer " + key.key
                key.total_requests += 1
                key.last_used_at = now()
                label = key.ref

            try:
                status, payload, retry_after = post(API_URL, headers, body, timeout)

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
                key.total_failures += 1
                key.last_error = f"HTTP {status}"
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
            except OSError as exc:
                if key is not None:
                    key.total_failures += 1
                    key.last_error = "rede/timeout"
                logv(f"falha de rede/timeout com {label}: {exc} — a rotacionar")
            finally:
                if key is not None:
                    state.release(key)
                state.snapshot(pool, cursor_hash=(key.hash_id if key is not None else None))

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

def cmd_status(check: bool, timeout: float, *, state: PoolState, reset_state: bool = False,
               max_inflight: int = DEFAULT_MAX_INFLIGHT) -> int:
    pool = load_keys()
    state.apply(pool)
    now = time.time()
    revive(pool, now)  # cooldowns já expirados aparecem como recuperados
    print(f"Pool Tavily: {len(pool)} credencial(is)" + ("" if pool else " — modo keyless puro"))
    for k in pool:
        bits = [f"req={k.total_requests}", f"err={k.total_failures}"]
        if k.last_error:
            bits.append(f"último={k.last_error}")
        if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and k.cooldown_until > now:
            bits.append(f"cooldown={int(k.cooldown_until - now)}s")
        if isinstance(k.usage, dict) and k.usage.get("limit") is not None:
            bits.append(f"restam {k.usage['remaining']}/{k.usage['limit']}")
        print(f"  {k.env_name:<20} {k.ref}  [{k.status}]  " + " · ".join(bits))
    infl = "sem teto" if max_inflight <= 0 else f"máx. {max_inflight} simultâneas/conta"
    print(f"Registo: {state.describe()}  ·  concorrência: {infl}")
    if not pool:
        print("\nNenhuma chave no terminal. Declaração (pool recomendado):")
        print('  export TAVILY_API_KEY_A="tvly-..."')
        print('  export TAVILY_API_KEY_B="tvly-..."')
        print("Sem chaves o script opera em modo keyless (limites mais severos).")
        return 0
    if reset_state:
        state.reset()
        print("\nRegisto limpo — todas as chaves voltam a ser consideradas na próxima pesquisa.")
    if check:
        print("\nVerificação ao vivo via /usage (não gasta créditos de pesquisa; limite próprio: 10 req/10 min)…")
        for k in pool:
            try:
                status, payload = http_get_json(USAGE_URL, {"Authorization": "Bearer " + k.key}, timeout)
            except OSError as exc:
                print(f"  {k.env_name:<20} {k.ref}  → sem resposta ({exc})")
                continue
            kind = classify(status)
            if kind == "success":
                k.usage = parse_usage(payload)
                k.status, k.cooldown_until, k.failures = "ACTIVE", 0.0, 0
                u = k.usage
                rest = "sem teto" if u["limit"] is None else f"restam {u['remaining']}"
                verdict = f"VÁLIDA · plano {u['plan']} · usados {u['used']}/{u['limit'] if u['limit'] is not None else '∞'} · {rest}"
            elif kind == "unauthorized":
                mark_revoked(k)
                verdict = "REVOGADA (401) — gere nova em app.tavily.com"
            elif status == 429:
                verdict = "rate limit do endpoint /usage (máx. 10 req/10 min) — repita dentro de instantes"
            else:
                verdict = f"HTTP {status}"
            print(f"  {k.env_name:<20} {k.ref}  → {verdict}")
        state.snapshot(pool, live=True)  # verificação ao vivo: estado com autoridade
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

    # ---- controlos novos: registo persistente + concorrência ----

    def s_estado_persistente_salta_chave_morta():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A", "B")
            post, _c = transport([(401, {}, None), OK])
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                       now=lambda: 0.0, state=PoolState.open(td))
            # nova invocação: pool novo, estado recarregado do disco
            pool2 = make_pool("A", "B")
            post2, calls2 = transport([OK])
            result = run_search("q", pool=pool2, post=post2, sleep=lambda _s: None, rng=lambda: 0.0,
                                now=lambda: 1.0, state=PoolState.open(td))
            assert result["meta"]["attempts"] == 1, "a chave morta não devia voltar a ser tentada"
            assert calls2[0]["headers"]["Authorization"].endswith("B"), "a 2.ª invocação devia começar já em B"

    def s_cursor_nao_recomeca_do_inicio():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A", "B")
            post, _c = transport([OK])
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                       now=lambda: 0.0, state=PoolState.open(td))  # usa A
            pool2 = make_pool("A", "B")
            post2, calls2 = transport([OK])
            run_search("q", pool=pool2, post=post2, sleep=lambda _s: None, rng=lambda: 0.0,
                       now=lambda: 1.0, state=PoolState.open(td))
            assert calls2[0]["headers"]["Authorization"].endswith("B"), \
                "a 2.ª invocação devia continuar em B, não recomeçar em A"

    def s_concorrencia_conta_ocupada_salta():
        pool = make_pool("A", "B")
        state = PoolState.memory()
        assert state.try_acquire(pool[0], 2, 0.0) and state.try_acquire(pool[0], 2, 0.0), "ocupar os 2 slots de A"
        post, calls = transport([OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                            now=lambda: 0.0, state=state, max_inflight=2)
        assert calls[0]["headers"]["Authorization"].endswith("B"), "com A no teto, a request devia sair por B"
        assert result["meta"]["attempts"] == 1

    def s_concorrencia_todas_ocupadas_espera_e_retry():
        pool = make_pool("A")
        state = PoolState.memory()
        state.try_acquire(pool[0], 1, 0.0)  # A no teto (1 por conta)
        clock = [0.0]
        sleeps: list[float] = []
        script = [OK]

        def post(_url, _headers, _body, _t):
            return script.pop(0)

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds
            state.release(pool[0])  # outro processo libertou o slot

        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0,
                            now=lambda: clock[0], state=state, max_inflight=1, max_wait=30)
        assert result["meta"]["attempts"] == 1, "o retry devia acabar em sucesso"
        assert sleeps and sleeps[0] >= CONCURRENCY_RETRY_S, "devia esperar pelo slot e repetir"

    def s_concorrencia_esgotada_vai_para_keyless():
        pool = make_pool("A")
        state = PoolState.memory()
        state.try_acquire(pool[0], 1, 0.0)
        post, calls = transport([OK])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                            now=lambda: 0.0, state=state, max_inflight=1, max_wait=0.2)
        assert result["meta"]["keyless_used"] is True
        assert calls[0]["headers"].get("X-Tavily-Access-Mode") == "keyless", \
            "sem slots e sem espera disponível, a saída é a contingência keyless"

    def s_registo_nunca_contem_segredos():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A")
            post, _c = transport([OK])
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                       state=PoolState.open(td))
            path = os.path.join(td, "pool-state.json")
            raw = open(path, encoding="utf-8").read()
            assert pool[0].key not in raw, "o registo não pode conter material de chave"
            assert pool[0].hash_id in raw, "a credencial deve estar identificada por hash"
            mode = os.stat(path).st_mode & 0o777
            assert mode == 0o600, f"permissões {oct(mode)} deviam ser 0600"

    def s_registo_merge_cross_processo():
        with tempfile.TemporaryDirectory() as td:
            # processo 1: carrega, usa A (1 request) e grava
            k1 = make_pool("A")[0]
            s1 = PoolState.open(td)
            s1.apply([k1])
            k1.total_requests += 1
            s1.snapshot([k1])
            # processo 2: tinha uma VISTA ANTIGA (carregou antes), viu um 401 e gravou
            k2 = make_pool("A")[0]
            s2 = PoolState.open(td)
            s2.apply([k2])
            k2.total_requests += 1
            mark_revoked(k2)
            s2.snapshot([k2])
            # processo 1 (vista antiga, ainda ACTIVE) tenta regravar
            k1.status = "ACTIVE"
            s1.snapshot([k1])
            final = PoolState.open(td).data["keys"][k1.hash_id]
            assert final["status"] == "REVOKED", "uma vista antiga não pode ressuscitar uma chave com 401 registado"
            assert final["total_requests"] == 2, "os contadores cross-processo não podem perder incrementos"

    def s_registo_indisponivel_nao_parte_pesquisa():
        with tempfile.TemporaryDirectory() as td:
            blocker = os.path.join(td, "bloqueio")
            with open(blocker, "w", encoding="utf-8") as handle:
                handle.write("sou um ficheiro, não uma diretoria")
            pool = make_pool("A")
            post, _c = transport([OK])
            result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                                state=PoolState.open(os.path.join(blocker, "estado")))
            assert result["meta"]["attempts"] == 1, "a pesquisa tem de funcionar com o registo indisponível"

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
        ("registo persistente: chave morta não é re-tentada na invocação seguinte", s_estado_persistente_salta_chave_morta),
        ("cursor persistido: a invocação seguinte não recomeça em A", s_cursor_nao_recomeca_do_inicio),
        ("concorrência: conta no teto é saltada para a próxima", s_concorrencia_conta_ocupada_salta),
        ("concorrência: todas ocupadas → espera e RETRY (não falha)", s_concorrencia_todas_ocupadas_espera_e_retry),
        ("concorrência: esgotada sem espera → contingência keyless", s_concorrencia_esgotada_vai_para_keyless),
        ("registo persistido sem material de chave (0600, só hashes)", s_registo_nunca_contem_segredos),
        ("merge cross-processo: sem lost updates e REVOKED absorvente", s_registo_merge_cross_processo),
        ("registo indisponível degrada em memória sem partir a pesquisa", s_registo_indisponivel_nao_parte_pesquisa),
    ]
    for name, fn in scenarios:
        scenario(name, fn)
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} cenários OK")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_max_inflight(cli_value: int | None) -> int:
    if cli_value is not None:
        return max(0, int(cli_value))
    env_value = (os.environ.get("TAVILY_MAX_INFLIGHT_PER_KEY") or "").strip()
    if env_value.isdigit():
        return max(0, int(env_value))
    return DEFAULT_MAX_INFLIGHT


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
    p_search.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT, help="espera máx. por cooldown/concorrência antes de desistir (s)")
    p_search.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="orçamento de texto na saída")
    p_search.add_argument("--max-inflight-per-key", type=int, default=None,
                          help="máx. de requests simultâneas por conta (0 = sem teto; predef. 2 ou TAVILY_MAX_INFLIGHT_PER_KEY)")
    p_search.add_argument("--no-state", action="store_true", help="não persistir o registo do pool (modo efémero)")
    p_search.add_argument("--state-dir", default=None,
                          help="diretório do registo (predef.: $TAVILY_STATE_DIR ou ~/.local/state/tavily-agent-skill)")
    p_search.add_argument("--verbose", action="store_true", help="traços de rotação em stderr")

    p_status = sub.add_parser("status", help="estado do pool de chaves (sem segredos)")
    p_status.add_argument("--check", action="store_true",
                          help="validação ao vivo via /usage (não gasta créditos de pesquisa; máx. 10 req/10 min)")
    p_status.add_argument("--reset-state", action="store_true",
                          help="limpa o registo persistente (todas as chaves voltam a ser consideradas)")
    p_status.add_argument("--state-dir", default=None, help="diretório do registo (ver search)")
    p_status.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    sub.add_parser("selftest", help="verificação determinística da máquina de rotação (offline)")

    args = parser.parse_args(argv)

    try:
        if args.command == "selftest":
            return cmd_selftest()
        if args.command == "status":
            state = PoolState.open(args.state_dir)
            return cmd_status(args.check, args.timeout, state=state,
                              reset_state=args.reset_state,
                              max_inflight=resolve_max_inflight(None))

        VERBOSITY = bool(args.verbose)
        pool = load_keys()
        if not pool:
            print(
                "Aviso: nenhuma chave Tavily no terminal — a usar contingência keyless (limites mais severos). "
                'Declare o pool (export TAVILY_API_KEY_A="tvly-…") para limites normais.',
                file=sys.stderr,
            )
        state = PoolState.memory() if args.no_state else PoolState.open(args.state_dir)
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
            state=state,
            max_inflight=resolve_max_inflight(args.max_inflight_per_key),
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
