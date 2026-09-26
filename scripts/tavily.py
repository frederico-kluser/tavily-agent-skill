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
import contextlib
import datetime
import email.utils
import hashlib
import http.client
import io
import json
import math
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
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
RETRY_AFTER_MAX_S = 3600.0  # Retry-After absurdo não pode "matar" uma chave por anos
CONCURRENCY_RETRY_S = 0.5
INFLIGHT_TTL_S = 120.0          # validade mínima de um slot in-flight (processo morto não o prende)
MAX_PASSES = 6
TRANSIENT_STRIKES = 2  # falhas transitórias (5xx/rede) por credencial — e keyless — por invocação
STATE_VERSION = 1
USAGE_STALE_S = 3600.0         # saldo registado com mais de 60 min → refrescar no `status`
USAGE_MIN_INTERVAL_S = 360.0   # /usage: 10 req/10 min → no máx. 1 consulta automática a cada 6 min
AUTO_REFRESH_TIMEOUT_S = 5.0   # o refrescamento automático nunca pendura o `status`
CLOCK_SKEW_S = 300.0           # carimbos mais no futuro do que isto são tratados como inválidos
DEPTH_CREDITS = {"advanced": 2}  # restantes profundidades: 1 crédito (docs.tavily.com, 2026-09)
MAX_TIMEOUT_S = 600.0
# slot com expiração mais distante do que isto = relógio avariado (folga acima do maior TTL possível)
INFLIGHT_MAX_HORIZON_S = 2 * MAX_TIMEOUT_S + 10.0 + CLOCK_SKEW_S
MAX_WAIT_S = 86400.0
MAX_BYTES_CAP = 10_000_000
MAX_INFLIGHT_CAP = 1000

VERBOSITY = False
_SECRETS: set[str] = set()  # valores a redigir em QUALQUER saída (incluindo --verbose e erros)


def logv(message: str) -> None:
    """Diagnóstico de rotação em stderr — silencioso por omissão (ver --verbose)."""
    if VERBOSITY:
        print(redact(f"[tavily] {message}", _SECRETS), file=sys.stderr)


def _as_float(value, default: float = 0.0) -> float:
    """Número finito tolerante (registo/payload corrompido nunca rebenta)."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _as_int(value, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _as_number(value):
    """int/float finito, ou None quando ausente/inválido (sem converter lixo em 0)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number == int(number) else number


def _utc_month(ts) -> tuple[int, int] | None:
    try:
        t = time.gmtime(ts)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    return t.tm_year, t.tm_mon


def _clean_text(value) -> str:
    """Texto sempre codificável: surrogates isolados (JSON malformado) → '?'."""
    return str(value).encode("utf-8", "replace").decode("utf-8")


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
    credits_spent: int = 0          # créditos gastos (usage.credits das respostas), cumulativo
    usage_queried_at: float = 0.0   # última consulta a /usage (sucesso OU falha) — anti-rajada
    aliases: list[str] = field(default_factory=list)  # outras variáveis com o MESMO valor
    # base contabilística para merge cross-processo (delta = atual − base)
    base_requests: int = 0
    base_failures: int = 0
    base_credits: int = 0
    # o snapshot só AFIRMA o que este processo observou (nunca reescreve a vista carregada)
    dirty: bool = False        # estado/cooldown/falhas/último erro observados agora
    usage_dirty: bool = False  # saldo consultado agora via /usage
    consec_base: int = 0       # falhas consecutivas carregadas (o registo soma só o delta)
    failures_reset: bool = False  # este processo viu um sucesso: as falhas recomeçam em 0
    error_observed: bool = False  # último erro visto por ESTE processo (não herdado)

    @property
    def ref(self) -> str:
        """Identificação sem material de segredo (apenas os últimos 4)."""
        return "…" + (self.key[-4:] if len(self.key) >= 4 else "····")

    @property
    def hash_id(self) -> str:
        """Identidade estável do registo — derivada da chave, nunca a revela."""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]


MIN_KEY_LEN = 8


def valid_key_format(value: str) -> bool:
    """Formato aceitável para o cabeçalho Authorization: ASCII imprimível, sem
    espaços, aspas nem barra invertida, com pelo menos 8 caracteres. Uma chave
    colada com quebra de linha, aspas tipográficas ou "Bearer " rebentaria no
    http.client (fora do tratamento de rede) — e o repr dela escaparia à redação."""
    return len(value) >= MIN_KEY_LEN and all(33 <= ord(c) <= 126 and c not in "\"'\\" for c in value)


def _key_vars(env) -> list[str]:
    names = sorted(k for k in env if k.startswith("TAVILY_API_KEY_"))
    return (["TAVILY_API_KEY"] if "TAVILY_API_KEY" in env else []) + names


def load_keys(env: dict[str, str] | None = None) -> list[KeyMeta]:
    """Lê o pool a partir do terminal, em ordem determinística (Pass^k-friendly).

    Deduplica por VALOR: variáveis com a mesma chave são uma só conta. O nome
    canónico é a variável do agrupamento (`TAVILY_API_KEY_A`…), por ordem
    alfabética; o alias `TAVILY_API_KEY` fica registado em `aliases`. Uma
    `TAVILY_API_KEY` com valor próprio continua a ser a primeira do pool.
    Valores com formato inválido ficam de fora (ver `invalid_key_vars`).
    """
    env = os.environ if env is None else env
    pool: list[KeyMeta] = []
    by_value: dict[str, KeyMeta] = {}
    for name in sorted(k for k in env if k.startswith("TAVILY_API_KEY_")):
        value = (env.get(name) or "").strip()
        if not value or not valid_key_format(value):
            continue
        if value in by_value:
            by_value[value].aliases.append(name)
            continue
        by_value[value] = KeyMeta(name, value)
        pool.append(by_value[value])
    single = (env.get("TAVILY_API_KEY") or "").strip()
    if single and valid_key_format(single):
        if single in by_value:
            by_value[single].aliases.append("TAVILY_API_KEY")
        else:
            pool.insert(0, KeyMeta("TAVILY_API_KEY", single))
    return pool


def invalid_key_vars(env: dict[str, str] | None = None) -> list[str]:
    """Variáveis de chave definidas mas com formato inválido (nunca os valores)."""
    env = os.environ if env is None else env
    return [name for name in _key_vars(env)
            if (env.get(name) or "").strip() and not valid_key_format((env.get(name) or "").strip())]


def secret_values(env: dict[str, str] | None = None) -> set[str]:
    """Todos os valores a redigir: cada variável de chave (válida ou não) e cada
    segmento seu com ≥ 8 caracteres (uma chave com quebra de linha aparece em
    pedaços num repr)."""
    env = os.environ if env is None else env
    out: set[str] = set()
    for name in _key_vars(env):
        value = (env.get(name) or "").strip()
        if len(value) >= 4:
            out.add(value)
        out.update(part for part in value.split() if len(part) >= MIN_KEY_LEN)
    return out


def remember_secrets(values) -> None:
    for v in values:
        s = v.key if isinstance(v, KeyMeta) else str(v)
        if len(s) >= 4:
            _SECRETS.add(s)


def redact(text: str, secrets) -> str:
    """Canário de segredos por valor: material de chave nunca sai em claro.
    `secrets` = KeyMeta e/ou strings; as mais longas primeiro (uma chave que é
    prefixo de outra não deixa escapar o resto)."""
    values = {s.key if isinstance(s, KeyMeta) else str(s) for s in secrets}
    out = text
    for value in sorted((v for v in values if len(v) >= 4), key=len, reverse=True):
        if value in out:
            out = out.replace(value, "[REDACTED]")
    return out


_KEY_LIKE = re.compile(r"tvly-[A-Za-z0-9._~+/=-]{6,}")


def redact_all(text: str) -> str:
    """Redação máxima para caminhos de erro: segredos conhecidos (processo +
    ambiente) e qualquer token com forma de chave Tavily."""
    return _KEY_LIKE.sub("[REDACTED]", redact(text, _SECRETS | secret_values()))


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


def _usage_checked_at(usage, now: float) -> float:
    """`checked_at` válido de um registo de consumo (-1 = ausente/no futuro)."""
    if not isinstance(usage, dict):
        return -1.0
    checked = _as_float(usage.get("checked_at"), -1.0)
    return -1.0 if checked > now + CLOCK_SKEW_S else checked


def _merge_entry(existing: dict | None, k: KeyMeta, *, now: float, authoritative: bool = False) -> dict:
    """Merge cross-processo do registo de UMA chave (sem lost updates):

    * o processo só AFIRMA o que observou: estado/cooldown/falhas/último erro
      só entram se `k.dirty` (senão mantém-se o que está no disco — uma vista
      carregada antes de um `--reset-state`/`--check` nunca é reescrita);
    * estado observado: vence a maior severidade (REVOKED é absorvente), salvo
      `authoritative` (a chave acabou de ser verificada ao vivo);
    * contadores (requests, falhas, créditos): disco + delta desta invocação;
    * consumo (/usage): vence a consulta mais recente (`checked_at`), e o
      ponto de partida da estimativa é o total de créditos já no disco.
    """
    old = existing if isinstance(existing, dict) else {}
    merged = {
        "ref": k.ref,
        "status": old["status"] if isinstance(old.get("status"), str) and old["status"] in STATUS_SEVERITY
        else "ACTIVE",
        "cooldown_until": _as_float(old.get("cooldown_until")),
        "failures": _as_int(old.get("failures")),
        "last_error": str(old.get("last_error") or ""),
    }
    if k.dirty:
        if authoritative or STATUS_SEVERITY.get(k.status, 0) >= STATUS_SEVERITY.get(merged["status"], 0):
            merged["status"] = k.status
        merged["cooldown_until"] = (k.cooldown_until if authoritative
                                    else max(merged["cooldown_until"], k.cooldown_until))
        if k.failures_reset:  # sucesso observado aqui: só contam as falhas posteriores
            merged["failures"] = k.failures
        else:                 # só as falhas observadas por este processo (sobrevive a um reset)
            merged["failures"] += max(0, k.failures - k.consec_base)
        if k.error_observed:
            merged["last_error"] = k.last_error or merged["last_error"]
    delta_requests = max(0, k.total_requests - k.base_requests)
    merged["total_requests"] = _as_int(old.get("total_requests")) + delta_requests
    merged["total_failures"] = _as_int(old.get("total_failures")) + max(0, k.total_failures - k.base_failures)
    merged["credits_spent"] = _as_int(old.get("credits_spent")) + max(0, k.credits_spent - k.base_credits)
    merged["last_used_at"] = max(_as_float(old.get("last_used_at")), k.last_used_at if delta_requests else 0.0)
    merged["usage_queried_at"] = max(_as_float(old.get("usage_queried_at")), k.usage_queried_at)
    usage = old.get("usage") if _usage_checked_at(old.get("usage"), now) >= 0 else None
    if k.usage_dirty and isinstance(k.usage, dict) and \
            _usage_checked_at(k.usage, now) > _usage_checked_at(usage, now):
        usage = dict(k.usage)  # spent_at_check foi amostrado antes do GET (ver sync_credits)
    merged["usage"] = usage
    merged["updated_at"] = now
    return merged


_ENTRY_NUMBERS = ("cooldown_until", "failures", "total_requests", "total_failures", "credits_spent",
                  "last_used_at", "usage_queried_at", "updated_at")
_USAGE_NUMBERS = ("used", "limit", "remaining", "key_used", "key_limit", "checked_at", "spent_at_check")


def _empty_state() -> dict:
    return {"version": STATE_VERSION, "cursor": None, "keys": {}, "inflight": {}}


def _sanitize_usage(usage) -> dict | None:
    if not isinstance(usage, dict):
        return None
    out = {name: _as_number(usage.get(name)) for name in _USAGE_NUMBERS}
    plan = usage.get("plan")
    out["plan"] = _clean_text(plan)[:64] if isinstance(plan, str) else "?"
    return out


def _sanitize_entry(entry) -> dict | None:
    """Entrada do registo reduzida aos campos conhecidos, com tipos escalares e
    profundidade limitada — um ficheiro corrompido (ou aninhado ao absurdo)
    nunca chega ao resto do código."""
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    out: dict = {"status": status if isinstance(status, str) and status in STATUS_SEVERITY else "ACTIVE"}
    for name in _ENTRY_NUMBERS:
        value = _as_number(entry.get(name))
        if value is not None:
            out[name] = value
    for name in ("ref", "last_error"):
        if isinstance(entry.get(name), str):
            out[name] = _clean_text(entry[name])[:200]
    usage = _sanitize_usage(entry.get("usage"))
    if usage is not None:
        out["usage"] = usage
    return out


def _sanitize_state(data) -> dict:
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return _empty_state()
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    inflight = data.get("inflight") if isinstance(data.get("inflight"), dict) else {}
    clean_keys = {}
    for key_id, entry in keys.items():
        sane = _sanitize_entry(entry)
        if sane is not None:
            clean_keys[str(key_id)] = sane
    clean_inflight = {}
    for key_id, slots in inflight.items():
        if isinstance(slots, list):
            clean_inflight[str(key_id)] = [s for s in (_as_number(v) for v in slots) if s is not None]
    cursor = data.get("cursor")
    return {"version": STATE_VERSION, "cursor": cursor if isinstance(cursor, str) else None,
            "keys": clean_keys, "inflight": clean_inflight}


