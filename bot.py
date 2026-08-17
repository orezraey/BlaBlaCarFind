# -*- coding: utf-8 -*-
"""Bot de Telegram que monitora caronas novas no BlaBlaCar Brasil.

O usuario registra origem, destino e data; a cada ciclo o bot busca a rota e
avisa quando surge uma carona que ainda nao tinha aparecido, ja com os sinais
de confianca do motorista (nota, taxa de cancelamento, SuperDriver).

Rodar:
    set TELEGRAM_BOT_TOKEN=...
    python bot.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import clock
import storage
from blablacar import BlaBlaCar, BlaBlaCarError, Place, RideDetails, Trip

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("blablacarfind")

ENV_FILE = Path(__file__).with_name(".env")

# Le o .env que fica ao lado do codigo. `override=False` faz variaveis ja
# definidas no ambiente vencerem o arquivo -- assim producao (systemd, Docker)
# ignora o .env sem precisar apaga-lo.
try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE, override=False)
except ImportError:  # sem python-dotenv, so valem as variaveis de ambiente
    pass


def env_int(name: str, default: int) -> int:
    """Le um inteiro do ambiente tolerando valor vazio ou invalido."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r não é um número; usando %d", name, raw, default)
        return default


POLL_MINUTES = env_int("POLL_MINUTES", 15)
# Pausa entre rotas dentro de um ciclo, para nao disparar rajadas contra a API.
DELAY_BETWEEN_WATCHES = 2.0

ASK_ORIGIN, ASK_DEST, ASK_DATE = range(3)

WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]

# Um cliente para o processo todo: reaproveita o token e a sessao TLS.
_client = BlaBlaCar()
# Serializa o acesso a API -- curl_cffi e sincrono e nao queremos paralelismo aqui.
_api_lock = asyncio.Lock()


async def api_call(func, *args, **kwargs):
    async with _api_lock:
        return await asyncio.to_thread(func, *args, **kwargs)


# ----------------------------------------------------------------- formatacao


def fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]} {d.day:02d}/{MONTHS[d.month - 1]}"


def cancellation_badge(driver) -> str:
    """Selo de risco. Sem historico e diferente de bom historico -- nao vira verde."""
    return {
        "NEVER": "🟢 Nunca cancela caronas",
        "RARELY": "🟢 Raramente cancela caronas",
        "SOMETIMES": "🟡 Cancela caronas às vezes",
        "OFTEN": "🔴 Cancela caronas com frequência",
        "ALWAYS": "🔴 Cancela caronas quase sempre",
    }.get(driver.cancellation_code, "⚪ Sem histórico de cancelamento")


def format_trip(trip: Trip, details: RideDetails | None, watch: storage.Watch) -> str:
    esc = html.escape
    lines = [
        "🚗 <b>Nova carona encontrada</b>",
        f"{esc(watch.origin_name)} → {esc(watch.dest_name)} · {fmt_date(watch.travel_date)}",
        "",
        f"🕐 <b>{esc(trip.departure_time)} → {esc(trip.arrival_time)}</b>  ({esc(trip.duration)})",
        f"📍 {esc(trip.departure_place)} → {esc(trip.arrival_place)}",
        f"💰 <b>{esc(trip.price)}</b>",
    ]
    if trip.overnight:
        lines.append("🌙 Viagem noturna")

    if details is None:
        lines += ["", "⚠️ Não consegui carregar os dados do motorista desta vez."]
        return "\n".join(lines)

    driver = details.driver
    lines += ["", f"👤 <b>{esc(driver.name or 'Motorista')}</b>"]

    trust = []
    if driver.rating_label and driver.rating_label != "0":
        trust.append(f"⭐ {esc(driver.rating_label)}")
    else:
        trust.append("⭐ sem avaliações")
    if driver.is_super_driver:
        trust.append("🏅 SuperDriver")
    if driver.is_verified:
        trust.append("✅ verificado")
    lines.append(" · ".join(trust))
    lines.append(cancellation_badge(driver))

    if driver.vehicle:
        lines.append(f"🚙 {esc(driver.vehicle)}")
    if details.approval_mode == "MANUAL":
        lines.append("⚠️ A reserva só vale depois que o motorista aprovar")
    if driver.message:
        note = driver.message.strip().replace("\n", " ")
        if len(note) > 180:
            note = note[:177] + "..."
        lines.append(f"\n💬 <i>{esc(note)}</i>")

    return "\n".join(lines)


