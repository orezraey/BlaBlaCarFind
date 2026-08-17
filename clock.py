# -*- coding: utf-8 -*-
"""Referencia de data e hora do bot.

Centraliza a resolucao do fuso horario para que "hoje" signifique a mesma coisa
em todo o projeto. Sem isso, `date.today()` seguiria o fuso do host e um servidor
em UTC trataria as 21h de Brasilia como o dia seguinte -- adiantando as datas
oferecidas no cadastro e expirando rotas um dia antes da viagem.

Por padrao usa o fuso do sistema. A variavel TIMEZONE aceita um nome IANA
(ex.: America/Sao_Paulo) e tem precedencia, para o caso de o bot rodar em um
servidor cujo relogio esta em outro fuso que nao o das viagens.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("blablacarfind.clock")

TIMEZONE_ENV = "TIMEZONE"

_configured: tzinfo | None = None
_resolved = False


def _system_timezone() -> tzinfo:
    """Fuso do sistema, reavaliado a cada chamada.

    `astimezone()` sem argumento devolve o deslocamento vigente agora, entao
    resolver na hora do uso mantem o horario de verao correto.
    """
    tz = datetime.now().astimezone().tzinfo
    if tz is None:  # praticamente inalcancavel; mantem o retorno sempre valido
        raise RuntimeError("o sistema nao expos um fuso horario")
    return tz


def _resolve() -> tzinfo | None:
    """None significa "usar o fuso do sistema", resolvido a cada chamada."""
    global _configured, _resolved
    if _resolved:
        return _configured

    name = (os.environ.get(TIMEZONE_ENV) or "").strip()
    if name:
        try:
            _configured = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(
                "%s=%r não é um fuso horário válido; usando o fuso do sistema. "
                "Use um nome IANA, por exemplo America/Sao_Paulo.",
                TIMEZONE_ENV,
                name,
            )
            _configured = None
    _resolved = True
    return _configured


def reset_cache() -> None:
    """Forca nova leitura do ambiente. Usado em teste."""
    global _configured, _resolved
    _configured, _resolved = None, False


def timezone() -> tzinfo:
    return _resolve() or _system_timezone()


def now() -> datetime:
    """Agora, com fuso embutido."""
    return datetime.now(timezone())


def today() -> date:
    """Data corrente no fuso de referencia."""
    return now().date()


def timestamp() -> str:
    """Instante atual em ISO 8601 com deslocamento, para gravar no banco.

    O deslocamento explicito evita a ambiguidade de um horario solto, que nao
    diz se foi anotado em UTC ou no horario local.
    """
    return now().isoformat(timespec="seconds")


def label() -> str:
    """Descricao legivel do fuso em uso, para o log de inicializacao."""
    name = (os.environ.get(TIMEZONE_ENV) or "").strip()
    offset = now().strftime("%z")
    readable = f"UTC{offset[:3]}:{offset[3:]}" if offset else "sem deslocamento"
    if _resolve() is not None:
        return f"{name} ({readable})"
    return f"{_system_timezone()} ({readable}, detectado do sistema)"