def _align_credits(key: KeyMeta, disk_credits: int) -> None:
    """Põe a conta na escala de créditos do disco, preservando o delta próprio."""
    own = key.credits_spent - key.base_credits
    key.base_credits = disk_credits
    key.credits_spent = disk_credits + own


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

    def _empty(self) -> dict:
        return _empty_state()

    def _load(self) -> None:
        """Espelha o disco em `self.data`. Ficheiro ausente/corrompido → registo
        vazio (nunca a cópia em memória: apagar o ficheiro limpa mesmo o registo)."""
        if not self.dirpath:
            return
        try:
            with open(self._file, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.data = self._empty()
            return
        except (OSError, ValueError, RecursionError):
            self.data = self._empty()  # ilegível/corrompido/aninhado demais → começa limpo
            return
        self.data = _sanitize_state(data)

    def _save(self) -> None:
        if not self.dirpath:
            return
        try:
            os.makedirs(self.dirpath, mode=0o700, exist_ok=True)
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=True, indent=2)
                handle.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._file)
        except (OSError, ValueError, RecursionError) as exc:
            # persistência é um BÓNUS: nunca pode partir uma pesquisa
            self.degraded = True
            self.dirpath = None
            logv(f"registo do pool indisponível ({type(exc).__name__}) — a continuar em modo memória")

    @property
    def persistent(self) -> bool:
        return bool(self.dirpath) and not self.degraded

    # -- sincronização com o pool ------------------------------------------
    def apply(self, pool: list[KeyMeta]) -> None:
        """Restaura o estado registado — chaves mortas/sem cota não são re-tentadas.
        Valores corrompidos no ficheiro são tratados como ausentes (nunca rebentam)."""
        entries = self.data.get("keys") or {}
        for k in pool:
            k.dirty = k.usage_dirty = k.failures_reset = k.error_observed = False
            k.consec_base = k.failures
            entry = entries.get(k.hash_id)
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if isinstance(status, str) and status in STATUS_SEVERITY:
                k.status = status
            k.cooldown_until = _as_float(entry.get("cooldown_until"))
            k.failures = _as_int(entry.get("failures"))
            k.total_requests = _as_int(entry.get("total_requests"))
            k.total_failures = _as_int(entry.get("total_failures"))
            k.credits_spent = _as_int(entry.get("credits_spent"))
            k.last_error = _clean_text(entry.get("last_error") or "")
            k.last_used_at = _as_float(entry.get("last_used_at"))
            k.usage_queried_at = _as_float(entry.get("usage_queried_at"))
            if isinstance(entry.get("usage"), dict):
                k.usage = entry["usage"]
            k.base_requests, k.base_failures = k.total_requests, k.total_failures
            k.base_credits = k.credits_spent
            k.consec_base = k.failures

    def start_cursor(self, pool: list[KeyMeta]) -> int:
        """Retoma o round-robin DEPOIS da última chave usada (nunca sempre do início)."""
        cursor_id = self.data.get("cursor")
        if not cursor_id or not pool:
            return 0
        for i, k in enumerate(pool):
            if k.hash_id == cursor_id:
                return (i + 1) % len(pool)
        return 0

    def snapshot(self, pool: list[KeyMeta], cursor_hash: str | None = None,
                 authoritative: frozenset | set = frozenset(), now: float | None = None) -> None:
        """Regista as OBSERVAÇÕES deste processo com merge cross-processo (ver
        `_merge_entry`): nunca perde observações alheias nem reescreve a vista
        carregada. Chaves em `authoritative` (verificadas agora ao vivo) impõem
        estado/cooldown com autoridade. Só grava chaves tocadas ou já registadas."""
        with self._lock():
            self._load()
            entries = self.data.setdefault("keys", {})
            now = time.time() if now is None else now
            for k in pool:
                existing = entries.get(k.hash_id)
                touched = (k.dirty or k.usage_dirty or k.usage_queried_at > 0
                           or k.total_requests != k.base_requests or k.credits_spent != k.base_credits)
                if existing is None and not touched:
                    continue
                entries[k.hash_id] = _merge_entry(existing, k, now=now,
                                                  authoritative=k.hash_id in authoritative)
                k.base_requests, k.base_failures = k.total_requests, k.total_failures
                k.base_credits = k.credits_spent
                k.failures = k.consec_base = _as_int(entries[k.hash_id].get("failures"))
                k.dirty = k.usage_dirty = k.failures_reset = k.error_observed = False
            if cursor_hash:
                self.data["cursor"] = cursor_hash
            self._save()

    def reset(self) -> None:
        """Limpa o registo (recuperação: volta a considerar todas as chaves)."""
        self.data = self._empty()
        with self._lock():
            self._save()

    def claim_usage_query(self, key: KeyMeta, now: float, min_interval: float = USAGE_MIN_INTERVAL_S) -> bool:
        """Reserva ATÓMICA (sob flock) de uma consulta automática a /usage para a
        conta: recusa se outro processo já refrescou o saldo ou consultou há
        menos de `min_interval`. Sem registo persistente não há reserva possível
        → recusa (sem persistência não haveria anti-rajada entre invocações)."""
        if not self.persistent:
            return False
        with self._lock():
            self._load()
            entries = self.data.setdefault("keys", {})
            entry = entries.get(key.hash_id) if isinstance(entries.get(key.hash_id), dict) else None
            if entry is not None and isinstance(entry.get("usage"), dict) \
                    and not _usage_stale(entry["usage"], now):
                key.usage = entry["usage"]  # outro processo acabou de o refrescar
                return False
            queried = _as_float(entry.get("usage_queried_at")) if entry else 0.0
            if queried <= now + CLOCK_SKEW_S and now - queried < min_interval:
                return False
            if entry is None:
                entry = entries[key.hash_id] = {"ref": key.ref, "status": "ACTIVE"}
            entry["usage_queried_at"] = now
            self._save()
            disk_credits = _as_int(entry.get("credits_spent"))
        if not self.persistent:
            return False  # a reserva não ficou gravada: não arrisca uma rajada
        key.usage_queried_at = now
        _align_credits(key, disk_credits)
        return True

    def sync_credits(self, key: KeyMeta) -> None:
        """Alinha `credits_spent` da conta com o disco IMEDIATAMENTE antes de uma
        consulta a /usage: o ponto de partida da estimativa (`spent_at_check`)
        fica na escala partilhada entre processos. Créditos gastos por outros
        durante o próprio GET contam a dobrar — erro conservador (subestima)."""
        if not self.dirpath:
            return
        with self._lock():
            self._load()
            entry = self.data.get("keys", {}).get(key.hash_id)
            disk_credits = _as_int(entry.get("credits_spent")) if isinstance(entry, dict) else 0
        _align_credits(key, disk_credits)

    # -- concorrência: slots in-flight por conta ---------------------------
    # Cada slot guarda a sua EXPIRAÇÃO (não o início): um pedido longo
    # (--timeout alto) não perde o slot a meio, e um processo morto só o prende
    # até expirar. Expirações absurdamente no futuro (relógio avariado) caem.
    def _prune_inflight(self, now: float) -> None:
        inflight = self.data.setdefault("inflight", {})
        for key_id in list(inflight):
            slots = inflight[key_id] if isinstance(inflight[key_id], list) else []
            fresh = [exp for exp in slots
                     if _as_number(exp) is not None and now < _as_float(exp) <= now + INFLIGHT_MAX_HORIZON_S]
            if fresh:
                inflight[key_id] = fresh
            else:
                del inflight[key_id]

    def try_acquire(self, key: KeyMeta, limit: int, now: float, ttl: float = INFLIGHT_TTL_S) -> bool:
        """Reserva um slot in-flight para a conta, válido até `now + ttl` (esse
        valor é o token a passar a `release`). limit<=0 → sem teto e SEM slot:
        nesse caso não há nada a libertar depois."""
        if limit <= 0:
            return True
        with self._lock():
            self._load()
            self._prune_inflight(now)
            slots = self.data.setdefault("inflight", {}).setdefault(key.hash_id, [])
            if len(slots) >= limit:
                self._save()
                return False
            slots.append(now + ttl)
            self._save()
            return True

    def release(self, key: KeyMeta, token: float | None = None) -> None:
        """Liberta o slot in-flight da conta: o reservado com `token` (o
        próprio — nunca o de outro processo) ou, sem ele, o mais antigo."""
        with self._lock():
            self._load()
            slots = self.data.get("inflight", {}).get(key.hash_id)
            if not isinstance(slots, list) or not slots:
                return
            if token is None:
                slots.pop(0)
            elif token in slots:
                slots.remove(token)
            else:
                return  # já expirou: não há slot próprio a libertar
            if not slots:
                del self.data["inflight"][key.hash_id]  # conta sem pedidos em voo: sem entrada
            self._save()


# --------------------------------------------------------------------------
# máquina de estados do pool
# --------------------------------------------------------------------------

def revive(pool: list[KeyMeta], now: float) -> None:
    """Recuperação lazy: cooldowns expirados voltam a ACTIVE (REVOKED nunca).
    As falhas consecutivas mantêm-se — só um sucesso as zera — para que o
    backoff exponencial de 429 sem Retry-After escale de facto."""
    for k in pool:
        if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and now >= k.cooldown_until:
            k.status, k.cooldown_until = "ACTIVE", 0.0


def _usage_stale(usage, now: float) -> bool:
    checked = _as_number(usage.get("checked_at")) if isinstance(usage, dict) else None
    return checked is None or now - checked > USAGE_STALE_S or checked > now + CLOCK_SKEW_S


def effective_remaining(k: KeyMeta, now: float | None = None):
    """Saldo estimado da conta: `remaining` da última consulta a /usage menos os
    créditos gastos desde então (contados pelas respostas). None = desconhecido
    — também quando a consulta é de um mês UTC anterior (as cotas repõem no
    dia 1) ou tem carimbo no futuro."""
    usage = k.usage if isinstance(k.usage, dict) else None
    remaining = _as_number(usage.get("remaining")) if usage else None
    if remaining is None:
        return None
    if now is not None:
        checked = _as_number(usage.get("checked_at"))
        if checked is None or checked > now + CLOCK_SKEW_S:
            return None
        month = _utc_month(checked)
        if month is None or month != _utc_month(now):
            return None
    spent_since = max(0, k.credits_spent - _as_int(usage.get("spent_at_check")))
    return max(0, remaining - spent_since)


def pick_key(pool: list[KeyMeta], cursor: int, now: float, skip: frozenset | set = frozenset()) -> tuple[KeyMeta | None, int]:
    """Escolhe a próxima credencial ACTIVE (fora de `skip`); None → contingência keyless.

    Orientada ao saldo: uma conta cujo saldo estimado é conhecido e inferior
    ao MELHOR saldo conhecido (ou nulo) fica para trás. Entre as restantes —
    saldo igual ao melhor ou desconhecido — mantém-se o round-robin a partir do
    cursor. Sem nenhum saldo conhecido é exatamente o round-robin clássico.
    """
    revive(pool, now)
    n = len(pool)
    candidates = [idx for idx in ((cursor + i) % n for i in range(n))
                  if pool[idx].status == "ACTIVE" and pool[idx].hash_id not in skip]
    if not candidates:
        return None, cursor
    balance = {idx: effective_remaining(pool[idx], now) for idx in candidates}
    known = [b for b in balance.values() if b is not None]
    best = max(known) if known else None
    for idx in candidates:
        b = balance[idx]
        if b is None or (b >= best and b > 0):
            return pool[idx], idx + 1
    idx = candidates[0]  # todas com saldo estimado nulo: a estimativa pode falhar (PAYGO)
    return pool[idx], idx + 1


def mark_rate_limited(k: KeyMeta, retry_after_s: float | None, now: float, rng) -> None:
    """429: arrefece a chave. Retry-After manda (com piso de 0,5 s — um
    `Retry-After: 0` não pode gerar uma rajada — e teto de 1 h); sem ele,
    backoff exponencial pelas falhas consecutivas + jitter."""
    k.status = "RATE_LIMITED"
    k.failures += 1
    k.dirty = True
    if retry_after_s is not None:
        delay = min(max(COOLDOWN_BASE_S, retry_after_s), RETRY_AFTER_MAX_S)
    else:
        delay = min(COOLDOWN_MAX_S, COOLDOWN_BASE_S * (2 ** k.failures)) + rng() * COOLDOWN_JITTER_S
    k.cooldown_until = now + delay


def mark_quota_exhausted(k: KeyMeta, now: float) -> None:
    """432/433: a Tavily repõe cotas no 1.º dia do mês civil seguinte (UTC)."""
    k.status = "QUOTA_EXHAUSTED"
    k.failures += 1
    k.dirty = True
    t = time.gmtime(now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    k.cooldown_until = float(calendar.timegm((year, month, 1, 0, 0, 0)))


def mark_revoked(k: KeyMeta) -> None:
    k.status = "REVOKED"
    k.dirty = True


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
    """Retry-After em segundos: delta inteiro ou data HTTP (RFC 9110, ex.
    "Wed, 21 Oct 2026 07:28:00 GMT"). Independente do locale; lixo → None."""
    if not value:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # "-0000" = UTC sem zona declarada
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, when.timestamp() - now)


def _read_json(stream):
    raw = stream.read().decode("utf-8", "replace")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _transport_error(exc: Exception) -> OSError:
    """Falhas do transporte que não são OSError viram falha de rede (rotação).
    NUNCA carrega a mensagem original: um cabeçalho inválido mostra o seu
    valor (a chave) no repr."""
    if isinstance(exc, http.client.HTTPException):
        return OSError(f"resposta HTTP inválida ({type(exc).__name__})")
    return OSError(f"pedido HTTP inválido para esta credencial ({type(exc).__name__})")


def http_post_json(url: str, headers: dict[str, str], body: dict, timeout: float):
    """Devolve (status:int, payload:dict|None, retry_after:str|None) ou levanta OSError
    (respostas malformadas/truncadas e pedidos inválidos também contam como falha)."""
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, _read_json(response), response.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            return exc.code, _read_json(exc), exc.headers.get("Retry-After") if exc.headers else None
    except (http.client.HTTPException, ValueError) as exc:  # UnicodeError ⊂ ValueError
        raise _transport_error(exc) from None


def http_get_json(url: str, headers: dict[str, str], timeout: float):
    """Devolve (status:int, payload:dict|None) ou levanta OSError."""
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return response.status, _read_json(response)
        except urllib.error.HTTPError as exc:
            return exc.code, _read_json(exc)
    except (http.client.HTTPException, ValueError) as exc:
        raise _transport_error(exc) from None


def error_detail(payload) -> str:
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("error"), str):
        return detail["error"]
    if isinstance(detail, str):
        return detail
    return ""


def parse_usage(payload, now: float | None = None) -> dict:
    """Extrai o consumo real da resposta de GET /usage (por conta e por chave).

    `remaining` = o menor saldo conhecido entre o plano da conta
    (plan_limit − plan_usage) e o teto próprio da chave (key.limit − key.usage);
    None quando nenhum teto é conhecido (ex.: plan_limit nulo). Tolerante a
    campos ausentes/inválidos — nunca levanta exceção.
    """
    data = payload if isinstance(payload, dict) else {}
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    key_info = data.get("key") if isinstance(data.get("key"), dict) else {}
    used = _as_number(account.get("plan_usage")) or 0
    limit = _as_number(account.get("plan_limit"))
    key_used = _as_number(key_info.get("usage")) or 0
    key_limit = _as_number(key_info.get("limit"))
    balances = [max(0, lim - use) for lim, use in ((limit, used), (key_limit, key_used)) if lim is not None]
    return {
        "plan": str(account.get("current_plan") or "?"),
        "used": used,
        "limit": limit,
        "remaining": min(balances) if balances else None,
        "key_used": key_used,
        "key_limit": key_limit,
        "checked_at": time.time() if now is None else now,
    }


def refresh_usage(k: KeyMeta, *, get, timeout: float, now: float) -> tuple[str, int]:
    """UMA consulta a GET /usage para a conta `k` (não gasta créditos de pesquisa).

    Regista sempre `usage_queried_at` (mesmo em falha — é o que impede rajadas
    contra o limite de 10 req/10 min); em sucesso grava o saldo e o ponto de
    partida da estimativa (`spent_at_check`); 401 → REVOKED. Devolve
    (classificação, HTTP status); falhas de rede levantam OSError.
    """
    k.usage_queried_at = now
    status, payload = get(USAGE_URL, {"Authorization": "Bearer " + k.key}, timeout)
    kind = classify(status)
    if kind == "success" and not (isinstance(payload, dict) and (
            isinstance(payload.get("account"), dict) or isinstance(payload.get("key"), dict))):
        kind = "invalid-usage"  # 200 de um proxy/portal cativo: não apaga o saldo conhecido
    if kind == "success":
        k.usage = parse_usage(payload, now=now)
        k.usage["spent_at_check"] = k.credits_spent
        k.usage_dirty = True
    elif kind == "unauthorized":
        mark_revoked(k)
    return kind, status


def usage_is_stale(k: KeyMeta, now: float) -> bool:
    return _usage_stale(k.usage, now)


def auto_refresh_usage(pool: list[KeyMeta], *, get, now: float, timeout: float,
                       claim=None) -> list[tuple[KeyMeta, str]]:
    """Refresca o saldo das contas com registo de /usage com mais de 60 min,
    SEM rajadas: no máx. 1 consulta por conta a cada 6 min (sucesso ou falha),
    nunca em contas REVOKED, e pára à primeira falha de rede (não soma
    timeouts). `claim(k, now)` reserva a consulta atomicamente entre processos
    (PoolState.claim_usage_query); sem ele usa só o carimbo em memória.
    Cada consulta tem um prazo TOTAL (timeout × 1,5 + 0,2 s), DNS incluído.
    Devolve [(conta, resultado)] com resultado "ok", "HTTP <n>",
    "resposta inválida" (2xx sem dados de consumo) ou "sem rede"."""
    outcomes: list[tuple[KeyMeta, str]] = []
    deadline = timeout * 1.5 + 0.2

    def bounded_get(url, headers, t):
        return _call_with_deadline(get, (url, headers, t), deadline)

    for k in pool:
        if k.status == "REVOKED" or not usage_is_stale(k, now):
            continue
        if claim is not None:
            if not claim(k, now):
                continue
        elif 0 <= now - k.usage_queried_at < USAGE_MIN_INTERVAL_S:
            continue
        try:
            kind, status = refresh_usage(k, get=bounded_get, timeout=timeout, now=now)
        except OSError as exc:
            logv(f"refrescamento automático de /usage falhou para {k.ref}: {exc}")
            outcomes.append((k, "sem rede"))
            break
        if kind == "success":
            outcomes.append((k, "ok"))
        elif kind == "invalid-usage":
            outcomes.append((k, "resposta inválida"))
        else:
            outcomes.append((k, f"HTTP {status}"))
    return outcomes