def watch_to_places(watch: storage.Watch) -> tuple[Place, Place]:
    return (
        Place(watch.origin_place_id, watch.origin_name, watch.origin_lat, watch.origin_lng),
        Place(watch.dest_place_id, watch.dest_name, watch.dest_lat, watch.dest_lng),
    )


def search_watch(watch: storage.Watch) -> list[Trip]:
    origin, dest = watch_to_places(watch)
    return _client.search(
        origin, dest, watch.travel_date, seats=watch.seats, supply="CARPOOLING"
    )


# -------------------------------------------------------------------- comandos


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Olá! Eu monitoro caronas no BlaBlaCar e te aviso assim que aparecer uma nova.\n\n"
        "/monitorar — cadastrar uma rota\n"
        "/rotas — ver o que estou monitorando\n"
        "/checar — conferir agora, sem esperar o ciclo\n"
        "/parar — encerrar o monitoramento de uma rota\n\n"
        f"Eu confiro cada rota a cada {POLL_MINUTES} minutos."
    )


async def cmd_monitorar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("De qual cidade você vai sair?")
    return ASK_ORIGIN


async def _offer_places(update: Update, ctx: ContextTypes.DEFAULT_TYPE, prefix: str) -> int | None:
    query = (update.message.text or "").strip()
    if len(query) < 3:
        await update.message.reply_text("Escreve um pouco mais do nome da cidade.")
        return None
    try:
        suggestions = await api_call(_client.suggestions, query)
    except BlaBlaCarError as exc:
        log.warning("suggestions falhou: %s", exc)
        await update.message.reply_text("Não consegui consultar agora. Tenta de novo?")
        return None
    if not suggestions:
        await update.message.reply_text("Não achei essa cidade. Tenta escrever de outro jeito.")
        return None

    ctx.user_data[f"{prefix}_options"] = suggestions[:5]
    buttons = [
        [InlineKeyboardButton(
            (s.get("main_text") or "")[:40]
            + (f" — {s['secondary_text'][:30]}" if s.get("secondary_text") else ""),
            callback_data=f"{prefix}:{i}",
        )]
        for i, s in enumerate(suggestions[:5])
    ]
    await update.message.reply_text("Qual delas?", reply_markup=InlineKeyboardMarkup(buttons))
    return None


async def on_origin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await _offer_places(update, ctx, "origin")
    return ASK_ORIGIN


async def on_dest_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await _offer_places(update, ctx, "dest")
    return ASK_DEST


async def _resolve_choice(ctx: ContextTypes.DEFAULT_TYPE, prefix: str, index: int):
    """Sugestao -> Place. O geocode traz a coordenada, guardada como fallback."""
    option = ctx.user_data[f"{prefix}_options"][index]
    address = option.get("address") or option.get("main_text")
    place = await api_call(_client.geocode, address)
    # O place_id da sugestao e mais especifico que o do geocode textual.
    return Place(option.get("id") or place.place_id, option.get("main_text") or place.name,
                 place.latitude, place.longitude)


async def on_pick_origin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    place = await _resolve_choice(ctx, "origin", int(query.data.split(":")[1]))
    ctx.user_data["origin"] = place
    await query.edit_message_text(f"Origem: {place.name}")
    await query.message.reply_text("E para qual cidade?")
    return ASK_DEST


