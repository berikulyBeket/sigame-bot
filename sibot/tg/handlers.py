"""Обработчики Telegram: команды, кнопки, сообщения с ответами, загрузка пакетов."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    Message,
    User,
)

from ..config import Config
from ..game.session import Event, GameSession, Phase, Player
from ..library import Library
from ..registry import Registry
from ..siq import PackageError
from . import render as R
from .callbacks import AppealCb, ChoiceCb, InfoCb, JudgeCb, LobbyCb, NoopCb, PassCb, PickCb, PressCb, VoteCb

log = logging.getLogger(__name__)

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
MANAGERS = {ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR}
ANONYMOUS_ADMIN = 1087968824  # GroupAnonymousBot — так приходят сообщения анонимных админов


def display_name(user: User) -> str:
    name = user.full_name.strip() or (f"@{user.username}" if user.username else f"Игрок {user.id}")
    return R.cut(name, 32)


def deep_link(me: User, kind: str, gid: str) -> str:
    return f"https://t.me/{me.username}?start={kind}{gid}"


async def can_dm(bot: Bot, user_id: int, text: str) -> bool:
    """Бот может писать только тем, кто открывал с ним личку."""
    try:
        await bot.send_message(chat_id=user_id, text=text)
    except TelegramAPIError:
        return False
    return True


async def is_manager(bot: Bot, s: GameSession, user_id: int) -> bool:
    """Создатель игры, ведущий или админ чата."""
    if user_id == s.creator_id or (s.host is not None and s.host.id == user_id):
        return True
    try:
        member = await bot.get_chat_member(chat_id=s.chat_id, user_id=user_id)
    except TelegramAPIError:
        return False
    return member.status in MANAGERS


async def privacy_problem(bot: Bot, chat_id: int, me: User) -> bool:
    """True, если бот не увидит обычные сообщения с ответами (privacy mode и не админ)."""
    if me.can_read_all_group_messages:
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=me.id)
    except TelegramAPIError:
        return True
    return member.status not in MANAGERS


async def join_flow(bot: Bot, s: GameSession, user: User, me: User) -> tuple[str | None, str | None]:
    """Записать игрока. Возвращает (ошибка, ссылка на личку с ботом — если писать ему нельзя)."""
    if err := s.join_error(user.id):
        return err, None
    if not await can_dm(bot, user.id, R.dm_joined(s.chat_title)):
        return None, deep_link(me, "j", s.gid)
    return await s.join(user.id, display_name(user)), None


async def set_commands(bot: Bot) -> None:
    group = [
        BotCommand(command="newgame", description="Новая игра"),
        BotCommand(command="join", description="Войти в игру"),
        BotCommand(command="leave", description="Выйти из игры"),
        BotCommand(command="score", description="Счёт"),
        BotCommand(command="skip", description="Пропустить вопрос"),
        BotCommand(command="stop", description="Остановить игру"),
        BotCommand(command="help", description="Правила и команды"),
    ]
    private = [
        BotCommand(command="start", description="Начало"),
        BotCommand(command="help", description="Правила и команды"),
    ]
    await bot.set_my_commands(group, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())


# ----------------------------------------------------------------- команды


async def cmd_start_deep(message: Message, command: CommandObject, registry: Registry) -> None:
    """Переход по ссылке из лобби: t.me/bot?start=j<id> (играть) или h<id> (вести)."""
    payload = command.args or ""
    s = registry.get(payload[1:])
    user = message.from_user
    if s is None or user is None:
        await message.answer("Эта игра уже недоступна. Новая игра — командой /newgame в группе.")
        return
    if payload.startswith("h"):
        err = await s.set_host(user.id, display_name(user))
        await message.answer(R.dm_host(s.chat_title) if err is None else f"⚠️ {err}")
    else:
        err = await s.join(user.id, display_name(user))
        await message.answer(R.dm_joined(s.chat_title) if err is None else f"⚠️ {err}")


async def cmd_start(message: Message) -> None:
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(R.start_text())
    else:
        await message.answer("Новая игра — /newgame, правила — /help")


async def cmd_help(message: Message) -> None:
    await message.answer(R.HELP)


async def cmd_newgame(
    message: Message, bot: Bot, registry: Registry, library: Library, config: Config, me: User
) -> None:
    user = message.from_user
    if user is None or user.id == ANONYMOUS_ADMIN or message.sender_chat is not None:
        await message.reply("Создайте игру от своего имени, а не анонимно.")
        return
    existing = registry.by_chat(message.chat.id)
    if existing is not None:
        if existing.phase is Phase.LOBBY:
            await existing.repost_lobby()
        else:
            await message.reply("Здесь уже идёт игра. Счёт — /score, остановить — /stop.")
        return
    s = GameSession(
        bot=bot,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id if message.is_topic_message else None,
        chat_title=message.chat.title or "чат",
        creator_id=user.id,
        library=library,
        registry=registry,
        bot_username=me.username or "",
        max_players=config.max_players,
        max_upload_mb=config.max_upload_mb,
        time_scale=config.time_scale,
    )
    s.privacy_warning = await privacy_problem(bot, message.chat.id, me)
    registry.add(s)
    await s.open_lobby()


async def cmd_newgame_private(message: Message) -> None:
    await message.answer("Игра идёт в группе: добавьте меня в чат с друзьями и напишите там /newgame.")


async def cmd_join(message: Message, bot: Bot, registry: Registry, me: User) -> None:
    s = registry.by_chat(message.chat.id)
    if s is None or message.from_user is None:
        await message.reply("Игры нет. Создать — /newgame")
        return
    err, link = await join_flow(bot, s, message.from_user, me)
    if link:
        await message.reply(
            "Сначала откройте бота в личке — туда придут ставки и вопросы финала.",
            reply_markup=R.keyboard([[R.btn("🤖 Открыть бота", url=link)]]),
        )
    elif err:
        await message.reply(err)


async def cmd_leave(message: Message, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    if s is not None and message.from_user is not None and (err := await s.leave(message.from_user.id)):
        await message.reply(err)


async def cmd_stop(message: Message, bot: Bot, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    if s is None or message.from_user is None:
        await message.reply("Игра не запущена")
        return
    if not await is_manager(bot, s, message.from_user.id):
        await message.reply("Остановить игру может создатель, ведущий или админ чата")
        return
    await s.stop("по команде /stop")


async def cmd_score(message: Message, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    await message.reply(R.score_list(s) if s is not None else "Игра не запущена. Новая — /newgame")


async def cmd_skip(message: Message, bot: Bot, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    if s is None or message.from_user is None:
        return
    if not await is_manager(bot, s, message.from_user.id):
        await message.reply("Пропустить вопрос может ведущий, создатель или админ чата")
    elif not s.skip():
        await message.reply("Сейчас нечего пропускать")


def _target(message: Message, s: GameSession, args: list[str]) -> tuple[Player | None, list[str]]:
    """Игрок из реплая или по номеру из /score."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        uid = reply.from_user.id
        if s.host is not None and s.host.id == uid:
            return s.host, args
        return s.players.get(uid), args
    if args and args[0].isdigit():
        return s.player_by_index(int(args[0])), args[1:]
    return None, args