def _call_with_deadline(fn, args: tuple, deadline: float):
    """Corre `fn(*args)` numa thread daemon e desiste ao fim de `deadline` s —
    cobre o que o timeout do socket não cobre (DNS lento, vários endereços
    ligados em série). A thread abandonada morre com o processo."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as exc:  # noqa: BLE001 — reenviada ao chamador
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise OSError(f"sem resposta em {deadline:.1f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


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
    remaining = max(0, int(max_bytes))
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
    keyless: bool = True,
    max_attempts: int | None = None,
    post=None,
    sleep=time.sleep,
    rng=random.random,
    now=time.time,
) -> dict:
    """
    Executa a pesquisa DONDE o chamador nunca vê falhas transitórias:
    qualquer morte de request (429/432/433/401/5xx/rede/timeout) provoca
    rotação de credencial e reemissão da MESMA request.

    Por passagem, cada conta é tentada no máx. uma vez (orientada ao saldo,
    em round-robin a partir do cursor). Contas no teto de concorrência
    (`max_inflight`; 0 = sem teto) são saltadas; se TODAS as que faltam
    estiverem ocupadas, espera e repete — até `max_wait`, contado em todas as
    esperas. Uma falha transitória (5xx/rede) dá à conta uma 2.ª ronda após um
    backoff curto; ao fim de TRANSIENT_STRIKES falhas transitórias a conta (e o
    keyless) sai desta invocação. Esgotadas as contas, a contingência keyless
    (`keyless=False` desliga) é tentada uma vez por passagem; depois espera
    pelo cooldown mais curto e faz nova passagem (máx. MAX_PASSES).
    `max_attempts` limita o total de pedidos (ex.: selftest --live).
    """
    if pool is None:
        pool = load_keys()
    if state is None:
        state = PoolState.memory()
    if post is None:
        post = http_post_json
    remember_secrets(pool)

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
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        raise SkillError(
            "a consulta contém bytes que não são texto UTF-8 válido.",
            "reescreva a consulta como texto normal (sem bytes binários) e repita.",
        ) from None

    body = {
        "query": query,
        "search_depth": depth,
        "max_results": max(1, min(int(max_results), MAX_RESULTS_CAP)),
        "topic": topic,
        "include_answer": bool(include_answer),
        "chunks_per_source": 3,
        "include_usage": True,  # créditos reais gastos → estimativa de saldo por conta
    }

    state.apply(pool)
    cursor = state.start_cursor(pool)
    slot_ttl = max(INFLIGHT_TTL_S, 2.0 * timeout + 10.0)

    attempts = 0
    keyless_attempts = 0
    waited = 0.0
    remaining = 0.0
    strikes: dict[str, int] = {}  # hash_id | "keyless" → falhas transitórias nesta invocação

    def benched(cred_id: str) -> bool:
        return strikes.get(cred_id, 0) >= TRANSIENT_STRIKES

    def budget_left() -> bool:
        return max_attempts is None or attempts < max_attempts

    # travão de segurança do ciclo interno: cada iteração envia um pedido, dorme
    # (≥ 0,5 s, limitado por max_wait) ou marca uma conta ocupada (≤ n entre
    # esperas) — este teto só é atingível por um bug, nunca por espera legítima
    min_pause = min(CONCURRENCY_RETRY_S, COOLDOWN_BASE_S)
    step_cap = int((2 * max_wait / min_pause + len(pool) + 2) * (len(pool) + 1)) + len(pool) + 16

    for pass_no in range(MAX_PASSES):
        busy: set[str] = set()   # no teto de concorrência (limpo após cada espera)
        tried: set[str] = set()  # já falharam nesta passagem (qualquer motivo)
        keyless_this_pass = False
        for _step in range(step_cap):
            if not budget_left():
                break
            out = {k.hash_id for k in pool if benched(k.hash_id)}
            key, cursor = pick_key(pool, cursor, now(), busy | tried | out)

            if key is None:
                waiting = [k for k in pool if k.status == "ACTIVE" and k.hash_id not in tried | out]
                retryable = [k for k in pool if k.status == "ACTIVE" and k.hash_id in tried
                             and 0 < strikes.get(k.hash_id, 0) < TRANSIENT_STRIKES]
                if waiting:
                    # as contas que faltam estão todas no teto de concorrência: espera e repete
                    pause = CONCURRENCY_RETRY_S + rng() * COOLDOWN_JITTER_S
                    if waited + pause <= max_wait:
                        logv(f"todas as contas no teto de concorrência — a aguardar {pause:.1f}s e a repetir a mesma request")
                        sleep(pause)
                        waited += pause
                        busy.clear()
                        continue
                    logv("orçamento de espera esgotado com contas ocupadas — a usar contingência keyless")
                elif retryable:
                    # falha transitória isolada: 2.ª ronda para essas contas, após backoff curto
                    pause = COOLDOWN_BASE_S + rng() * COOLDOWN_JITTER_S
                    if waited + pause <= max_wait:
                        logv(f"falha transitória — a aguardar {pause:.1f}s e a repetir a mesma request")
                        sleep(pause)
                        waited += pause
                        tried.difference_update(k.hash_id for k in retryable)
                        continue
                if not keyless or keyless_this_pass or benched("keyless"):
                    break  # passagem esgotada: segue para a espera de cooldown

            token = None
            if key is not None:
                acquired_at = now()
                if not state.try_acquire(key, max_inflight, acquired_at, ttl=slot_ttl):
                    busy.add(key.hash_id)
                    logv(f"conta {key.ref} no teto de concorrência ({max_inflight} por conta) — a saltar para a próxima")
                    continue
                if max_inflight > 0:
                    token = acquired_at + slot_ttl

            attempts += 1
            headers = {"Content-Type": "application/json"}
            if key is None:
                headers["X-Tavily-Access-Mode"] = "keyless"
                keyless_attempts += 1
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
                    if key is not None:
                        key.credits_spent += credits_used(payload, depth)
                        key.failures = key.consec_base = 0
                        key.failures_reset = key.dirty = True
                    result = normalize(payload, query)
                    result["meta"] = {
                        "attempts": attempts,
                        "keyless_used": keyless_attempts > 0,
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
                    if kind == "transient":
                        strikes["keyless"] = strikes.get("keyless", 0) + 1
                    logv(f"contingência keyless respondeu HTTP {status} — a continuar")
                    continue
                key.total_failures += 1
                key.last_error = f"HTTP {status}"
                key.dirty = key.error_observed = True
                tried.add(key.hash_id)
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
                strikes[key.hash_id] = strikes.get(key.hash_id, 0) + 1
                logv(f"instabilidade transitória (HTTP {status}) com {key.ref} — a rotacionar")
            except OSError as exc:
                cred_id = "keyless" if key is None else key.hash_id
                strikes[cred_id] = strikes.get(cred_id, 0) + 1
                if key is not None:
                    key.total_failures += 1
                    key.last_error = "rede/timeout"
                    key.dirty = key.error_observed = True
                    tried.add(key.hash_id)
                logv(f"falha de rede/timeout com {label}: {exc} — a rotacionar")
            finally:
                if key is not None and token is not None:
                    state.release(key, token)
                state.snapshot(pool, cursor_hash=(key.hash_id if key is not None else None), now=now())

        if not budget_left():
            break
        # passagem esgotada: espera pelo cooldown mais curto das chaves que VÃO
        # recuperar (REVOKED não conta; uma conta fora da invocação por falhas
        # transitórias está sempre ACTIVE) e repete a MESMA request.
        t = now()
        revive(pool, t)
        waits = [k.cooldown_until - t for k in pool
                 if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and k.cooldown_until > t]
        if any(k.status == "ACTIVE" and not benched(k.hash_id) for k in pool):
            waits.append(COOLDOWN_BASE_S)  # conta viva (revivida/ocupada/1 falha): nova passagem em breve
        remaining = min(waits, default=0.0)
        if waits and waited + remaining <= max_wait:
            pause = remaining + rng() * COOLDOWN_JITTER_S
            logv(f"pool impedido nesta passagem — a aguardar {pause:.1f}s e a repetir a mesma request")
            sleep(pause)
            waited += pause
            continue
        break

    masked = ", ".join(k.ref for k in pool) or "nenhuma"
    if keyless_attempts:
        tail = "contingência keyless incluída"
    elif keyless:
        tail = "contingência keyless não chegou a ser necessária/possível"
    else:
        tail = "contingência keyless desativada"
    raise SkillError(
        f"todas as alternativas falharam ({attempts} tentativa(s); pool: {masked}; {tail}).",
        f"aguarde ~{max(1, math.ceil(remaining))}s e repita a MESMA invocação, ou declare mais chaves "
        f"(export TAVILY_API_KEY_E=\"tvly-…\") e repita. `tavily.py status --check` audita o pool.",
    )


def credits_used(payload, depth: str) -> int:
    """Créditos gastos por uma pesquisa bem-sucedida: `usage.credits` da resposta
    (include_usage) ou, na falta dele, o custo documentado da profundidade."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    credits = _as_number(usage.get("credits")) if isinstance(usage, dict) else None
    if credits is not None and credits >= 0:
        return math.ceil(credits)
    return DEPTH_CREDITS.get(depth, 1)