async def on_pick_dest(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    place = await _resolve_choice(ctx, "dest", int(query.data.split(":")[1]))
    ctx.user_data["dest"] = place
    await query.edit_message_text(f"Destino: {place.name}")

    today = clock.today()
    days = [today + timedelta(days=i) for i in range(1, 8)]
    buttons = [
        [InlineKeyboardButton(fmt_date(d.isoformat()), callback_data=f"date:{d.isoformat()}")]
        for d in days
    ]
    await query.message.reply_text(
        "Para qual data? Escolhe abaixo ou escreve no formato DD/MM/AAAA.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ASK_DATE


def parse_date(text: str) -> str | None:
    text = text.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


async def _create_watch(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, travel_date: str) -> str:
    origin: Place = ctx.user_data["origin"]
    dest: Place = ctx.user_data["dest"]
    watch_id = await asyncio.to_thread(
        storage.add_watch,
        chat_id,
        (origin.name, origin.place_id, origin.latitude, origin.longitude),
        (dest.name, dest.place_id, dest.latitude, dest.longitude),
        travel_date,
        1,
    )
    watch = await asyncio.to_thread(storage.get_watch, watch_id)

    # Baseline: as caronas que ja existem nao sao "novas". Sem isso, o primeiro
    # ciclo notificaria a rota inteira de uma vez.
    try:
        trips = await api_call(search_watch, watch)
        await asyncio.to_thread(
            storage.mark_seen, watch_id, [(t.natural_key, t.price) for t in trips]
        )
        found = len(trips)
    except BlaBlaCarError as exc:
        log.warning("baseline da rota %s falhou: %s", watch_id, exc)
        return (
            f"✅ Monitorando <b>{html.escape(origin.name)} → {html.escape(dest.name)}</b> "
            f"em {fmt_date(travel_date)} (rota #{watch_id}).\n"
            "Não consegui listar as caronas atuais agora, mas o monitoramento já começou."
        )

    existing = (
        "Nenhuma carona publicada ainda — te aviso na primeira que aparecer."
        if found == 0
        else f"Já existem {found} carona(s); vou avisar só das novas a partir de agora."
    )
    return (
        f"✅ Monitorando <b>{html.escape(origin.name)} → {html.escape(dest.name)}</b> "
        f"em {fmt_date(travel_date)} (rota #{watch_id}).\n{existing}"
    )


async def on_pick_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    travel_date = query.data.split(":", 1)[1]
    await query.edit_message_text(f"Data: {fmt_date(travel_date)}")
    msg = await _create_watch(query.message.chat_id, ctx, travel_date)
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)
    ctx.user_data.clear()
    return ConversationHandler.END


async def on_date_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    travel_date = parse_date(update.message.text or "")
    if not travel_date:
        await update.message.reply_text("Não entendi a data. Usa DD/MM/AAAA, por exemplo 22/08/2026.")
        return ASK_DATE
    if date.fromisoformat(travel_date) < clock.today():
        await update.message.reply_text("Essa data já passou. Escolhe uma data futura.")
        return ASK_DATE
    msg = await _create_watch(update.message.chat_id, ctx, travel_date)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("Cadastro cancelado.")
    return ConversationHandler.END


async def cmd_rotas(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    watches = await asyncio.to_thread(storage.list_watches, update.message.chat_id)
    if not watches:
        await update.message.reply_text("Você não tem nenhuma rota monitorada. Use /monitorar.")
        return
    lines = ["<b>Rotas monitoradas</b>", ""]
    for w in watches:
        n = await asyncio.to_thread(storage.seen_count, w.id)
        lines.append(
            f"#{w.id} — {html.escape(w.origin_name)} → {html.escape(w.dest_name)}\n"
            f"     {fmt_date(w.travel_date)} · {n} carona(s) já vista(s)"
        )
    lines.append("\nPara encerrar: /parar &lt;número&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_parar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.message.chat_id
    if not ctx.args:
        watches = await asyncio.to_thread(storage.list_watches, chat_id)
        if not watches:
            await update.message.reply_text("Você não tem rotas monitoradas.")
            return
        buttons = [
            [InlineKeyboardButton(f"#{w.id} — {w.label()}", callback_data=f"stop:{w.id}")]
            for w in watches
        ]
        await update.message.reply_text(
            "Qual rota você quer encerrar?", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return
    try:
        watch_id = int(ctx.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("Informe o número da rota, ex: /parar 3")
        return
    ok = await asyncio.to_thread(storage.stop_watch, watch_id, chat_id)
    await update.message.reply_text(
        f"Rota #{watch_id} encerrada." if ok else f"Não achei a rota #{watch_id}."
    )


async def cmd_checar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Forca um ciclo agora, sem esperar o intervalo. Util para testar."""
    chat_id = update.message.chat_id
    watches = await asyncio.to_thread(storage.list_watches, chat_id)
    if not watches:
        await update.message.reply_text("Você não tem rotas monitoradas. Use /monitorar.")
        return
    await update.message.reply_text(f"Conferindo {len(watches)} rota(s) agora...")
    found = 0
    for watch in watches:
        try:
            found += await check_watch(ctx.application, watch)
        except BlaBlaCarError as exc:
            log.warning("checagem manual da rota #%s falhou: %s", watch.id, exc)
            await update.message.reply_text(f"Rota #{watch.id}: não consegui consultar agora.")
        await asyncio.sleep(DELAY_BETWEEN_WATCHES)
    if found == 0:
        await update.message.reply_text("Nenhuma carona nova por enquanto.")


async def on_stop_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    watch_id = int(query.data.split(":")[1])
    ok = await asyncio.to_thread(storage.stop_watch, watch_id, query.message.chat_id)
    await query.edit_message_text(
        f"Rota #{watch_id} encerrada." if ok else f"Não achei a rota #{watch_id}."
    )


# ------------------------------------------------------------------ vigilancia


async def check_watch(app: Application, watch: storage.Watch) -> int:
    """Consulta a rota e notifica as caronas novas. Devolve quantas foram novas."""
    trips = await api_call(search_watch, watch)
    known = await asyncio.to_thread(storage.known_keys, watch.id)
    new_trips = [t for t in trips if t.natural_key not in known]

    if not new_trips:
        log.info("rota #%s: %d caronas, nenhuma nova", watch.id, len(trips))
        return 0

    log.info("rota #%s: %d nova(s) de %d", watch.id, len(new_trips), len(trips))
    delivered: list[Trip] = []
    for trip in new_trips:
        details = None
        try:
            # 1 request extra por carona nova -- e a unica fonte dos dados do motorista.
            details = await api_call(_client.ride_details, trip, watch.seats)
        except BlaBlaCarError as exc:
            log.warning("detalhe da carona falhou (rota #%s): %s", watch.id, exc)

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Ver no BlaBlaCar", url=trip.url)]]
        )
        try:
            await app.bot.send_message(
                chat_id=watch.chat_id,
                text=format_trip(trip, details, watch),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except Forbidden:
            # Usuario bloqueou o bot ou apagou o chat: insistir nao adianta.
            log.warning("chat %s inacessivel; encerrando rota #%s", watch.chat_id, watch.id)
            await asyncio.to_thread(storage.stop_watch, watch.id, watch.chat_id)
            return len(delivered)
        except Exception as exc:  # rede, rate limit do Telegram, indisponibilidade
            log.warning("falha ao notificar chat %s: %s", watch.chat_id, exc)
            continue  # nao marca como vista: tenta de novo no proximo ciclo
        delivered.append(trip)

    # Marca so o que foi realmente entregue, para uma falha de envio nao fazer a
    # carona sumir silenciosamente.
    await asyncio.to_thread(
        storage.mark_seen, watch.id, [(t.natural_key, t.price) for t in delivered]
    )
    return len(delivered)


async def poll_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    app = ctx.application

    for expired in await asyncio.to_thread(storage.expire_past_watches):
        try:
            await app.bot.send_message(
                chat_id=expired.chat_id,
                text=f"A data da rota #{expired.id} ({expired.label()}) passou. "
                     "Encerrei o monitoramento dela.",
            )
        except Exception as exc:
            log.warning("aviso de expiracao falhou: %s", exc)

    watches = await asyncio.to_thread(storage.active_watches)
    if not watches:
        return
    log.info("ciclo: %d rota(s) ativa(s)", len(watches))
    for i, watch in enumerate(watches):
        try:
            await check_watch(app, watch)
        except BlaBlaCarError as exc:
            log.warning("rota #%s falhou: %s", watch.id, exc)
        except Exception:
            log.exception("erro inesperado na rota #%s", watch.id)
        if i < len(watches) - 1:
            await asyncio.sleep(DELAY_BETWEEN_WATCHES)


def main() -> None:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN não definido.\n\n"
            "  1. copy .env.example .env\n"
            "  2. abra o .env e preencha TELEGRAM_BOT_TOKEN com o token do @BotFather\n"
            "  3. python bot.py\n\n"
            f"Arquivo esperado: {ENV_FILE}"
        )

    storage.init()
    app = Application.builder().token(token).build()

    conversation = ConversationHandler(
        entry_points=[CommandHandler("monitorar", cmd_monitorar)],
        states={
            ASK_ORIGIN: [
                CallbackQueryHandler(on_pick_origin, pattern=r"^origin:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_origin_text),
            ],
            ASK_DEST: [
                CallbackQueryHandler(on_pick_dest, pattern=r"^dest:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_dest_text),
            ],
            ASK_DATE: [
                CallbackQueryHandler(on_pick_date, pattern=r"^date:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_date_text),
            ],
        },
        fallbacks=[CommandHandler("cancelar", cmd_cancelar)],
    )

    app.add_handler(conversation)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ajuda", cmd_start))
    app.add_handler(CommandHandler("rotas", cmd_rotas))
    app.add_handler(CommandHandler("parar", cmd_parar))
    app.add_handler(CommandHandler("checar", cmd_checar))
    app.add_handler(CallbackQueryHandler(on_stop_button, pattern=r"^stop:\d+$"))

    app.job_queue.run_repeating(poll_job, interval=POLL_MINUTES * 60, first=30)

    log.info("fuso horário: %s", clock.label())
    log.info("bot no ar; ciclo a cada %d min", POLL_MINUTES)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