async def cmd_kick(message: Message, command: CommandObject, bot: Bot, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    if s is None or message.from_user is None or not await is_manager(bot, s, message.from_user.id):
        return
    target, _ = _target(message, s, (command.args or "").split())
    if target is None:
        await message.reply("Укажите номер игрока из /score или ответьте командой на его сообщение")
        return
    if err := await s.kick(target.id):
        await message.reply(err)


async def cmd_setscore(message: Message, command: CommandObject, bot: Bot, registry: Registry) -> None:
    s = registry.by_chat(message.chat.id)
    if s is None or message.from_user is None or not await is_manager(bot, s, message.from_user.id):
        return
    target, rest = _target(message, s, (command.args or "").split())
    value = rest[0].replace("−", "-") if rest else ""
    if target is None or not value.lstrip("+-").isdigit():
        await message.reply("Формат: /setscore N сумма (N — номер из /score) или реплаем: /setscore сумма")
        return
    if err := await s.set_score(target.id, int(value)):
        await message.reply(err)


async def on_document(message: Message, bot: Bot, registry: Registry, library: Library, config: Config) -> None:
    """Загрузка своего пакета .siq в лобби."""
    doc = message.document
    if doc is None or not (doc.file_name or "").casefold().endswith(".siq") or message.from_user is None:
        return
    s = registry.by_chat(message.chat.id)
    if s is None or s.phase is not Phase.LOBBY:
        await message.reply("Пакет загружается во время набора игроков: начните с /newgame")
        return
    if not await is_manager(bot, s, message.from_user.id):
        await message.reply("Сменить пакет может создатель игры или админ чата")
        return
    if doc.file_size and doc.file_size > config.max_upload_mb * 1024 * 1024:
        await message.reply(
            f"Файл больше {config.max_upload_mb} МБ — через облачный Bot API бот не может его скачать. "
            "Сожмите медиа в пакете или запустите бота с локальным Bot API (см. README)."
        )
        return
    status = await message.reply("⏳ Загружаю пакет…")
    path = library.upload_path(doc.file_unique_id)
    try:
        if not path.exists():
            part = path.with_suffix(".part")
            await bot.download(doc, destination=part, timeout=300)
            part.replace(path)
        entry = await library.add_upload(path, message.chat.id)
    except PackageError as e:
        path.unlink(missing_ok=True)
        await status.edit_text(f"❌ Это не пакет SIGame: {R.esc(e)}")
        return
    except (TelegramAPIError, OSError) as e:
        log.warning("Не удалось скачать пакет: %s", e)
        await status.edit_text("❌ Не удалось скачать файл. Попробуйте ещё раз.")
        return
    await s.set_package(entry)
    await status.edit_text(f"✅ Пакет «{R.esc(entry.title)}» загружен\n{R.package_summary(entry)}")


async def on_group_text(message: Message, registry: Registry) -> None:
    """Ответы на вопросы и ставки текстом в общем чате."""
    s = registry.by_chat(message.chat.id)
    user = message.from_user
    if s is None or s.phase is not Phase.PLAYING or user is None or message.sender_chat is not None:
        return
    if err := s.submit(Event("text", user.id, message.text, private=False)):
        await message.reply(err)  # например, ставка вне допустимого диапазона


async def on_private_text(message: Message, registry: Registry) -> None:
    """Ставки и тайные ответы в личке."""
    user = message.from_user
    if user is None:
        return
    reasons = []
    for s in registry.for_user(user.id):
        err = s.submit(Event("text", user.id, message.text, private=True))
        if err is None:
            return
        if err:
            reasons.append(err)
    if reasons:
        await message.answer(f"⚠️ {reasons[0]}")
    else:
        await message.answer(
            "Сейчас я ничего от вас не жду 🙂\nИгры начинаются в группах командой /newgame, правила — /help."
        )


# ------------------------------------------------------------------- кнопки


async def cb_lobby(cq: CallbackQuery, callback_data: LobbyCb, bot: Bot, registry: Registry, me: User) -> None:
    s = registry.get(callback_data.g)
    if s is None or s.phase is not Phase.LOBBY:
        await cq.answer("Этот набор уже закрыт")
        return
    user, action = cq.from_user, callback_data.a

    if action == "join":
        err, link = await join_flow(bot, s, user, me)
        if link:  # Telegram откроет личку с ботом; после «Старт» игрок попадёт в игру
            await cq.answer(url=link)
        else:
            await cq.answer(err or "Вы в игре!", show_alert=bool(err))
        return
    if action == "leave":
        err = await s.leave(user.id)
        await cq.answer(err or "Вы вышли из игры")
        return
    if action == "host":
        if err := s.host_error(user.id):
            await cq.answer(err, show_alert=True)
        elif not await can_dm(bot, user.id, R.dm_host(s.chat_title)):
            await cq.answer(url=deep_link(me, "h", s.gid))
        else:
            err = await s.set_host(user.id, display_name(user))
            await cq.answer(err or "Вы ведущий!")
        return

    if not await is_manager(bot, s, user.id):
        await cq.answer("Это может создатель игры или админ чата", show_alert=True)
        return
    match action:
        case "unhost":
            await s.drop_host()
            await cq.answer("Теперь ведущий — бот")
        case "packs":
            await s.show_packs(True)
            await cq.answer()
        case "back":
            await s.show_packs(False)
            await cq.answer()
        case "pack":
            err = await s.choose_package(callback_data.v)
            await cq.answer(err or "Пакет выбран")
        case "tempo":
            await s.cycle_tempo()
            await cq.answer()
        case "appeals":
            await s.toggle_appeals()
            await cq.answer()
        case "start":
            err = await s.start()
            await cq.answer(err or "Поехали!", show_alert=bool(err))
        case "cancel":
            await s.stop("отменена")
            await cq.answer("Игра отменена")
        case _:
            await cq.answer()


async def _submit(cq: CallbackQuery, registry: Registry, gid: str, ev: Event, ok_text: str | None = None) -> None:
    s = registry.get(gid)
    if s is None:
        await cq.answer("Игра уже закончилась")
        return
    err = s.submit(ev)
    await cq.answer(ok_text if err is None else err or None)


def _private(cq: CallbackQuery) -> bool:
    return cq.message is not None and cq.message.chat.type == ChatType.PRIVATE


async def cb_pick(cq: CallbackQuery, callback_data: PickCb, registry: Registry) -> None:
    ev = Event("pick", cq.from_user.id, (callback_data.t, callback_data.q), step=callback_data.n)
    await _submit(cq, registry, callback_data.g, ev)


async def cb_press(cq: CallbackQuery, callback_data: PressCb, registry: Registry) -> None:
    ev = Event("press", cq.from_user.id, step=callback_data.n)
    await _submit(cq, registry, callback_data.g, ev, "🎤 Вы отвечаете! Пишите ответ в чат")


async def cb_pass(cq: CallbackQuery, callback_data: PassCb, registry: Registry) -> None:
    ev = Event("pass", cq.from_user.id, step=callback_data.n)
    await _submit(cq, registry, callback_data.g, ev, "🙅 Вы пасуете. Передумаете — жмите «Ответить!»")


async def cb_choice(cq: CallbackQuery, callback_data: ChoiceCb, registry: Registry) -> None:
    ev = Event(callback_data.k, cq.from_user.id, callback_data.v, step=callback_data.n, private=_private(cq))
    ok_text = {"bet": "Ставка принята", "opt": "Ответ принят"}.get(callback_data.k)
    await _submit(cq, registry, callback_data.g, ev, ok_text)


async def cb_judge(cq: CallbackQuery, callback_data: JudgeCb, registry: Registry) -> None:
    ev = Event("judge", cq.from_user.id, (callback_data.u, callback_data.v), step=callback_data.n, private=True)
    await _submit(cq, registry, callback_data.g, ev, "Принято")


async def cb_info(cq: CallbackQuery, callback_data: InfoCb, registry: Registry) -> None:
    s = registry.get(callback_data.g)
    text = s.theme_info(callback_data.r, callback_data.t) if s is not None else "Игра уже закончилась"
    await cq.answer(text, show_alert=True)


async def cb_appeal(cq: CallbackQuery, callback_data: AppealCb, registry: Registry) -> None:
    s = registry.get(callback_data.g)
    if s is None:
        await cq.answer("Игра уже закончилась")
        return
    err = s.request_appeal(callback_data.i, cq.from_user.id)
    await cq.answer(err or "Апелляция подана — голосуют остальные игроки", show_alert=bool(err))


async def cb_vote(cq: CallbackQuery, callback_data: VoteCb, registry: Registry) -> None:
    s = registry.get(callback_data.g)
    if s is None:
        await cq.answer("Игра уже закончилась")
        return
    err = await s.cast_vote(callback_data.i, cq.from_user.id, bool(callback_data.v))
    await cq.answer(err or "Голос учтён")


async def cb_noop(cq: CallbackQuery) -> None:
    await cq.answer()


def build_router() -> Router:
    router = Router(name="sigame")
    private = F.chat.type == ChatType.PRIVATE
    group = F.chat.type.in_(GROUPS)

    m = router.message
    m.register(cmd_start_deep, CommandStart(deep_link=True), private)
    m.register(cmd_start, CommandStart())
    m.register(cmd_help, Command("help", "rules"))
    m.register(cmd_newgame, Command("newgame", "game"), group)
    m.register(cmd_newgame_private, Command("newgame", "game"))
    m.register(cmd_join, Command("join"), group)
    m.register(cmd_leave, Command("leave"), group)
    m.register(cmd_stop, Command("stop"), group)
    m.register(cmd_score, Command("score"), group)
    m.register(cmd_skip, Command("skip"), group)
    m.register(cmd_kick, Command("kick"), group)
    m.register(cmd_setscore, Command("setscore"), group)
    m.register(on_document, F.document, group)
    m.register(on_group_text, F.text, ~F.text.startswith("/"), group)
    m.register(on_private_text, F.text, ~F.text.startswith("/"), private)

    c = router.callback_query
    c.register(cb_lobby, LobbyCb.filter())
    c.register(cb_pick, PickCb.filter())
    c.register(cb_press, PressCb.filter())
    c.register(cb_pass, PassCb.filter())
    c.register(cb_choice, ChoiceCb.filter())
    c.register(cb_judge, JudgeCb.filter())
    c.register(cb_info, InfoCb.filter())
    c.register(cb_appeal, AppealCb.filter())
    c.register(cb_vote, VoteCb.filter())
    c.register(cb_noop, NoopCb.filter())
    return router