def normalize(payload, query: str) -> dict:
    data = payload if isinstance(payload, dict) else {}
    raw_results = data.get("results")
    results = []
    for item in raw_results if isinstance(raw_results, list) else []:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": _clean_text(item.get("title", "")),
                "url": _clean_text(item.get("url", "")),
                "content": _clean_text(item.get("content", "")),
                "score": _as_float(item.get("score")),
            }
        )
    answer = data.get("answer")
    return {
        "query": _clean_text(data.get("query") or query),
        "answer": _clean_text(answer) if isinstance(answer, str) else None,
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

def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def cmd_status(check: bool, timeout: float, *, state: PoolState, reset_state: bool = False,
               max_inflight: int = DEFAULT_MAX_INFLIGHT, refresh: bool = True,
               env: dict[str, str] | None = None, now=time.time, get=None) -> int:
    """Estado do pool (sem segredos). Sem `--check`, refresca sozinho o saldo
    registado há mais de 60 min (no máx. 1 consulta a /usage por conta a cada
    6 min, reservada atomicamente no registo); `refresh=False` desliga.
    Relógio, env e GET são injetáveis."""
    if get is None:
        get = http_get_json
    env = os.environ if env is None else env
    remember_secrets(secret_values(env))
    if reset_state:
        state.reset()  # primeiro — também sem chaves; o pool abaixo parte do registo limpo
    pool = load_keys(env)
    state.apply(pool)
    t = now()
    revive(pool, t)  # cooldowns já expirados aparecem como recuperados

    outcomes: list[tuple[KeyMeta, str]] = []
    refresh_off = False
    if pool and refresh and not check and not reset_state:
        if state.persistent:
            outcomes = auto_refresh_usage(pool, get=get, now=t, timeout=min(timeout, AUTO_REFRESH_TIMEOUT_S),
                                          claim=state.claim_usage_query)
            if outcomes:
                state.snapshot([k for k, _ in outcomes], now=t)
        else:
            refresh_off = any(k.status != "REVOKED" and usage_is_stale(k, t) for k in pool)

    print(f"Pool Tavily: {len(pool)} credencial(is)" + ("" if pool else " — modo keyless puro"))
    for k in pool:
        bits = [f"req={k.total_requests}", f"err={k.total_failures}"]
        if k.last_error:
            bits.append(f"último={k.last_error}")
        if k.status in ("RATE_LIMITED", "QUOTA_EXHAUSTED") and k.cooldown_until > t:
            bits.append(f"cooldown={int(k.cooldown_until - t)}s")
        balance = effective_remaining(k, t)
        if balance is not None:
            limit = k.usage.get("limit") if isinstance(k.usage, dict) else None
            checked = _as_float(k.usage.get("checked_at"), t)
            bits.append(f"restam {balance}" + (f"/{limit}" if limit is not None else "")
                        + f" (saldo de há {_age(t - checked)})")
        if k.aliases:
            bits.append("alias: " + ", ".join(k.aliases))
        print(redact(f"  {k.env_name:<20} {k.ref}  [{k.status}]  " + " · ".join(bits), _SECRETS))
    for name in invalid_key_vars(env):
        print(f"  {name:<20} [FORMATO INVÁLIDO]  ignorada — o valor tem espaços, aspas, quebras de linha, "
              f"caracteres fora de ASCII ou menos de {MIN_KEY_LEN} caracteres; corrija a variável")
    infl = "sem teto" if max_inflight <= 0 else f"máx. {max_inflight} simultâneas/conta"
    print(f"Registo: {state.describe()}  ·  concorrência: {infl}")
    refreshed = [k.env_name for k, outcome in outcomes if outcome == "ok"]
    failed = [f"{k.env_name} ({outcome})" for k, outcome in outcomes if outcome != "ok"]
    if refreshed:
        print(f"Saldo refrescado agora via /usage: {', '.join(refreshed)} (registo com mais de 60 min).")
    if failed:
        print(f"Não foi possível refrescar o saldo de {', '.join(failed)} — a mostrar o último registado.")
    if refresh_off:
        print("Saldo com mais de 60 min, mas sem registo persistente não há refrescamento automático "
              "— use `status --check`.")
    if reset_state:
        print("\nRegisto limpo — todas as chaves voltam a ser consideradas na próxima pesquisa.")
    if not pool:
        print("\nNenhuma chave válida no terminal. Declaração (pool recomendado):")
        print('  export TAVILY_API_KEY_A="tvly-..."')
        print('  export TAVILY_API_KEY_B="tvly-..."')
        print("Sem chaves o script opera em modo keyless (limites mais severos).")
        return 0
    if check:
        print("\nVerificação ao vivo via /usage (não gasta créditos de pesquisa; limite próprio: 10 req/10 min)…")
        verified: set[str] = set()
        for k in pool:
            state.sync_credits(k)
            try:
                kind, status = refresh_usage(k, get=get, timeout=timeout, now=now())
            except OSError as exc:
                print(redact(f"  {k.env_name:<20} {k.ref}  → sem resposta ({exc})", _SECRETS))
                continue
            if kind == "success":
                k.status, k.cooldown_until, k.failures, k.consec_base = "ACTIVE", 0.0, 0, 0
                k.dirty = k.failures_reset = True
                verified.add(k.hash_id)
                u = k.usage
                rest = "sem teto" if u["remaining"] is None else f"restam {u['remaining']}"
                verdict = f"VÁLIDA · plano {u['plan']} · usados {u['used']}/{u['limit'] if u['limit'] is not None else '∞'} · {rest}"
            elif kind == "unauthorized":
                verified.add(k.hash_id)
                verdict = "REVOGADA (401) — gere nova em app.tavily.com"
            elif kind == "invalid-usage":
                verdict = "resposta inválida do /usage (sem dados de consumo) — proxy ou portal cativo na rede?"
            elif status == 429:
                verdict = "rate limit do endpoint /usage (máx. 10 req/10 min) — repita dentro de instantes"
            else:
                verdict = f"HTTP {status}"
            print(redact(f"  {k.env_name:<20} {k.ref}  → {verdict}", _SECRETS))
        state.snapshot(pool, authoritative=verified, now=now())  # só as verificadas impõem estado
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
        except Exception as exc:  # noqa: BLE001 — um cenário que rebenta é FAIL, nunca traceback
            failures += 1
            print(f"  FAIL  {name}: exceção inesperada {type(exc).__name__}: {exc}")

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
        assert result["meta"]["keyless_used"] is False and len(sleeps) == 1, \
            "libertado o slot, a MESMA conta tem de servir logo a seguir (não o keyless)"

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

    # ---- peças auxiliares (v0.3.0) ----

    # payload REAL de GET /usage (conta Researcher, capturado em 2026-09-26)
    REAL_USAGE = {
        "key": {"usage": 800, "limit": None, "search_usage": 800, "crawl_usage": 0,
                "extract_usage": 0, "map_usage": 0, "research_usage": 0},
        "account": {"current_plan": "Researcher", "plan_usage": 800, "plan_limit": 1000,
                    "search_usage": 800, "crawl_usage": 0, "extract_usage": 0, "map_usage": 0,
                    "research_usage": 0, "paygo_usage": 0, "paygo_limit": None},
    }

    def fake_get(responses):
        calls = []

        def get(url, headers, _timeout):
            calls.append({"url": url, "headers": dict(headers)})
            step = responses.pop(0)
            if isinstance(step, Exception):
                raise step
            return step

        return get, calls

    def usage_entry(remaining: int, checked_at: float = 0.0) -> dict:
        return {"status": "ACTIVE", "usage": {"plan": "Researcher", "used": 1000 - remaining, "limit": 1000,
                                              "remaining": remaining, "checked_at": checked_at,
                                              "spent_at_check": 0}}

    def s_retry_after_data_http():
        now = float(calendar.timegm((2026, 10, 21, 7, 27, 30, 0, 0, 0)))
        date = "Wed, 21 Oct 2026 07:28:00 GMT"
        assert parse_retry_after(date, now) == 30.0, "data HTTP devia dar 30 s"
        assert parse_retry_after(date, now + 3600) == 0.0, "data no passado → 0 (nunca negativo)"
        assert parse_retry_after("7", now) == 7.0, "delta em segundos"
        for junk in ("amanhã", "²", "-5", "", None):
            assert parse_retry_after(junk, now) is None, f"{junk!r} devia ser ignorado sem rebentar"
        pool = make_pool("A", "B")
        post, _c = transport([(429, {}, date), OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: now)
        assert pool[0].cooldown_until == now + 30.0, "o 429 com data HTTP devia arrefecer a chave 30 s"

    def s_retry_after_absurdo_limitado():
        pool = make_pool("A", "B")
        post, _c = transport([(429, {}, "999999999"), OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0)
        assert pool[0].cooldown_until == RETRY_AFTER_MAX_S, "Retry-After absurdo tem de ser limitado a 1 h"

    def s_parse_usage_payload_real():
        u = parse_usage(REAL_USAGE, now=123.0)
        assert (u["plan"], u["used"], u["limit"], u["remaining"]) == ("Researcher", 800, 1000, 200), u
        assert u["key_used"] == 800 and u["key_limit"] is None and u["checked_at"] == 123.0, u

    def s_parse_usage_sem_teto_e_lixo():
        sem_teto = json.loads(json.dumps(REAL_USAGE))
        sem_teto["account"]["plan_limit"] = None
        u = parse_usage(sem_teto, now=1.0)
        assert u["limit"] is None and u["remaining"] is None, "plan_limit nulo → saldo desconhecido"
        sem_teto["key"].update({"limit": 100, "usage": 30})
        assert parse_usage(sem_teto, now=1.0)["remaining"] == 70, "teto da chave conta quando o do plano é nulo"
        com_teto_chave = json.loads(json.dumps(REAL_USAGE))
        com_teto_chave["key"].update({"limit": 850, "usage": 800})
        assert parse_usage(com_teto_chave, now=1.0)["remaining"] == 50, "vence o MENOR saldo (chave vs plano)"
        for junk in (None, [], "x", {"account": "x"}, {"account": {"plan_usage": "abc", "plan_limit": "zz"}}):
            u = parse_usage(junk, now=1.0)
            assert u["remaining"] is None and u["used"] == 0, f"payload inválido {junk!r} não pode rebentar"

    def s_resolve_max_inflight_prioridade():
        assert resolve_max_inflight(5, {"TAVILY_MAX_INFLIGHT_PER_KEY": "3"}) == 5, "CLI vence o ambiente"
        assert resolve_max_inflight(None, {"TAVILY_MAX_INFLIGHT_PER_KEY": "3"}) == 3, "ambiente vence a predefinição"
        assert resolve_max_inflight(None, {}) == DEFAULT_MAX_INFLIGHT == 2, "predefinição = 2"
        assert resolve_max_inflight(0, {"TAVILY_MAX_INFLIGHT_PER_KEY": "3"}) == 0, "CLI 0 = sem teto"
        assert resolve_max_inflight(None, {"TAVILY_MAX_INFLIGHT_PER_KEY": "0"}) == 0, "ambiente 0 = sem teto"
        assert resolve_max_inflight(None, {"TAVILY_MAX_INFLIGHT_PER_KEY": " 4 "}) == 4
        for junk in ("abc", "-1", "²", "1.5"):
            assert resolve_max_inflight(None, {"TAVILY_MAX_INFLIGHT_PER_KEY": junk}) == 2, f"{junk!r} → predefinição"

    def s_teto_zero_nao_rouba_slots():
        pool = make_pool("A")
        state = PoolState.memory()
        assert state.try_acquire(pool[0], 2, 1.0) and state.try_acquire(pool[0], 2, 1.5), "slots de OUTRO processo"
        others = list(state.data["inflight"][pool[0].hash_id])
        assert others == [1.0 + INFLIGHT_TTL_S, 1.5 + INFLIGHT_TTL_S], f"o slot guarda a expiração: {others}"
        post, calls = transport([OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                   now=lambda: 2.0, state=state, max_inflight=0)
        assert calls[0]["headers"]["Authorization"].endswith("A"), "0 = sem teto: a conta cheia continua a servir"
        assert state.data["inflight"][pool[0].hash_id] == others, "sem teto não pode libertar slots alheios"
        state.try_acquire(pool[0], 3, 3.0)
        state.release(pool[0], 3.0 + INFLIGHT_TTL_S)
        assert state.data["inflight"][pool[0].hash_id] == others, "release liberta o slot PRÓPRIO"

    def s_load_keys_alias_sem_duplicar():
        a, b = make_pool("A", "B")
        pool = load_keys({"TAVILY_API_KEY": a.key, "TAVILY_API_KEY_A": a.key, "TAVILY_API_KEY_B": b.key})
        assert [k.env_name for k in pool] == ["TAVILY_API_KEY_A", "TAVILY_API_KEY_B"], [k.env_name for k in pool]
        assert pool[0].aliases == ["TAVILY_API_KEY"] and pool[1].aliases == [], "alias registado como nota"
        assert pool[0].hash_id == a.hash_id, "a identidade no registo continua a ser o valor da chave"

    def s_load_keys_ordem_deterministica():
        a, b, c = make_pool("A", "B", "C")
        env1 = {"TAVILY_API_KEY_C": c.key, "TAVILY_API_KEY": "tvly-UNICA-xxxxxxxx", "TAVILY_API_KEY_A": a.key,
                "TAVILY_API_KEY_D": a.key, "TAVILY_API_KEY_B": b.key, "TAVILY_API_KEY_E": "  "}
        env2 = dict(reversed(list(env1.items())))
        names1 = [(k.env_name, k.aliases) for k in load_keys(env1)]
        assert names1 == [(k.env_name, k.aliases) for k in load_keys(env2)], "ordem tem de ser estável"
        assert [n for n, _ in names1] == ["TAVILY_API_KEY", "TAVILY_API_KEY_A", "TAVILY_API_KEY_B",
                                          "TAVILY_API_KEY_C"], names1
        assert names1[1][1] == ["TAVILY_API_KEY_D"], "valor repetido noutra variável é alias, não conta nova"

    # ---- rotação orientada ao saldo (v0.3.0) ----

    def s_saldo_maior_serve_primeiro():
        pool = make_pool("A", "B")
        state = PoolState.memory()
        state.data["keys"] = {pool[0].hash_id: usage_entry(5), pool[1].hash_id: usage_entry(900)}
        post, calls = transport([OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0, state=state)
        assert calls[0]["headers"]["Authorization"].endswith("B"), "a conta com 900 restantes devia servir primeiro"
        assert effective_remaining(pool[1]) == 899, "o crédito gasto desconta do saldo estimado"

    def write_state(td: str, keys: dict, inflight: dict | None = None) -> None:
        with open(os.path.join(td, "pool-state.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": STATE_VERSION, "cursor": None, "keys": keys, "inflight": inflight or {}}, handle)

    def s_saldo_igual_alterna_com_cursor():
        with tempfile.TemporaryDirectory() as td:
            seed = make_pool("A", "B")
            write_state(td, {seed[0].hash_id: usage_entry(900), seed[1].hash_id: usage_entry(901)})
            used = []
            for i in range(3):
                pool = make_pool("A", "B")
                post, calls = transport([OK])
                run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                           now=lambda: float(i), state=PoolState.open(td))
                used.append(calls[0]["headers"]["Authorization"][-1])
            # 901 serve (→900); empate 900/900 → cursor (depois de B) → A (→899); B 900 volta a servir
            assert used == ["B", "A", "B"], f"o saldo estimado tem de guiar e o cursor desempatar (obtido {used})"
            entries = PoolState.open(td).data["keys"]
            assert entries[seed[0].hash_id]["credits_spent"] == 1 and entries[seed[1].hash_id]["credits_spent"] == 2

    def s_saldo_falha_transitoria_ainda_rotaciona():
        pool = make_pool("A", "B")
        state = PoolState.memory()
        state.data["keys"] = {pool[0].hash_id: usage_entry(900), pool[1].hash_id: usage_entry(5)}
        post, calls = transport([(503, {}, None), OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0, state=state)
        used = [c["headers"]["Authorization"][-1] for c in calls]
        assert used == ["A", "B"], f"um 5xx na conta com mais saldo tem de rodar para a seguinte (obtido {used})"

    def s_creditos_contados_pela_resposta():
        pool = make_pool("A")
        paid = (200, {"query": "q", "results": [], "usage": {"credits": 2}}, None)
        post, calls = transport([paid, OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        assert calls[0]["body"].get("include_usage") is True, "a request tem de pedir include_usage"
        assert pool[0].credits_spent == 2, "usage.credits da resposta é a fonte de verdade"
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, depth="advanced")
        assert pool[0].credits_spent == 4, "sem usage na resposta, advanced custa 2 créditos"

    # ---- saldo (/usage) refrescado sozinho no status (v0.3.0) ----

    def s_usage_refresca_quando_velho():
        T = 1_800_000_000.0
        k = make_pool("A")[0]
        k.usage = {"remaining": 900, "limit": 1000, "checked_at": T - 3601}
        k.usage_queried_at = T - 3601
        get, calls = fake_get([(200, REAL_USAGE)])
        assert auto_refresh_usage([k], get=get, now=T, timeout=1.0) == [(k, "ok")] and len(calls) == 1, \
            "stale → 1 consulta"
        assert k.usage["checked_at"] == T and k.usage["remaining"] == 200, "checked_at/saldo atualizados"
        get2, calls2 = fake_get([])
        assert auto_refresh_usage([k], get=get2, now=T + 60, timeout=1.0) == [] and calls2 == [], \
            "saldo fresco → nenhuma consulta"

    def s_usage_sem_rajadas():
        T = 1_800_000_000.0
        k, dead = make_pool("A", "B")
        k.usage = {"remaining": 900, "checked_at": T - 7200}
        k.usage_queried_at = T - 180  # tentativa falhada há 3 min
        mark_revoked(dead)
        get, calls = fake_get([(429, {}), OSError("timeout")])
        auto_refresh_usage([k, dead], get=get, now=T, timeout=1.0)
        assert calls == [], "consulta há < 6 min: não pode repetir (limite de 10 req/10 min)"
        auto_refresh_usage([k, dead], get=get, now=T + 240, timeout=1.0)
        assert len(calls) == 1, "7 min depois volta a tentar — e REVOKED nunca é consultada"
        auto_refresh_usage([k, dead], get=get, now=T + 300, timeout=1.0)
        assert len(calls) == 1, "o 429 também conta como consulta: sem rajada"
        auto_refresh_usage([k, dead], get=get, now=T + 700, timeout=1.0)
        assert len(calls) == 2 and k.usage["checked_at"] == T - 7200, "falha de rede não rebenta nem inventa saldo"

    def s_status_refresca_e_persiste():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            a, b = make_pool("A", "B")
            env = {"TAVILY_API_KEY": a.key, "TAVILY_API_KEY_A": a.key, "TAVILY_API_KEY_B": b.key}
            get, calls = fake_get([(200, REAL_USAGE), (200, REAL_USAGE)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cmd_status(False, 5.0, state=PoolState.open(td), env=env, now=lambda: T, get=get)
            text = out.getvalue()
            assert code == 0 and len(calls) == 2, "saldo desconhecido → uma consulta por conta"
            assert "restam 200/1000" in text, "o status tem de mostrar o saldo fresco"
            assert a.key not in text and b.key not in text, "sem material de chave na saída"
            entry = PoolState.open(td).data["keys"][a.hash_id]
            assert entry["usage"]["checked_at"] == T and entry["usage_queried_at"] == T, "refrescamento persistido"
            get2, calls2 = fake_get([])
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(False, 5.0, state=PoolState.open(td), env=env, now=lambda: T + 600, get=get2)
            assert calls2 == [], "10 min depois o saldo ainda é fresco: nenhuma consulta"

    def s_status_nome_canonico_sem_duplicar():
        a, b = make_pool("A", "B")
        env = {"TAVILY_API_KEY": a.key, "TAVILY_API_KEY_A": a.key, "TAVILY_API_KEY_B": b.key}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_status(False, 5.0, state=PoolState.memory(), env=env, refresh=False, now=lambda: 0.0)
        text = out.getvalue()
        lines = [line for line in text.splitlines() if line.startswith("  TAVILY_API_KEY")]
        assert "Pool Tavily: 2 credencial(is)" in text, "pool a 2 entradas"
        assert [line.split()[0] for line in lines] == ["TAVILY_API_KEY_A", "TAVILY_API_KEY_B"], lines
        assert "alias: TAVILY_API_KEY" in lines[0], "o alias fica como nota da entrada canónica"

    # ---- robustez do contrato (v0.3.0) ----

    def s_revogada_nao_impede_espera_de_cooldown():
        pool = make_pool("A", "B")
        clock = [0.0]
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        post, calls = transport([(401, {}, None), (429, {}, "2"), (503, {}, None), OK])
        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0,
                            now=lambda: clock[0], max_wait=30)
        assert sleeps and sleeps[-1] >= 2.0, "com A revogada, devia esperar pelo cooldown de B (não desistir)"
        assert calls[-1]["headers"]["Authorization"].endswith("B") and result["meta"]["attempts"] == 4

    def s_resposta_http_malformada_e_falha_de_rede():
        original = urllib.request.urlopen

        def broken(*_a, **_k):
            raise http.client.IncompleteRead(b"parcial")

        urllib.request.urlopen = broken
        try:
            for call in (lambda: http_post_json(API_URL, {}, {}, 1.0), lambda: http_get_json(USAGE_URL, {}, 1.0)):
                try:
                    call()
                    raise AssertionError("devia levantar OSError")
                except OSError:
                    pass
        finally:
            urllib.request.urlopen = original

    def s_registo_corrompido_nao_rebenta():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A", "B")
            garbage = {"version": STATE_VERSION, "cursor": 42,
                       "keys": {pool[0].hash_id: {"status": ["x"], "cooldown_until": "abc", "total_requests": "x",
                                                  "usage": {"remaining": "zzz"}, "credits_spent": None},
                                pool[1].hash_id: "não sou um dicionário"},
                       "inflight": {pool[0].hash_id: "não sou uma lista", pool[1].hash_id: ["x", None]}}
            with open(os.path.join(td, "pool-state.json"), "w", encoding="utf-8") as handle:
                json.dump(garbage, handle)
            post, _c = transport([OK])
            result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                                now=lambda: 0.0, state=PoolState.open(td))
            assert result["meta"]["attempts"] == 1, "registo corrompido tem de ser tratado como ausente"
            entry = PoolState.open(td).data["keys"][pool[0].hash_id]
            assert entry["total_requests"] == 1 and entry["status"] == "ACTIVE", entry

    @contextlib.contextmanager
    def patched_env(values: dict):
        """Ambiente HERMÉTICO para testes que passam por main(): nenhuma chave real."""
        saved = {k: v for k, v in os.environ.items() if k.startswith("TAVILY_")}
        for name in saved:
            del os.environ[name]
        os.environ.update(values)
        try:
            yield
        finally:
            for name in [k for k in os.environ if k.startswith("TAVILY_")]:
                del os.environ[name]
            os.environ.update(saved)

    @contextlib.contextmanager
    def patched_run_search(fake):
        g = globals()
        original = g["run_search"]
        g["run_search"] = fake
        try:
            yield
        finally:
            g["run_search"] = original

    def run_main(argv):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def never_search(*_a, **_k):
        raise AssertionError("não devia chegar a pesquisar (I/O real)")

    def s_cli_erros_seguem_o_contrato():
        bad_args = (["search", "x", "--no-state", "--timeout", "-1"], ["search", "x", "--no-state", "--timeout", "0"],
                    ["search", "x", "--no-state", "--timeout", "nan"], ["search", "x", "--no-state", "--timeout", "1e12"],
                    ["search", "x", "--no-state", "--max-wait", "inf"], ["search", "x", "--no-state", "--max-wait", "-1"],
                    ["search", "x", "--no-state", "--max-bytes", "-5"], ["search", "x", "--no-state", "--depth", "profunda"],
                    ["status", "--opcao-que-nao-existe"], [])
        with patched_env({}), patched_run_search(never_search):
            for argv in bad_args:
                code, _out, err = run_main(argv)
                assert code == 2, f"{argv}: exit {code} (esperado 2)"
                assert "Erro: argumentos inválidos" in err and "Solução:" in err, f"{argv}: {err!r}"
                assert "Traceback" not in err, argv
        with patched_env({}):
            code, _out, err = run_main(["search", "", "--no-state"])
        assert code == 2 and "Erro: a consulta está vazia" in err and "Solução:" in err, err

    def s_falha_interna_sem_traceback_nem_segredo():
        secret = make_pool("Z")[0].key

        def explode(*_a, **_k):
            raise ValueError("bug com " + secret)

        with patched_env({"TAVILY_API_KEY_Z": secret}), patched_run_search(explode):
            code, _out, err = run_main(["search", "x", "--no-state"])
            assert code == 2 and "Erro: falha interna" in err and "Solução:" in err, err
            assert "Traceback" not in err and secret not in err, "sem traceback nem segredo"
            code, _out, err = run_main(["search", "x", "--no-state", "--verbose"])
            assert code == 2 and "Traceback" in err, "com --verbose o diagnóstico mostra o traceback…"
            assert secret not in err and "[REDACTED]" in err, "…mas sempre redigido"

    def s_skillerror_em_main_redigida():
        secret = make_pool("Z")[0].key

        def echo(*_a, **_k):
            raise SkillError(f"a API rejeitou a consulta (eco {secret}).", "simplifique")

        with patched_env({"TAVILY_API_KEY_Z": secret}), patched_run_search(echo):
            code, _out, err = run_main(["search", "x", "--no-state"])
        assert code == 2 and "Erro:" in err and secret not in err and "[REDACTED]" in err, err

    def s_argparse_redige_segredos():
        secret = make_pool("Z")[0].key
        with patched_env({"TAVILY_API_KEY_Z": secret}), patched_run_search(never_search):
            for argv in (["search", "q", "--api-key", secret], ["search", "q", "--depth", secret],
                         ["status", "--timeout", secret], ["search", "q", "--depth", "tvly-dev-NAOESTANOENV123"]):
                code, _out, err = run_main(argv)
                assert code == 2 and "Erro: argumentos inválidos" in err, err
                assert secret not in err and "NAOESTANOENV" not in err, f"o argparse ecoou um segredo: {err!r}"

    def s_verbose_redige_segredos():
        global VERBOSITY
        pool = make_pool("A", "B")
        post, _c = transport([OSError(f"eco {pool[0].key}"), OK])
        err = io.StringIO()
        previous, VERBOSITY = VERBOSITY, True
        try:
            with contextlib.redirect_stderr(err):
                run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        finally:
            VERBOSITY = previous
        assert "[tavily]" in err.getvalue(), "o --verbose devia emitir diagnóstico"
        assert pool[0].key not in err.getvalue(), "o --verbose também tem de redigir segredos"

    def s_orcamento_invalido_nunca_inverte_o_corte():
        result = {"answer": "resposta", "results": [{"content": "x" * 100}], "meta": {}}
        apply_budget(result, -5)
        assert result["answer"] is None and result["results"][0]["content"] == "", "orçamento negativo = 0 bytes"
        assert result["meta"]["truncated"] is True

    # ---- revisão adversarial v0.3.0: rotação e esperas ----

    def recording_sleep(clock):
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        return sleeps, fake_sleep

    def s_falha_transitoria_isolada_repete_a_conta():
        pool = make_pool("A")
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        post, calls = transport([(503, {}, None), OK])
        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
        assert [c["headers"].get("Authorization", "keyless")[-1] for c in calls] == ["A", "A"], calls
        assert result["meta"]["keyless_used"] is False, "um 5xx isolado repete a conta, não vai a keyless"
        assert sleeps == [COOLDOWN_BASE_S], f"a repetição espera um backoff curto (obtido {sleeps})"

    def s_nova_ronda_nao_fura_round_robin():
        pool = make_pool("A", "B")
        clock = [0.0]
        _sleeps, fake_sleep = recording_sleep(clock)
        post, calls = transport([(503, {}, None), (429, {}, "0"), OK])
        run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
        used = [c["headers"]["Authorization"][-1] for c in calls]
        assert used == ["A", "B", "A"], f"uma chave com 429 'Retry-After: 0' não pode furar a ronda (obtido {used})"

    def s_saldo_rede_na_preferida_rotaciona():
        pool = make_pool("A", "B")
        state = PoolState.memory()
        state.data["keys"] = {pool[0].hash_id: usage_entry(900), pool[1].hash_id: usage_entry(5)}
        post, calls = transport([OSError("timeout"), OK])
        run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0, state=state)
        used = [c["headers"]["Authorization"][-1] for c in calls]
        assert used == ["A", "B"], f"timeout na conta com mais saldo tem de rodar para a seguinte (obtido {used})"

    def s_pick_key_escaloes_de_saldo():
        def pool_with(*balances):
            pool = make_pool(*"ABC"[:len(balances)])
            for k, b in zip(pool, balances):
                k.usage = None if b is None else {"remaining": b, "checked_at": 0.0, "spent_at_check": 0}
            return pool

        def pick(pool, cursor):
            key, _ = pick_key(pool, cursor, 0.0)
            return key.env_name[-1]

        assert pick(pool_with(0, None), 0) == "B", "saldo 0 conhecido fica atrás do desconhecido"
        assert pick(pool_with(0, 0), 0) == "A" and pick(pool_with(0, 0), 1) == "B", "todas a 0 → round-robin"
        assert [pick(pool_with(100, None, 50), c) for c in (0, 1, 2)] == ["A", "B", "A"], \
            "desconhecido e melhor saldo alternam; saldo inferior (50) fica para trás"
        assert [pick(pool_with(None, None, None), c) for c in (0, 1, 2)] == ["A", "B", "C"], "sem saldo = round-robin"

    def s_espera_de_concorrencia_usa_max_wait():
        pool = make_pool("A")
        state = PoolState.memory()
        state.try_acquire(pool[0], 1, 0.0)  # slot preso por outro processo durante toda a pesquisa
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        post, calls = transport([OK])
        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0],
                            state=state, max_inflight=1, max_wait=5.0)
        assert 4.5 <= sum(sleeps) <= 5.0, f"a espera por concorrência tem de ir até --max-wait (esperou {sum(sleeps)})"
        assert result["meta"]["keyless_used"] is True and len(calls) == 1, "esgotada a espera, vai a keyless"

    def s_transitorias_limitadas():
        pool = make_pool("A", "B")
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        calls = []

        def post(_url, headers, _body, _t):
            calls.append(headers["Authorization"][-1] if "Authorization" in headers else "keyless")
            if "Authorization" not in headers:
                return (503, {}, None)
            return (503, {}, None) if headers["Authorization"].endswith("A") else (429, {}, "1")

        try:
            run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
            raise AssertionError("devia desistir")
        except SkillError as exc:
            assert "keyless incluída" in exc.problem, exc.problem
        assert calls.count("A") == TRANSIENT_STRIKES, f"5xx persistente: no máx. {TRANSIENT_STRIKES} pedidos (A={calls})"
        assert calls.count("keyless") <= TRANSIENT_STRIKES and calls.count("B") <= MAX_PASSES, calls
        assert sum(sleeps) <= DEFAULT_MAX_WAIT + COOLDOWN_JITTER_S, "as esperas respeitam --max-wait"

    def s_retry_after_zero_sem_rajada():
        pool = make_pool("A")
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        calls = []

        def post(_url, headers, _body, _t):
            calls.append(headers.get("Authorization", "keyless")[-1])
            return (429, {}, "0")

        try:
            run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
        except SkillError:
            pass
        assert calls.count("A") <= MAX_PASSES, f"'Retry-After: 0' não pode gerar rajada (A={calls.count('A')})"
        assert sleeps and min(sleeps) >= COOLDOWN_BASE_S, "cada repetição espera pelo menos 0,5 s"
        k = make_pool("Z")[0]
        mark_rate_limited(k, 0.0, 10.0, lambda: 0.0)
        assert k.cooldown_until == 10.0 + COOLDOWN_BASE_S, "o cooldown registado (visto por outros) tem piso"

    def s_backoff_429_escala():
        k = make_pool("A")[0]
        mark_rate_limited(k, None, 0.0, lambda: 0.0)
        first = k.cooldown_until
        revive([k], first)
        mark_rate_limited(k, None, first, lambda: 0.0)
        assert k.cooldown_until - first > first, "o backoff sem Retry-After tem de escalar entre 429 seguidos"
        post, _c = transport([OK])
        run_search("q", pool=[k], post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 1e6)
        assert k.failures == 0, "um sucesso zera as falhas consecutivas"

    def s_revogada_com_cooldown_residual_nao_espera():
        pool = make_pool("A")
        mark_revoked(pool[0])
        pool[0].cooldown_until = 20.0  # resíduo de um 429 anterior
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        post, calls = transport([(503, {}, None), (503, {}, None)])
        try:
            run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
            raise AssertionError("devia desistir")
        except SkillError:
            pass
        assert all(s <= COOLDOWN_BASE_S + COOLDOWN_JITTER_S for s in sleeps), \
            f"não se espera pelo cooldown de uma chave REVOKED (esperas {sleeps})"

    def s_live_isolado_um_pedido():
        probe = make_pool("A")
        post, calls = transport([(503, {}, None), OK, OK])
        try:
            run_search("q", pool=probe, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 0.0,
                       max_wait=0.0, keyless=False, max_attempts=1)
            raise AssertionError("devia falhar")
        except SkillError as exc:
            assert "keyless desativada" in exc.problem, exc.problem
        assert len(calls) == 1 and "Authorization" in calls[0]["headers"], "--live: exatamente 1 pedido, sem keyless"

    # ---- revisão adversarial v0.3.0: registo, saldo e /usage ----

    def s_spent_at_check_com_creditos():
        k = make_pool("A")[0]
        k.credits_spent = 10
        get, _c = fake_get([(200, REAL_USAGE)])
        refresh_usage(k, get=get, timeout=1.0, now=5.0)
        assert k.usage["spent_at_check"] == 10 and effective_remaining(k, 5.0) == 200, "saldo fresco = /usage"
        k.credits_spent += 3
        assert effective_remaining(k, 5.0) == 197, "créditos gastos depois da consulta descontam"

    def s_creditos_persistem_entre_invocacoes():
        with tempfile.TemporaryDirectory() as td:
            seed = make_pool("A")
            write_state(td, {seed[0].hash_id: usage_entry(900)})
            for i in range(2):
                post, _c = transport([OK])
                run_search("q", pool=make_pool("A"), post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                           now=lambda: float(i), state=PoolState.open(td))
            fresh = make_pool("A")
            PoolState.open(td).apply(fresh)
            assert fresh[0].credits_spent == 2, "credits_spent tem de persistir entre invocações"
            assert effective_remaining(fresh[0], 3.0) == 898, "a invocação seguinte vê o saldo descontado"

    def s_status_check_autoritativo():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            a, b = make_pool("A", "B")
            write_state(td, {a.hash_id: {"status": "REVOKED", "total_requests": 5},
                             b.hash_id: {"status": "RATE_LIMITED", "cooldown_until": T + 50, "total_requests": 3}})
            env = {"TAVILY_API_KEY_A": a.key, "TAVILY_API_KEY_B": b.key}
            get, calls = fake_get([(200, REAL_USAGE), (429, {})])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_status(True, 5.0, state=PoolState.open(td), env=env, now=lambda: T, get=get)
            text = out.getvalue()
            assert len(calls) == 2 and "VÁLIDA" in text and "rate limit do endpoint /usage" in text, text
            entries = PoolState.open(td).data["keys"]
            assert entries[a.hash_id]["status"] == "ACTIVE", "--check com sucesso reativa com autoridade"
            assert entries[a.hash_id]["total_requests"] == 5, "a verificação não mexe nos contadores"
            assert entries[b.hash_id]["status"] == "RATE_LIMITED", "conta não verificada mantém o estado"
            assert entries[a.hash_id]["usage"]["remaining"] == 200

    def s_merge_campos_novos():
        with tempfile.TemporaryDirectory() as td:
            seed = make_pool("A")[0]
            write_state(td, {seed.hash_id: dict(usage_entry(900, checked_at=100.0), usage_queried_at=100.0)})
            k1, k2 = make_pool("A")[0], make_pool("A")[0]
            s1, s2 = PoolState.open(td), PoolState.open(td)
            s1.apply([k1])
            s2.apply([k2])
            fresh = json.loads(json.dumps(REAL_USAGE))
            get, _c = fake_get([(200, fresh)])
            refresh_usage(k2, get=get, timeout=1.0, now=200.0)  # p2 refresca o saldo
            s2.snapshot([k2], now=200.0)
            k1.credits_spent += 1  # p1 (vista antiga) gasta 1 crédito
            k1.total_requests += 1
            s1.snapshot([k1], now=201.0)
            entry = PoolState.open(td).data["keys"][seed.hash_id]
            assert entry["usage"]["checked_at"] == 200.0, "o /usage mais recente não pode ser sobreposto"
            assert entry["credits_spent"] == 1 and entry["usage_queried_at"] == 200.0, entry
            final = make_pool("A")
            PoolState.open(td).apply(final)
            assert effective_remaining(final[0], 300.0) == 199, "o crédito gasto depois do /usage desconta"

    def s_reserva_usage_atomica():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            s1, s2 = PoolState.open(td), PoolState.open(td)  # dois `status` concorrentes
            k1, k2 = make_pool("A")[0], make_pool("A")[0]
            assert s1.claim_usage_query(k1, T) is True, "o primeiro reserva a consulta"
            assert s2.claim_usage_query(k2, T + 1) is False, "o segundo, em simultâneo, não consulta"
            assert s2.claim_usage_query(k2, T + USAGE_MIN_INTERVAL_S + 1) is True, "passados 6 min volta a poder"
        assert PoolState.memory().claim_usage_query(make_pool("A")[0], T) is False, \
            "sem registo persistente não há anti-rajada → não consulta sozinho"

    def s_slot_proprio_libertado_pela_pesquisa():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A")
            PoolState.open(td).try_acquire(pool[0], 2, 0.0)  # slot de outro processo
            post, _c = transport([OK])
            run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0, now=lambda: 1.0,
                       state=PoolState.open(td), max_inflight=2)
            slots = PoolState.open(td).data["inflight"][pool[0].hash_id]
            assert slots == [INFLIGHT_TTL_S], f"a pesquisa só pode libertar o SEU slot (ficou {slots})"

    def s_slot_ttl_e_horizonte():
        state = PoolState.memory()
        k = make_pool("A")[0]
        state.try_acquire(k, 5, 0.0, ttl=610.0)
        state.data["inflight"][k.hash_id].append(99_999.0)  # expiração absurda (relógio avariado)
        state._prune_inflight(600.0)
        assert state.data["inflight"][k.hash_id] == [610.0], "slot de pedido longo vive; lixo no futuro cai"

    def s_reset_check_nao_ressuscita():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            a = make_pool("A")[0]
            write_state(td, {a.hash_id: {"status": "REVOKED", "total_requests": 9, "last_error": "HTTP 401"}})
            get, _c = fake_get([(429, {})])
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(True, 5.0, state=PoolState.open(td), reset_state=True,
                           env={"TAVILY_API_KEY_A": a.key}, now=lambda: T, get=get)
            entry = PoolState.open(td).data["keys"].get(a.hash_id) or {}
            assert entry.get("status", "ACTIVE") == "ACTIVE" and entry.get("total_requests", 0) == 0, \
                f"--reset-state --check não pode regravar o registo antigo: {entry}"

    def s_vista_antiga_nao_desfaz_reset_nem_check():
        with tempfile.TemporaryDirectory() as td:
            a, b = make_pool("A", "B")
            write_state(td, {a.hash_id: {"status": "REVOKED"}})
            stale = PoolState.open(td)
            pool = make_pool("A", "B")
            stale.apply(pool)  # pesquisa em curso carregou A como REVOKED
            PoolState.open(td).reset()  # entretanto: `status --reset-state`
            pool[1].total_requests += 1
            stale.snapshot(pool, now=1.0)  # a pesquisa termina (usou B)
            entries = PoolState.open(td).data["keys"]
            assert entries.get(a.hash_id, {}).get("status", "ACTIVE") == "ACTIVE", "o reset não pode ser desfeito"
            assert entries[b.hash_id]["total_requests"] == 1
            write_state(td, {a.hash_id: {"status": "REVOKED"}})
            stale2 = PoolState.open(td)
            pool2 = make_pool("A", "B")
            stale2.apply(pool2)
            checker = PoolState.open(td)
            ka = make_pool("A")[0]
            checker.apply([ka])
            ka.status, ka.dirty = "ACTIVE", True
            checker.snapshot([ka], authoritative={ka.hash_id}, now=2.0)  # `status --check`: A válida
            pool2[1].total_requests += 1
            stale2.snapshot(pool2, now=3.0)
            assert PoolState.open(td).data["keys"][a.hash_id]["status"] == "ACTIVE", "o --check não pode ser desfeito"

    def s_registo_apagado_nao_ressuscita():
        with tempfile.TemporaryDirectory() as td:
            a = make_pool("A")[0]
            write_state(td, {a.hash_id: {"status": "REVOKED", "total_requests": 7}})
            state = PoolState.open(td)
            pool = make_pool("A")
            state.apply(pool)
            os.remove(os.path.join(td, "pool-state.json"))  # utilizador limpou à mão
            state.snapshot(pool, now=1.0)
            entry = PoolState.open(td).data["keys"].get(a.hash_id)
            assert entry is None, f"apagar o ficheiro limpa mesmo o registo (ficou {entry})"

    def s_registo_com_nan_infinito_e_surrogates():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A", "B")
            raw = ('{"version": 1, "cursor": null, "keys": {"%s": {"status": "ACTIVE", "cooldown_until": NaN, '
                   '"total_requests": 1e400, "failures": Infinity, "credits_spent": %d, "last_error": "\\ud800", '
                   '"usage": {"remaining": 1e400, "checked_at": -Infinity}}, '
                   '"naotocada": {"lixo": {"a": [[[1]]]}, "usage": [[["x"]]], "status": ["x"]}}, '
                   '"inflight": {"%s": [NaN, 1e400]}}'
                   % (pool[0].hash_id, 10 ** 400, pool[0].hash_id))
            with open(os.path.join(td, "pool-state.json"), "w", encoding="utf-8") as handle:
                handle.write(raw)
            post, _c = transport([OK])
            result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                                now=lambda: 0.0, state=PoolState.open(td))
            assert result["meta"]["attempts"] == 1, "valores não finitos/gigantes no registo não podem partir nada"
            with contextlib.redirect_stdout(io.StringIO()):
                assert cmd_status(False, 5.0, state=PoolState.open(td), refresh=False, now=lambda: 0.0,
                                  env={"TAVILY_API_KEY_A": pool[0].key}) == 0
            saved = open(os.path.join(td, "pool-state.json"), encoding="utf-8").read()
            json.loads(saved)
            assert "lixo" not in saved and "[[" not in saved, "entradas não tocadas também são reduzidas ao conhecido"

    def s_saldo_do_mes_anterior_ignorado():
        T = float(calendar.timegm((2026, 10, 1, 0, 30, 0, 0, 0, 0)))  # 30 min depois do reset mensal
        pool = make_pool("A", "B")
        pool[0].usage = {"remaining": 5, "checked_at": T - 3600, "spent_at_check": 0}
        pool[1].usage = {"remaining": 900, "checked_at": T - 3600, "spent_at_check": 0}
        assert effective_remaining(pool[1], T) is None, "saldo de setembro não vale em outubro (cotas repostas)"
        key, _ = pick_key(pool, 0, T)
        assert key.env_name.endswith("A"), "sem saldo válido volta ao round-robin"

    def s_checked_at_no_futuro():
        T = 1_800_000_000.0
        k = make_pool("A")[0]
        k.usage = {"remaining": 900, "checked_at": T + 86400}
        assert usage_is_stale(k, T) and effective_remaining(k, T) is None, "carimbo no futuro = inválido"
        with tempfile.TemporaryDirectory() as td:
            write_state(td, {k.hash_id: {"usage": {"remaining": 1, "checked_at": T + 86400}}})
            fresh = make_pool("A")[0]
            get, _c = fake_get([(200, REAL_USAGE)])
            refresh_usage(fresh, get=get, timeout=1.0, now=T)
            PoolState.open(td).snapshot([fresh], now=T)
            assert PoolState.open(td).data["keys"][k.hash_id]["usage"]["checked_at"] == T, \
                "um checked_at no futuro não pode vencer o merge"

    def s_reset_sem_chaves():
        with tempfile.TemporaryDirectory() as td:
            write_state(td, {"abc": {"status": "REVOKED"}})
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(False, 5.0, state=PoolState.open(td), reset_state=True, env={}, now=lambda: 0.0)
            assert PoolState.open(td).data["keys"] == {}, "--reset-state limpa o registo mesmo sem chaves"

    def s_refresh_sem_registo_persistente():
        k = make_pool("A")[0]
        get, calls = fake_get([(200, REAL_USAGE)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_status(False, 5.0, state=PoolState.memory(), env={"TAVILY_API_KEY_A": k.key},
                       now=lambda: 1_800_000_000.0, get=get)
        assert calls == [] and "sem registo persistente" in out.getvalue(), "sem registo não há consultas sozinhas"

    def s_refresh_falhado_nao_anuncia_sucesso():
        with tempfile.TemporaryDirectory() as td:
            k = make_pool("A")[0]
            get, _c = fake_get([(429, {})])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_status(False, 5.0, state=PoolState.open(td), env={"TAVILY_API_KEY_A": k.key},
                           now=lambda: 1_800_000_000.0, get=get)
            text = out.getvalue()
            assert "Saldo refrescado agora" not in text and "Não foi possível refrescar" in text, text

    def s_refresh_para_na_primeira_falha_de_rede():
        with tempfile.TemporaryDirectory() as td:
            pool = make_pool("A", "B", "C")
            env = {k.env_name: k.key for k in pool}
            seen_timeouts: list[float] = []

            def get(_url, _headers, timeout):
                seen_timeouts.append(timeout)
                raise OSError("sem rede")

            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(False, 25.0, state=PoolState.open(td), env=env, now=lambda: 1_800_000_000.0, get=get)
            assert len(seen_timeouts) == 1, "pára à 1.ª falha de rede (não soma timeouts por conta)"
            assert seen_timeouts[0] <= AUTO_REFRESH_TIMEOUT_S, "o refrescamento automático usa timeout curto"

    # ---- revisão adversarial v0.3.0: chaves, segredos e saída ----

    def s_chave_malformada_fica_de_fora():
        good = make_pool("B")[0]
        seg1, seg2 = "tvly-dev-SEGREDOAAAAAAAAAAAA", "tvly-dev-SEGREDOBBBBBBBBBBBB"
        env = {"TAVILY_API_KEY_A": f"{seg1}\n{seg2}", "TAVILY_API_KEY_B": good.key,
               "TAVILY_API_KEY_C": "\u201dtvly-dev-ASPASTIPOGRAFICAS\u201d", "TAVILY_API_KEY_D": "curta"}
        pool = load_keys(env)
        assert [k.env_name for k in pool] == ["TAVILY_API_KEY_B"], [k.env_name for k in pool]
        assert invalid_key_vars(env) == ["TAVILY_API_KEY_A", "TAVILY_API_KEY_C", "TAVILY_API_KEY_D"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cmd_status(False, 5.0, state=PoolState.memory(), env=env, refresh=False, now=lambda: 0.0)
        text = out.getvalue()
        assert text.count("[FORMATO INVÁLIDO]") == 3 and seg1 not in text and seg2 not in text, text
        try:
            http_post_json("http://127.0.0.1:9/", {"Authorization": f"Bearer {seg1}\n{seg2}"}, {}, 0.5)
            raise AssertionError("devia levantar OSError")
        except OSError as exc:
            assert "pedido HTTP inválido" in str(exc), f"cabeçalho inválido tem de virar falha tratada: {exc}"
            assert seg1 not in str(exc) and exc.__cause__ is None, "a exceção não pode transportar a chave"

    def s_redacao_prefixo_e_ordem():
        text = "a tvly-ABCDEFGH-XYZ b tvly-ABCDEFGH c"
        out = redact(text, ["tvly-ABCDEFGH", "tvly-ABCDEFGH-XYZ"])
        assert "XYZ" not in out and "ABCDEFGH" not in out, out

    def s_surrogate_na_resposta():
        pool = make_pool("A")
        weird = (200, {"query": "q", "answer": "r\ud800", "results": [
            {"title": "t\udfff", "url": "u", "content": "c\ud800", "score": 1}]}, None)
        post, _c = transport([weird])
        result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0)
        json.dumps(result, ensure_ascii=False).encode("utf-8")  # não pode levantar
        render_text(result, pool).encode("utf-8")

    def s_consulta_nao_utf8():
        post, calls = transport([OK])
        try:
            run_search("abc\udcff", pool=make_pool("A"), post=post, sleep=lambda _s: None, rng=lambda: 0.0)
            raise AssertionError("devia recusar")
        except SkillError as exc:
            assert "UTF-8" in exc.problem and "Solução" in str(exc)
        assert calls == [], "nenhum pedido (nem crédito) para uma consulta inválida"

    def _subprocess_env(td: str, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TAVILY_")}
        env.update(TAVILY_STATE_DIR=td, **extra)
        return env

    def s_stdout_sem_utf8():
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            for argv in (["status", "--no-refresh"], ["--help"], ["search", "--help"]):
                proc = subprocess.run([sys.executable, os.path.abspath(__file__), *argv], capture_output=True,
                                      env=_subprocess_env(td, PYTHONIOENCODING="ascii"), timeout=60)
                assert proc.returncode == 0, f"{argv}: exit {proc.returncode} {proc.stderr[-300:]!r}"
                assert b"Traceback" not in proc.stderr, f"{argv}: {proc.stderr[-300:]!r}"

    def s_pipe_fechado_pelo_leitor():
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            for argv in (["status", "--no-refresh"], ["--help"], ["search", "--help"]):
                proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), *argv],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_subprocess_env(td))
                proc.stdout.close()  # o leitor fecha antes de ler (ex.: `| true`)
                _out, err = proc.communicate(timeout=60)
                assert proc.returncode == 0, f"{argv}: exit {proc.returncode}: {err[-300:]!r}"
                assert b"Traceback" not in err and b"Exception ignored" not in err, f"{argv}: {err[-300:]!r}"

    def s_retry_after_formatos():
        now = float(calendar.timegm((2026, 10, 21, 7, 27, 30, 0, 0, 0)))
        assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 +0000", now) == 30.0, "zona numérica"
        assert parse_retry_after("wed, 21 oct 2026 07:28:00 gmt", now) == 30.0, "minúsculas"
        import locale
        previous = locale.setlocale(locale.LC_TIME)
        for name in ("pt_PT.UTF-8", "pt_BR.UTF-8", "de_DE.UTF-8"):
            try:
                locale.setlocale(locale.LC_TIME, name)
            except locale.Error:
                continue
            try:
                assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now) == 30.0, f"locale {name}"
            finally:
                locale.setlocale(locale.LC_TIME, previous)
            break

    # ---- ronda 2 da revisão: resíduos ----

    def s_registo_aninhado_ao_absurdo():
        for depth in (1100, 200_000):
            with tempfile.TemporaryDirectory() as td:
                pool = make_pool("A")
                nested = "[" * depth + "]" * depth
                with open(os.path.join(td, "pool-state.json"), "w", encoding="utf-8") as handle:
                    handle.write('{"version": 1, "cursor": null, "keys": {"%s": {"usage": %s}}, "inflight": {}}'
                                 % (pool[0].hash_id, nested))
                post, _c = transport([OK])
                result = run_search("q", pool=pool, post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                                    now=lambda: 0.0, state=PoolState.open(td))
                assert result["meta"]["attempts"] == 1, f"aninhamento {depth} não pode partir a pesquisa"
                with contextlib.redirect_stdout(io.StringIO()):
                    assert cmd_status(False, 5.0, state=PoolState.open(td), reset_state=True, env={},
                                      now=lambda: 0.0) == 0
                assert PoolState.open(td).data["keys"] == {}, "o --reset-state recupera o registo"

    def s_espera_longa_chega_ao_keyless():
        pool = make_pool("A", "B", "C")
        state = PoolState.memory()
        for k in pool:
            state.try_acquire(k, 1, 0.0, ttl=1000.0)  # contas presas por outros durante toda a pesquisa
        clock = [0.0]
        sleeps, fake_sleep = recording_sleep(clock)
        post, calls = transport([OK])
        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0],
                            state=state, max_inflight=1, max_wait=300.0)
        assert sum(sleeps) >= 299.5 and result["meta"]["keyless_used"] is True, \
            f"a espera tem de ir até --max-wait (esperou {sum(sleeps)}) e acabar no keyless"

    def s_slot_com_ttl_maximo_sobrevive():
        state = PoolState.memory()
        k = make_pool("A")[0]
        ttl = 2 * MAX_TIMEOUT_S + 10.0
        state.try_acquire(k, 5, 1000.0, ttl=ttl)
        state._prune_inflight(1000.0 - 5.0)  # outro processo com o relógio 5 s atrasado
        assert state.data["inflight"].get(k.hash_id) == [1000.0 + ttl], "slot vivo com TTL máximo não pode cair"

    def s_spent_at_check_amostrado_antes_do_get():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            a, b = make_pool("A", "B")
            write_state(td, {a.hash_id: {"credits_spent": 100}, b.hash_id: {"credits_spent": 7}})
            env = {"TAVILY_API_KEY_A": a.key, "TAVILY_API_KEY_B": b.key}

            def spend_on_a(n):  # outro processo gasta n créditos em A
                other = make_pool("A")[0]
                s = PoolState.open(td)
                s.apply([other])
                other.credits_spent += n
                s.snapshot([other], now=T)

            state = PoolState.open(td)  # o status carrega o registo (A=100)…
            spend_on_a(5)                # …e antes do GET de A outra pesquisa gasta 5 (disco: 105)
            responses = iter([(200, REAL_USAGE), (200, REAL_USAGE)])

            def get(_url, headers, _timeout):
                reply = next(responses)
                if headers["Authorization"].endswith(b.key):
                    spend_on_a(3)  # durante o GET de B, mais 3 em A (depois do /usage de A)
                return reply

            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(True, 5.0, state=state, env=env, now=lambda: T, get=get)
            final = make_pool("A")
            PoolState.open(td).apply(final)
            assert final[0].credits_spent == 108, final[0].credits_spent
            assert effective_remaining(final[0], T) == 197, \
                f"os 5 anteriores já estão no /usage; os 3 posteriores descontam (obtido {effective_remaining(final[0], T)})"

    def s_refresh_mensagens_por_resultado():
        T = 1_800_000_000.0
        cases = ((OSError("sem rede"), "sem rede"), ((503, {}), "HTTP 503"),
                 ((200, None), "resposta inválida"), ((200, ["não", "é", "objeto"]), "resposta inválida"))
        for reply, label in cases:
            with tempfile.TemporaryDirectory() as td:
                k = make_pool("A")[0]
                write_state(td, {k.hash_id: {"usage": {"remaining": 900, "limit": 1000,
                                                       "checked_at": T - 7200, "spent_at_check": 0}}})
                get, _c = fake_get([reply])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cmd_status(False, 5.0, state=PoolState.open(td), env={"TAVILY_API_KEY_A": k.key},
                               now=lambda: T, get=get)
                text = out.getvalue()
                assert "Saldo refrescado agora" not in text and f"({label})" in text, f"{label}: {text!r}"
                usage = PoolState.open(td).data["keys"][k.hash_id]["usage"]
                assert usage["remaining"] == 900, f"{label}: o saldo conhecido não pode ser apagado ({usage})"

    def s_check_resposta_invalida_nao_valida():
        with tempfile.TemporaryDirectory() as td:
            k = make_pool("A")[0]
            write_state(td, {k.hash_id: {"status": "REVOKED"}})
            get, _c = fake_get([(200, None)])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cmd_status(True, 5.0, state=PoolState.open(td), env={"TAVILY_API_KEY_A": k.key},
                           now=lambda: 1_800_000_000.0, get=get)
            assert "resposta inválida do /usage" in out.getvalue() and "VÁLIDA" not in out.getvalue()
            assert PoolState.open(td).data["keys"][k.hash_id]["status"] == "REVOKED", "sem dados, sem autoridade"

    def s_get_com_cabecalho_invalido():
        try:
            http_get_json("http://127.0.0.1:9/", {"Authorization": "Bearer tvly-dev-AAAA\ntvly-dev-BBBB"}, 0.5)
            raise AssertionError("devia levantar OSError")
        except OSError as exc:
            assert "pedido HTTP inválido" in str(exc) and "AAAA" not in str(exc), str(exc)

    def s_formato_de_chave():
        for bad in ("tvly-dév-xxxxxxxxxx", "tvly-\xa0xxxxxxxxxx", "tvly dev xxxxxxxxxx", "'tvly-dev-xxxxxxx'",
                    "tvly-dev-xxx\\xxxx", "tvly\u200bdevxxxxxxx", "curta"):
            assert not valid_key_format(bad), f"{bad!r} devia ser recusada"
        for good in ("tvly-dev-AbC123xyz", "tvly-AbC_123-xyz.9", "tvly-dev-" + "x" * 32):
            assert valid_key_format(good), f"{good!r} é um formato legítimo"

    def s_aviso_de_chave_invalida_na_pesquisa():
        good = make_pool("B")[0]
        bad_value = "tvly-dev-COM ESPACO-SEGREDO123"

        def fake_search(*_a, **_k):
            return {"query": "q", "answer": "a", "results": [], "meta": {"truncated": False, "keyless_used": False}}

        with patched_env({"TAVILY_API_KEY_B": good.key, "TAVILY_API_KEY_C": bad_value}), \
                patched_run_search(fake_search):
            code, _out, err = run_main(["search", "q", "--no-state"])
        assert code == 0 and "Aviso: TAVILY_API_KEY_C ignorada" in err, err
        assert "SEGREDO123" not in err, "o aviso nunca mostra o valor"

    def s_live_denuncia_chave_invalida():
        good = make_pool("A")[0]
        env = {"TAVILY_API_KEY_A": good.key, "TAVILY_API_KEY_B": "\u201ctvly-dev-ASPASTIPOGRAFICAS\u201d"}
        get, _g = fake_get([(200, REAL_USAGE)])
        real_ok = (200, {"query": "q", "results": [{"title": "t", "url": "https://exemplo.pt/", "content": "c",
                                                    "score": 1}], "usage": {"credits": 1}}, None)
        post, calls = transport([real_ok])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_selftest_live(1.0, env=env, get=get, post=post)
        text = out.getvalue()
        assert code == 1 and "FAIL  formato TAVILY_API_KEY_B" in text and "2/3" in text, text
        assert len(calls) == 1 and "ASPASTIPOGRAFICAS" not in text

    def s_refresh_com_prazo_total():
        k = make_pool("A")[0]

        def slow_get(_url, _headers, _timeout):
            time.sleep(1.0)  # DNS/ligação a pendurar para lá do timeout do socket
            return (200, REAL_USAGE)

        started = time.monotonic()
        outcomes = auto_refresh_usage([k], get=slow_get, now=1_800_000_000.0, timeout=0.1)
        elapsed = time.monotonic() - started
        assert outcomes == [(k, "sem rede")] and elapsed < 0.9, f"prazo total não respeitado ({elapsed:.2f}s)"

    def s_falhas_consecutivas_sobrevivem_ao_reset_so_como_delta():
        with tempfile.TemporaryDirectory() as td:
            b = make_pool("B")[0]
            write_state(td, {b.hash_id: {"failures": 6, "last_error": "HTTP 429"}})
            for outcome in ("sucesso", "429"):
                stale = PoolState.open(td)
                pool = make_pool("B")
                stale.apply(pool)  # pesquisa em curso carregou failures=6
                PoolState.open(td).reset()  # entretanto: `status --reset-state`
                if outcome == "sucesso":
                    pool[0].failures, pool[0].consec_base = 0, 0
                    pool[0].failures_reset = pool[0].dirty = True
                else:
                    mark_rate_limited(pool[0], None, 0.0, lambda: 0.0)
                    pool[0].last_error, pool[0].error_observed = "HTTP 429", True
                pool[0].total_requests += 1
                stale.snapshot(pool, now=1.0)
                entry = PoolState.open(td).data["keys"][b.hash_id]
                expected = (0, "") if outcome == "sucesso" else (1, "HTTP 429")
                assert (entry["failures"], entry["last_error"]) == expected, f"{outcome}: {entry}"
                write_state(td, {b.hash_id: {"failures": 6, "last_error": "HTTP 429"}})

    def s_spent_at_check_no_refrescamento_automatico():
        T = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as td:
            a = make_pool("A")[0]
            write_state(td, {a.hash_id: {"credits_spent": 100}})
            state = PoolState.open(td)  # o status carrega A=100…
            other = make_pool("A")[0]
            s2 = PoolState.open(td)
            s2.apply([other])
            other.credits_spent += 5  # …e outra pesquisa gasta 5 antes da consulta
            s2.snapshot([other], now=T)
            get, _c = fake_get([(200, REAL_USAGE)])
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_status(False, 5.0, state=state, env={"TAVILY_API_KEY_A": a.key}, now=lambda: T, get=get)
            usage = PoolState.open(td).data["keys"][a.hash_id]["usage"]
            assert usage["spent_at_check"] == 105, f"reserva alinha os créditos com o disco ({usage})"

    def s_usage_mais_recente_vence_entre_dois_refrescamentos():
        with tempfile.TemporaryDirectory() as td:
            k_old, k_new = make_pool("A")[0], make_pool("A")[0]
            s_old, s_new = PoolState.open(td), PoolState.open(td)
            s_old.apply([k_old])
            s_new.apply([k_new])
            older = json.loads(json.dumps(REAL_USAGE))
            older["account"]["plan_usage"] = 100
            get_old, _a = fake_get([(200, older)])
            get_new, _b = fake_get([(200, REAL_USAGE)])
            refresh_usage(k_old, get=get_old, timeout=1.0, now=200.0)
            refresh_usage(k_new, get=get_new, timeout=1.0, now=300.0)
            s_new.snapshot([k_new], now=300.0)  # a mais recente grava primeiro…
            s_old.snapshot([k_old], now=301.0)  # …a mais antiga depois
            usage = PoolState.open(td).data["keys"][k_new.hash_id]["usage"]
            assert usage["checked_at"] == 300.0 and usage["remaining"] == 200, usage

    def s_keyless_desativado_sozinho():
        post, calls = transport([(401, {}, None), OK])
        try:
            run_search("q", pool=make_pool("A"), post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                       now=lambda: 0.0, keyless=False)
            raise AssertionError("devia falhar sem recorrer ao keyless")
        except SkillError as exc:
            assert "keyless desativada" in exc.problem, exc.problem
        assert len(calls) == 1, "keyless=False nunca faz o pedido keyless"

    def s_max_attempts_sozinho():
        post, calls = transport([(503, {}, None), OK, OK])
        try:
            run_search("q", pool=make_pool("A"), post=post, sleep=lambda _s: None, rng=lambda: 0.0,
                       now=lambda: 0.0, max_attempts=1)
            raise AssertionError("devia parar ao fim de 1 tentativa")
        except SkillError:
            pass
        assert len(calls) == 1, "max_attempts=1 → exatamente 1 pedido (nem repetição nem keyless)"

    def s_conta_revivida_no_fim_da_passagem():
        pool = make_pool("A")
        clock = [0.0]
        _sleeps, fake_sleep = recording_sleep(clock)
        calls = []
        replies = iter([(429, {}, "0"), (503, {}, None), OK])

        def post(_url, headers, _body, _t):
            calls.append(headers["Authorization"][-1] if "Authorization" in headers else "keyless")
            clock[0] += 1.0  # cada pedido demora 1 s: o cooldown de A expira durante o keyless
            return next(replies)

        result = run_search("q", pool=pool, post=post, sleep=fake_sleep, rng=lambda: 0.0, now=lambda: clock[0])
        assert calls == ["A", "keyless", "A"] and result["meta"]["attempts"] == 3, calls

    def s_numeros_gigantes_nos_helpers():
        assert _as_float(10 ** 400) == 0.0 and _as_number(10 ** 400) is None and _as_int(10 ** 400) == 0
        assert _as_float(float("nan"), 7.0) == 7.0 and _as_number(float("inf")) is None
        assert _as_number("12") == 12 and _as_number(True) is None

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
        ("Retry-After com data HTTP (e delta/lixo) — independente do locale", s_retry_after_data_http),
        ("Retry-After absurdo limitado a 1 h (não mata a chave por anos)", s_retry_after_absurdo_limitado),
        ("parse_usage com payload real de /usage (plan_limit numérico)", s_parse_usage_payload_real),
        ("parse_usage com plan_limit nulo, teto por chave e payload inválido", s_parse_usage_sem_teto_e_lixo),
        ("resolve_max_inflight: CLI > ambiente > predefinição (2); 0 = sem teto", s_resolve_max_inflight_prioridade),
        ("teto 0: conta cheia serve e nenhum slot alheio é libertado", s_teto_zero_nao_rouba_slots),
        ("load_keys: alias TAVILY_API_KEY = _A não duplica a rotação", s_load_keys_alias_sem_duplicar),
        ("load_keys: ordem determinística e dedup por valor", s_load_keys_ordem_deterministica),
        ("saldo: a conta com mais créditos restantes serve primeiro", s_saldo_maior_serve_primeiro),
        ("saldo: estimativa desconta créditos e o cursor desempata (B→A→B)", s_saldo_igual_alterna_com_cursor),
        ("saldo: 5xx na conta preferida continua a rotacionar", s_saldo_falha_transitoria_ainda_rotaciona),
        ("créditos contados por usage.credits (include_usage) ou pela profundidade", s_creditos_contados_pela_resposta),
        ("status: saldo com > 60 min é refrescado; fresco não consulta", s_usage_refresca_quando_velho),
        ("status: sem rajadas a /usage (≥ 6 min entre consultas; REVOKED salta)", s_usage_sem_rajadas),
        ("status ponta a ponta: refresca, persiste e não repete", s_status_refresca_e_persiste),
        ("status: nome canónico _A com alias em nota (pool a 2)", s_status_nome_canonico_sem_duplicar),
        ("chave revogada não impede a espera pelo cooldown das outras", s_revogada_nao_impede_espera_de_cooldown),
        ("resposta HTTP malformada é tratada como falha de rede", s_resposta_http_malformada_e_falha_de_rede),
        ("registo corrompido é tratado como ausente (sem rebentar)", s_registo_corrompido_nao_rebenta),
        ("erros de CLI seguem Erro:/Solução: (exit 2, sem traceback)", s_cli_erros_seguem_o_contrato),
        ("falha interna inesperada: sem traceback nem segredo", s_falha_interna_sem_traceback_nem_segredo),
        ("--verbose também redige segredos", s_verbose_redige_segredos),
        ("orçamento negativo nunca inverte o corte do texto", s_orcamento_invalido_nunca_inverte_o_corte),
        ("SkillError em main passa pela redação", s_skillerror_em_main_redigida),
        ("argparse não ecoa segredos (valores do env e tokens tvly-)", s_argparse_redige_segredos),
        ("5xx isolado numa conta única: repete após backoff, sem keyless", s_falha_transitoria_isolada_repete_a_conta),
        ("429 'Retry-After: 0' não fura a ronda (A→B→A como na v0.2.1)", s_nova_ronda_nao_fura_round_robin),
        ("saldo: timeout na conta preferida continua a rotacionar", s_saldo_rede_na_preferida_rotaciona),
        ("pick_key: escalões de saldo (0, desconhecido, inferior, empate)", s_pick_key_escaloes_de_saldo),
        ("concorrência: a espera vai até --max-wait e só depois keyless", s_espera_de_concorrencia_usa_max_wait),
        ("falhas transitórias limitadas (2 por credencial) e esperas ≤ --max-wait", s_transitorias_limitadas),
        ("'Retry-After: 0' não gera rajada (piso de 0,5 s)", s_retry_after_zero_sem_rajada),
        ("backoff de 429 sem Retry-After escala; sucesso zera", s_backoff_429_escala),
        ("chave REVOKED com cooldown residual não provoca espera", s_revogada_com_cooldown_residual_nao_espera),
        ("pedido isolado (selftest --live): 1 pedido, sem keyless", s_live_isolado_um_pedido),
        ("spent_at_check com créditos já gastos", s_spent_at_check_com_creditos),
        ("credits_spent persiste e desconta entre invocações", s_creditos_persistem_entre_invocacoes),
        ("status --check: autoridade só nas verificadas; contadores intactos", s_status_check_autoritativo),
        ("merge dos campos novos: /usage recente vence, créditos por delta", s_merge_campos_novos),
        ("reserva de /usage atómica entre processos (e só com registo)", s_reserva_usage_atomica),
        ("a pesquisa liberta o SEU slot in-flight (registo em ficheiro)", s_slot_proprio_libertado_pela_pesquisa),
        ("slots guardam a expiração: pedido longo vive, lixo no futuro cai", s_slot_ttl_e_horizonte),
        ("--reset-state --check não regrava o registo antigo", s_reset_check_nao_ressuscita),
        ("vista antiga de outro processo não desfaz reset nem --check", s_vista_antiga_nao_desfaz_reset_nem_check),
        ("apagar o ficheiro de registo limpa-o de facto", s_registo_apagado_nao_ressuscita),
        ("registo com NaN/Infinity/inteiros gigantes/surrogates não rebenta", s_registo_com_nan_infinito_e_surrogates),
        ("saldo de um mês anterior não guia a rotação (reset mensal)", s_saldo_do_mes_anterior_ignorado),
        ("checked_at no futuro é inválido (stale e perde o merge)", s_checked_at_no_futuro),
        ("--reset-state funciona mesmo sem chaves", s_reset_sem_chaves),
        ("sem registo persistente não há refrescamento automático", s_refresh_sem_registo_persistente),
        ("refrescamento falhado não é anunciado como sucesso", s_refresh_falhado_nao_anuncia_sucesso),
        ("refrescamento pára à 1.ª falha de rede e usa timeout curto", s_refresh_para_na_primeira_falha_de_rede),
        ("chave malformada fica de fora, sem crash nem segredo", s_chave_malformada_fica_de_fora),
        ("redação: chave-prefixo não deixa escapar o resto", s_redacao_prefixo_e_ordem),
        ("surrogates na resposta da API não partem a saída", s_surrogate_na_resposta),
        ("consulta com bytes não-UTF-8 → erro instrutivo, sem pedido", s_consulta_nao_utf8),
        ("stdout sem UTF-8 (ascii): status e --help sem traceback", s_stdout_sem_utf8),
        ("leitor fecha o pipe: exit 0, sem 'Exception ignored'", s_pipe_fechado_pelo_leitor),
        ("Retry-After: zona numérica, minúsculas e locale não-C", s_retry_after_formatos),
        ("registo aninhado ao absurdo (RecursionError) não parte nada", s_registo_aninhado_ao_absurdo),
        ("espera longa por concorrência vai até --max-wait e acaba no keyless", s_espera_longa_chega_ao_keyless),
        ("slot com TTL máximo sobrevive a relógio ligeiramente atrasado", s_slot_com_ttl_maximo_sobrevive),
        ("spent_at_check amostrado antes do GET (escala partilhada)", s_spent_at_check_amostrado_antes_do_get),
        ("status: rede/5xx/200 sem JSON nunca são 'saldo refrescado' nem apagam o saldo", s_refresh_mensagens_por_resultado),
        ("--check com 200 sem dados não valida nem impõe estado", s_check_resposta_invalida_nao_valida),
        ("GET com cabeçalho inválido vira falha tratada, sem a chave", s_get_com_cabecalho_invalido),
        ("formato de chave: latin-1/espaços/aspas/ZWSP recusados; tvly- legítimas aceites", s_formato_de_chave),
        ("pesquisa avisa em stderr da chave com formato inválido (sem o valor)", s_aviso_de_chave_invalida_na_pesquisa),
        ("selftest --live denuncia a chave com formato inválido (sem falso verde)", s_live_denuncia_chave_invalida),
        ("refrescamento automático com prazo total (DNS/vários endereços)", s_refresh_com_prazo_total),
        ("falhas consecutivas/último erro sobrevivem a um reset só como delta", s_falhas_consecutivas_sobrevivem_ao_reset_so_como_delta),
        ("refrescamento automático alinha spent_at_check com o disco", s_spent_at_check_no_refrescamento_automatico),
        ("dois refrescamentos concorrentes: vence o /usage mais recente", s_usage_mais_recente_vence_entre_dois_refrescamentos),
        ("keyless=False: nunca faz o pedido keyless", s_keyless_desativado_sozinho),
        ("max_attempts=1: exatamente 1 pedido", s_max_attempts_sozinho),
        ("conta revivida no fim da passagem ganha nova passagem", s_conta_revivida_no_fim_da_passagem),
        ("helpers numéricos: inteiros gigantes, NaN e Infinity", s_numeros_gigantes_nos_helpers),
    ]
    for name, fn in scenarios:
        scenario(name, fn)
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} cenários OK")
    return 1 if failures else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def resolve_max_inflight(cli_value: int | None, env: dict[str, str] | None = None) -> int:
    """Teto de requests simultâneas por conta: CLI > TAVILY_MAX_INFLIGHT_PER_KEY >
    predefinição (2). `0` = sem teto; valor de ambiente inválido é ignorado."""
    if cli_value is not None:
        return max(0, int(cli_value))
    env = os.environ if env is None else env
    env_value = (env.get("TAVILY_MAX_INFLIGHT_PER_KEY") or "").strip()
    if env_value.isascii() and env_value.isdigit():
        return int(env_value)
    return DEFAULT_MAX_INFLIGHT


class _Parser(argparse.ArgumentParser):
    """Erros de argumentos também seguem o contrato `Erro:` / `Solução:` (exit 2)
    — e passam pela redação: o argparse ecoa o valor recusado, que pode ser
    uma chave (ex.: `--api-key "$TAVILY_API_KEY_A"`)."""

    def error(self, message: str):  # noqa: D401 — assinatura do argparse
        self.exit(2, f"Erro: argumentos inválidos — {redact_all(message)}.\n"
                     f"Solução: veja `{self.prog} --help` e repita a invocação corrigida.\n")


def _number_arg(kind, minimum: float, maximum: float, *, strict: bool = False):
    def parse(text: str):
        try:
            value = kind(text)
        except (ValueError, OverflowError):
            raise argparse.ArgumentTypeError(f"'{text}' não é um número válido") from None
        if isinstance(value, float) and not math.isfinite(value):
            raise argparse.ArgumentTypeError(f"'{text}' não é um número finito")
        if value < minimum or (strict and value == minimum):
            sign = ">" if strict else "≥"
            raise argparse.ArgumentTypeError(f"'{text}' tem de ser {sign} {minimum:g}")
        if value > maximum:
            raise argparse.ArgumentTypeError(f"'{text}' tem de ser ≤ {maximum:g}")
        return value
    return parse


def cmd_selftest_live(timeout: float = DEFAULT_TIMEOUT, env: dict[str, str] | None = None,
                      get=None, post=None) -> int:
    """Integração REAL (gasta créditos): por conta, 1 consulta a /usage (grátis)
    + 1 pesquisa `ultra-fast` com 1 resultado (≈1 crédito) SÓ com essa conta —
    exatamente 1 pedido, sem rotação, sem retry e sem keyless, para que cada
    conta seja provada isoladamente. Usa registo em memória: não mexe no
    registo persistente."""
    env = os.environ if env is None else env
    get = http_get_json if get is None else get
    pool = load_keys(env)
    invalid = invalid_key_vars(env)
    remember_secrets(secret_values(env))
    if not pool and not invalid:
        raise SkillError(
            "o selftest --live precisa de pelo menos uma chave Tavily válida no terminal.",
            'declare export TAVILY_API_KEY_A="tvly-…" e repita, ou corra só o selftest offline (sem --live).',
        )
    print(f"\nselftest --live — pipeline real ({len(pool)} conta(s); ≈1 crédito por conta + 1 consulta /usage):")
    failures = len(invalid)
    for name in invalid:  # uma conta que não pode ser usada nunca é um verde silencioso
        print(f"  FAIL  formato {name}: valor inválido (espaços, aspas, quebras de linha ou não-ASCII) "
              "— corrija a variável")
    for k in pool:
        label = f"{k.env_name} {k.ref}"
        try:
            kind, status = refresh_usage(k, get=get, timeout=timeout, now=time.time())
            if kind == "success":
                u = k.usage
                print(f"  PASS  /usage {label}: plano {u['plan']} · usados {u['used']}"
                      f"/{u['limit'] if u['limit'] is not None else '∞'}")
            else:
                failures += 1
                print(f"  FAIL  /usage {label}: HTTP {status}")
        except OSError as exc:
            failures += 1
            print(redact(f"  FAIL  /usage {label}: sem resposta ({exc})", _SECRETS))
        probe = KeyMeta(k.env_name, k.key)
        try:
            result = run_search("Tavily search API", depth="ultra-fast", max_results=1, include_answer=False,
                                timeout=timeout, max_wait=0.0, pool=[probe], state=PoolState.memory(),
                                keyless=False, max_attempts=1, post=post)
            urls = [r["url"] for r in result["results"] if r["url"].startswith("http")]
            if urls:
                print(f"  PASS  search {label}: {len(urls)} fonte(s) · {probe.credits_spent} crédito(s) gasto(s)")
            else:
                failures += 1
                print(f"  FAIL  search {label}: resposta sem fontes")
        except SkillError:
            failures += 1
            print(f"  FAIL  search {label}: {probe.last_error or 'falhou'}")
    checks = 2 * len(pool) + len(invalid)
    print(f"\n{checks - failures}/{checks} verificações ao vivo OK")
    return 1 if failures else 0


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="tavily.py",
        description="Pesquisa web Tavily com rotação automática de chaves (o chamador nunca vê erros de rotação).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    timeout_arg = _number_arg(float, 0.0, MAX_TIMEOUT_S, strict=True)
    wait_arg = _number_arg(float, 0.0, MAX_WAIT_S)
    bytes_arg = _number_arg(int, 0, MAX_BYTES_CAP)
    inflight_arg = _number_arg(int, 0, MAX_INFLIGHT_CAP)

    p_search = sub.add_parser("search", help="pesquisa na web com rotação transparente")
    p_search.add_argument("query", help="texto a pesquisar (recomendado < 1500 caracteres)")
    p_search.add_argument("--json", action="store_true", help="saída JSON estruturada (para citar)")
    p_search.add_argument("--depth", default="basic", choices=["ultra-fast", "fast", "basic", "advanced"])
    p_search.add_argument("--max-results", type=int, default=5, help="1..10 (fora do intervalo é ajustado)")
    p_search.add_argument("--topic", default="general", choices=["general", "news"])
    p_search.add_argument("--no-answer", action="store_true", help="não pedir a resposta sumarizada")
    p_search.add_argument("--timeout", type=timeout_arg, default=DEFAULT_TIMEOUT,
                          help=f"timeout por tentativa em s (0 < t ≤ {MAX_TIMEOUT_S:g})")
    p_search.add_argument("--max-wait", type=wait_arg, default=DEFAULT_MAX_WAIT,
                          help="espera máx. total por cooldown/concorrência/backoff antes de desistir (s)")
    p_search.add_argument("--max-bytes", type=bytes_arg, default=DEFAULT_MAX_BYTES,
                          help="orçamento de texto na saída")
    p_search.add_argument("--max-inflight-per-key", type=inflight_arg, default=None,
                          help="máx. de requests simultâneas por conta (0 = sem teto; predef. 2 ou TAVILY_MAX_INFLIGHT_PER_KEY)")
    p_search.add_argument("--no-state", action="store_true", help="não persistir o registo do pool (modo efémero)")
    p_search.add_argument("--state-dir", default=None,
                          help="diretório do registo (predef.: $TAVILY_STATE_DIR ou ~/.local/state/tavily-agent-skill)")
    p_search.add_argument("--verbose", action="store_true", help="traços de rotação em stderr (redigidos)")

    p_status = sub.add_parser("status", help="estado do pool de chaves (sem segredos)")
    p_status.add_argument("--check", action="store_true",
                          help="validação ao vivo via /usage (não gasta créditos de pesquisa; máx. 10 req/10 min)")
    p_status.add_argument("--no-refresh", action="store_true",
                          help="não refrescar sozinho o saldo registado há mais de 60 min (modo offline)")
    p_status.add_argument("--reset-state", action="store_true",
                          help="limpa o registo persistente (todas as chaves voltam a ser consideradas)")
    p_status.add_argument("--state-dir", default=None, help="diretório do registo (ver search)")
    p_status.add_argument("--timeout", type=timeout_arg, default=DEFAULT_TIMEOUT)
    p_status.add_argument("--verbose", action="store_true", help="diagnóstico em stderr (redigido)")

    p_selftest = sub.add_parser("selftest", help="verificação determinística da máquina de rotação (offline)")
    p_selftest.add_argument("--live", action="store_true",
                            help="depois do offline, prova o pipeline REAL: 1 pesquisa ultra-fast + 1 /usage por conta "
                                 "(≈1 crédito por conta)")
    p_selftest.add_argument("--timeout", type=timeout_arg, default=DEFAULT_TIMEOUT)
    return parser


def _run(argv: list[str] | None) -> int:
    global VERBOSITY
    args = _build_parser().parse_args(argv)
    VERBOSITY = bool(getattr(args, "verbose", False))

    try:
        if args.command == "selftest":
            code = cmd_selftest()
            if args.live:
                code = max(code, cmd_selftest_live(args.timeout))
            return code
        if args.command == "status":
            state = PoolState.open(args.state_dir)
            return cmd_status(args.check, args.timeout, state=state,
                              reset_state=args.reset_state,
                              max_inflight=resolve_max_inflight(None),
                              refresh=not args.no_refresh)

        pool = load_keys()
        for name in invalid_key_vars():
            print(f"Aviso: {name} ignorada — formato de chave inválido (espaços, aspas, quebras de linha ou "
                  f"caracteres fora de ASCII); corrija o valor da variável.", file=sys.stderr)
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
            print(redact(json.dumps(result, ensure_ascii=False, indent=2), _SECRETS))
        else:
            sys.stdout.write(render_text(result, pool))
        return 0
    except BrokenPipeError:
        raise  # tratado em main(): o leitor fechou a saída
    except SkillError as exc:
        print(redact_all(str(exc)), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Erro: interrompido pelo utilizador.\nSolução: invoque novamente quando quiser.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — rede de segurança: o contrato proíbe tracebacks
        if VERBOSITY:
            print(redact_all(traceback.format_exc()), file=sys.stderr)
        print(f"Erro: falha interna inesperada ({type(exc).__name__}).\n"
              "Solução: repita a invocação; se persistir, corra `tavily.py selftest` e repita com --verbose "
              "para o diagnóstico (sem segredos).", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada. Garante o contrato também nas bordas do processo:
    stdout/stderr que não aceitam UTF-8 substituem o que não conseguem
    codificar (nunca rebentam depois de a pesquisa ter gasto créditos) e um
    leitor que fecha o pipe (`| head`) não produz traceback."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # não é um TextIOWrapper (ex.: StringIO nos testes)
    remember_secrets(secret_values())
    code = None
    try:
        try:
            code = _run(argv)
        except SystemExit as exc:  # argparse: --help (0) ou argumentos inválidos (2)
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        try:  # o interpretador volta a fazer flush à saída: aponta o stdout para /dev/null
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError, AttributeError):
            pass
        if code is not None:
            return code  # o comando terminou; só o flush final encontrou o pipe fechado
        try:
            print("Erro: a saída foi fechada pelo leitor antes do fim.\n"
                  "Solução: repita sem cortar a saída (ex.: sem `| head`) se precisar dela completa.",
                  file=sys.stderr)
        except (OSError, ValueError):
            pass
        return 141


if __name__ == "__main__":
    sys.exit(main())
