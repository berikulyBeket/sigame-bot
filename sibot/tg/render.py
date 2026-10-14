"""Тексты сообщений (HTML) и inline-клавиатуры."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..game.timings import PRESET_TITLES
from ..siq.model import Content, Kind, Question, Round, Theme
from .callbacks import AppealCb, ChoiceCb, InfoCb, JudgeCb, LobbyCb, NoopCb, PassCb, PickCb, PressCb, VoteCb

if TYPE_CHECKING:
    from ..game.session import BidOptions, GameSession, Player
    from ..library import PackageEntry

MAX_BUTTONS = 100  # лимит Telegram на одну клавиатуру
MEDALS = ("🥇", "🥈", "🥉")

# ----------------------------------------------------------------- форматирование


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def num(n: int) -> str:
    return f"{abs(n):,}".replace(",", " ")


def pts(n: int) -> str:
    return ("−" if n < 0 else "") + num(n)


def signed(n: int) -> str:
    return ("+" if n > 0 else "−" if n < 0 else "±") + num(n)


def secs(t: float) -> str:
    return f"{max(1, round(t))} с"


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        form = forms[0]
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = forms[1]
    else:
        form = forms[2]
    return f"{n} {form}"


def bold(p: Player) -> str:
    return f"<b>{esc(p.name)}</b>"


def mention(p: Player) -> str:
    return f'<a href="tg://user?id={p.id}">{esc(p.name)}</a>'


def names(players: Iterable[Player]) -> str:
    return ", ".join(bold(p) for p in players) or "—"


def btn(text: str, data: CallbackData | None = None, url: str | None = None) -> InlineKeyboardButton:
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=cut(text, 60), callback_data=(data or NoopCb()).pack())


def keyboard(rows: Sequence[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def chunks(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def scoreboard(players: Iterable[Player]) -> str:
    rows = sorted(players, key=lambda p: -p.score)
    if not rows:
        return "— игроков нет"
    lines = []
    for i, p in enumerate(rows):
        icon = MEDALS[i] if i < len(MEDALS) and p.score > 0 else "▫️"
        lines.append(f"{icon} {esc(p.name)} — <b>{pts(p.score)}</b>")
    return "\n".join(lines)


# ---------------------------------------------------------------------- справка

HELP = """🎲 <b>Своя игра</b> — аналог SIGame прямо в Telegram

<b>Как играть</b>
1. Добавьте бота в группу и напишите /newgame.
2. Игроки жмут «🙋 Играть». Один раз бот попросит открыть его в личке — \
туда придут ставки и вопросы финала.
3. Создатель лобби выбирает пакет и жмёт «▶️ Начать».
4. Выбирающий открывает вопрос на табло. Кто первым нажал «🔔 Ответить!», пишет ответ в чат. \
Верно — плюс стоимость и право выбора, неверно — минус стоимость, и кнопка открывается для остальных.
5. Не знаете ответ — жмите «🙅 Пас». Спасуют все — вопрос пропускается без штрафов. \
Передумали — ещё можно нажать «🔔 Ответить!».

<b>Особые вопросы</b>
🐱 Кот в мешке — вопрос отдают другому игроку, отвечать обязательно.
💰 Вопрос со ставкой — торги, ва-банк перебивается только бо́льшим ва-банком.
🛡 Без риска — верный ответ ×2, за неверный ничего не снимают.
👥 Для всех — отвечают все сразу, в личку боту.
🏆 Финал — игроки с плюсовым счётом убирают темы, делают ставки и отвечают в личке.

<b>Ведущий</b> — бот (проверяет ответы сам, спорное решают апелляцией) или человек: \
нажмите «🎙 Стать ведущим», и бот будет присылать вам правильные ответы и кнопки «Верно/Неверно».

<b>Команды</b>
/newgame — новая игра в группе
/join, /leave — войти в игру или выйти
/score — счёт
/skip — пропустить вопрос
/kick N, /setscore N сумма — для ведущего и создателя
/stop — остановить игру

<b>Пакеты</b> — встроенные или свои .siq из SIGame/SIQuester: отправьте файл в чат, пока идёт набор игроков."""


def start_text() -> str:
    return (
        "👋 Привет! Я веду «Свою игру» — как SIGame, только в Telegram.\n\n"
        "Добавьте меня в группу и напишите там /newgame. В личке я принимаю ставки и ответы финала.\n\n"
        "Правила и команды: /help"
    )


def dm_joined(chat_title: str) -> str:
    return f"✅ Вы в игре в «{esc(chat_title)}».\nСюда я пришлю ставки и вопросы финала — отвечать нужно будет здесь."


def dm_host(chat_title: str) -> str:
    return (
        f"🎙 Вы ведущий игры в «{esc(chat_title)}».\n"
        "Сюда будут приходить правильные ответы и кнопки «✅ Верно / ❌ Неверно»."
    )


# ------------------------------------------------------------------------- лобби


def package_summary(entry: PackageEntry) -> str:
    pkg = entry.package
    regular = [r for r in pkg.rounds if not r.final]
    parts = [plural(len(regular), ("раунд", "раунда", "раундов"))]
    if pkg.has_final:
        parts[0] += " + финал"
    parts.append(plural(pkg.question_count, ("вопрос", "вопроса", "вопросов")))
    if pkg.authors:
        parts.append("автор: " + esc(cut(", ".join(pkg.authors), 60)))
    return " · ".join(parts)


def lobby_text(s: GameSession) -> str:
    lines = ["🎲 <b>Своя игра</b> — набор игроков", ""]
    if s.entry:
        lines.append(f"📦 Пакет: <b>{esc(s.entry.title)}</b>")
        lines.append(f"      {package_summary(s.entry)}")
        if s.entry.package.restriction:
            lines.append(f"      ограничение: {esc(s.entry.package.restriction)}")
    else:
        lines.append("📦 Пакет: <i>не выбран</i>")
    if s.host:
        lines.append(f"🎙 Ведущий: {bold(s.host)}")
    else:
        lines.append("🎙 Ведущий: 🤖 бот — ответы проверяются автоматически")
    appeals = "вкл" if s.appeals else "выкл"
    lines.append(f"⏱ Темп: {PRESET_TITLES[s.preset]} · ⚖️ Апелляции: {appeals}")
    lines += ["", f"👥 <b>Игроки</b> ({len(s.players)}/{s.max_players}):"]
    lines += [f"{i}. {esc(p.name)}" for i, p in enumerate(s.players.values(), 1)] or ["— пока никого"]
    if s.privacy_warning:
        lines += [
            "",
            "⚠️ Бот не видит обычные сообщения чата. Сделайте его админом "
            "(или отключите Group Privacy в @BotFather и добавьте бота заново) — "
            "иначе отвечайте реплаем на сообщения бота.",
        ]
    lines += ["", "Жмите «🙋 Играть». Начинает игру создатель лобби или админ чата."]
    return "\n".join(lines)


def lobby_kb(s: GameSession) -> InlineKeyboardMarkup:
    g = s.gid
    host_btn = (
        btn("🤖 Ведущий — бот", LobbyCb(g=g, a="unhost")) if s.host else btn("🎙 Стать ведущим", LobbyCb(g=g, a="host"))
    )
    return keyboard(
        [
            [btn("🙋 Играть", LobbyCb(g=g, a="join")), btn("🚪 Выйти", LobbyCb(g=g, a="leave"))],
            [host_btn, btn("📦 Пакет", LobbyCb(g=g, a="packs"))],
            [
                btn(f"⏱ Темп: {PRESET_TITLES[s.preset]}", LobbyCb(g=g, a="tempo")),
                btn(f"⚖️ Апелляции: {'вкл' if s.appeals else 'выкл'}", LobbyCb(g=g, a="appeals")),
            ],
            [btn("▶️ Начать", LobbyCb(g=g, a="start")), btn("✖️ Отмена", LobbyCb(g=g, a="cancel"))],
        ]
    )


def packs_text(max_mb: int) -> str:
    return (
        "📦 <b>Выбор пакета</b>\n\n"
        "Ниже — встроенные пакеты и загруженные в этот чат.\n"
        f"Свой пакет: отправьте в чат файл <b>.siq</b> (до {max_mb} МБ). "
        "Тысячи пакетов есть в библиотеке SIGame, свой можно собрать в редакторе SIQuester."
    )


def packs_kb(s: GameSession, entries: list[PackageEntry]) -> InlineKeyboardMarkup:
    rows = []
    for e in entries:
        mark = "✅ " if s.entry is e else ""
        rows.append([btn(f"{mark}{e.title} · {e.package.question_count} в.", LobbyCb(g=s.gid, a="pack", v=e.key))])
    rows.append([btn("⬅️ Назад", LobbyCb(g=s.gid, a="back"))])
    return keyboard(rows)


def lobby_cancelled() -> str:
    return "✖️ Набор в игру отменён. Новая игра: /newgame"


def lobby_started(s: GameSession) -> str:
    host = bold(s.host) if s.host else "🤖 бот"
    return (
        f"🎲 <b>Своя игра началась!</b>\n📦 {esc(s.entry.title) if s.entry else ''}\n"
        f"🎙 Ведущий: {host}\n👥 Игроки: {names(s.players.values())}"
    )


# -------------------------------------------------------------------------- игра


def game_intro(entry: PackageEntry, host: Player | None, players: Iterable[Player]) -> str:
    pkg = entry.package
    lines = [f"🎬 <b>{esc(pkg.name)}</b>", package_summary(entry)]
    if pkg.comment:
        lines.append(f"<i>{esc(cut(pkg.comment, 400))}</i>")
    lines += ["", f"🎙 Ведущий: {bold(host) if host else '🤖 бот'}", f"👥 Играют: {names(players)}"]
    return "\n".join(lines)


def round_intro(rnd: Round, no: int) -> str:
    lines = [f"📢 <b>Раунд {no}: {esc(rnd.name)}</b>"]
    if rnd.comment:
        lines.append(f"<i>{esc(cut(rnd.comment, 300))}</i>")
    lines += ["", "<b>Темы:</b>"] + [f"• {esc(t.name)}" for t in rnd.themes]
    return "\n".join(lines)


def round_end(rnd: Round, players: Iterable[Player]) -> str:
    return f"🏁 Раунд «{esc(rnd.name)}» завершён!\n{scoreboard(players)}"


def _theme_rows(rnd: Round, played: set[tuple[int, int]]) -> list[int]:
    return [ti for ti, t in enumerate(rnd.themes) if any((ti, qi) not in played for qi in range(len(t.questions)))]


def board_is_compact(rnd: Round, played: set[tuple[int, int]]) -> bool:
    total = sum(1 + len(rnd.themes[ti].questions) for ti in _theme_rows(rnd, played))
    return total > MAX_BUTTONS


def board_text(rnd: Round, players: Iterable[Player], played: set[tuple[int, int]], compact: bool) -> str:
    left = rnd.question_count - len(played)
    lines = [f"📋 <b>{esc(rnd.name)}</b> · осталось {plural(left, ('вопрос', 'вопроса', 'вопросов'))}"]
    if compact:
        lines += [f"{ti + 1}. {esc(rnd.themes[ti].name)}" for ti in _theme_rows(rnd, played)]
    lines += ["", scoreboard(players)]
    return "\n".join(lines)


def board_prompt(chooser: Player, t: float) -> str:
    return f"👉 Выбирает {mention(chooser)} · ⏱ {secs(t)}"


def board_chosen(chooser: Player, theme: Theme, price: int, auto: bool) -> str:
    if auto:
        return f"⏱ Время вышло — случайный вопрос: «{esc(theme.name)}» за {pts(price)}"
    return f"👉 {bold(chooser)} выбирает «{esc(theme.name)}» за {pts(price)}"


def board_kb(
    gid: str, step: int, rnd: Round, played: set[tuple[int, int]], compact: bool, round_no: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ti in _theme_rows(rnd, played):
        theme = rnd.themes[ti]
        if compact:  # слишком много кнопок: без заголовков, только доступные вопросы
            buttons = [
                btn(f"{ti + 1}·{q.price}", PickCb(g=gid, n=step, t=ti, q=qi))
                for qi, q in enumerate(theme.questions)
                if (ti, qi) not in played
            ]
        else:
            rows.append([btn(f"📂 {theme.name}", InfoCb(g=gid, r=round_no, t=ti))])
            buttons = [
                btn("·") if (ti, qi) in played else btn(str(q.price), PickCb(g=gid, n=step, t=ti, q=qi))
                for qi, q in enumerate(theme.questions)
            ]
        rows += chunks(buttons, 6)
    while sum(map(len, rows)) > MAX_BUTTONS:
        rows.pop()
    return keyboard(rows)


def theme_info(theme: Theme) -> str:
    text = theme.name
    if theme.comment:
        text += "\n\n" + theme.comment
    return cut(text, 200)


def question_header(theme: str, price: int | None, tag: str | None = None) -> str:
    head = f"📂 <b>{esc(theme)}</b>" + (f" · <b>{pts(price)}</b>" if price is not None else "")
    return head + (f"\n{tag}" if tag else "")


def youtube_link(url: str) -> str:
    m = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{6,})", url)
    return f"https://youtu.be/{m.group(1)}" if m else url


def content_text(items: Iterable[Content]) -> str:
    parts = []
    for c in items:
        if c.kind == Kind.TEXT:
            parts.append(f"🎙 <i>{esc(c.value)}</i>" if c.replic else esc(c.value))
        elif c.kind == Kind.HTML and not c.ref:
            parts.append(f'🔗 <a href="{html.escape(youtube_link(c.value))}">открыть</a>')
    return "\n\n".join(parts)


def options_text(q: Question) -> str:
    return "\n".join(f"<b>{esc(label)})</b> {esc(text)}" for label, text in q.options)


def question_text(header: str, q: Question) -> str:
    parts = [header, content_text(q.content), options_text(q)]
    return cut("\n\n".join(p for p in parts if p), 4000)


def press_kb(gid: str, step: int, passed: int = 0, total: int = 0) -> InlineKeyboardMarkup:
    """Кнопки вопроса; на «Пас» — сколько из оставшихся игроков уже спасовали."""
    pass_text = f"🙅 Пас · {passed}/{total}" if passed else "🙅 Пас"
    return keyboard([[btn("🔔 Ответить!", PressCb(g=gid, n=step)), btn(pass_text, PassCb(g=gid, n=step))]])


def press_more(t: float) -> str:
    return f"\n🔔 Кто-нибудь ещё? Жмите кнопку · ⏱ {secs(t)}"


def answer_prompt(p: Player, t: float, q: Question, hosted: bool, reply_hint: bool = False) -> str:
    if q.answer_type == "select":
        how = "Выберите вариант кнопкой или напишите букву"
    elif reply_hint:  # бот не видит обычные сообщения, но видит ответы на свои
        how = "Ответьте реплаем на это сообщение"
    else:
        how = "Пишите в чат"
    text = f"🎤 {mention(p)}, ваш ответ? {how} · ⏱ {secs(t)}"
    return text + ("\n🎙 Оценивает ведущий." if hosted else "")


def options_kb(gid: str, step: int, q: Question) -> InlineKeyboardMarkup:
    buttons = [btn(f"{label}) {text}", ChoiceCb(g=gid, n=step, k="opt", v=label)) for label, text in q.options]
    return keyboard(chunks(buttons, 2))


def wrong_line(p: Player, given: str | None, delta: int) -> str:
    money = f" {signed(delta)}" if delta else " Без штрафа."
    if given is None:
        return f"⌛ {bold(p)}: время на ответ вышло.{money}"
    return f"❌ {bold(p)}: «{esc(cut(given, 200))}» — неверно.{money}"


def right_line(p: Player, given: str | None, delta: int) -> str:
    said = f": «{esc(cut(given, 200))}»" if given else ""
    return f"✅ {bold(p)}{said} — верно! {signed(delta)}"


def nobody_line(tried: bool) -> str:
    return "😶 Правильного ответа никто не дал." if tried else "🤷 Никто не нажал кнопку."


def passed_line(tried: bool) -> str:
    return "🙅 Остальные спасовали." if tried else "🙅 Все спасовали — вопрос пропущен."


def answer_block(q: Question, display: Callable[[Question, str], str]) -> str:
    answer = q.answer
    lines = [f"💡 Ответ: <b>{esc(display(q, answer) if answer else '—')}</b>"]
    extra = content_text(q.answer_content)
    if extra:
        lines.append(extra)
    if q.comment:
        lines.append(f"<i>{esc(cut(q.comment, 500))}</i>")
    return "\n".join(lines)


def skipped_line() -> str:
    return "⏭ Вопрос пропущен."


def media_missing() -> str:
    return "<i>(медиафайл недоступен)</i>"


# ---------------------------------------------------------------- апелляции


def appeal_kb(gid: str, no: int) -> InlineKeyboardMarkup:
    return keyboard([[btn("⚖️ Апелляция", AppealCb(g=gid, i=no))]])


def vote_text(p: Player, given: str, answer: str, t: float) -> str:
    return (
        f"⚖️ <b>Апелляция</b>\n{bold(p)} просит засчитать ответ «{esc(cut(given, 200))}».\n"
        f"💡 Правильный ответ: <b>{esc(answer or '—')}</b>\n\nГолосуют остальные игроки · ⏱ {secs(t)}"
    )


def vote_kb(gid: str, vid: int, yes: int, no: int) -> InlineKeyboardMarkup:
    return keyboard(
        [[btn(f"👍 Засчитать · {yes}", VoteCb(g=gid, i=vid, v=1)), btn(f"👎 Нет · {no}", VoteCb(g=gid, i=vid, v=0))]]
    )


def vote_result(p: Player, given: str, accepted: bool, gain: int, yes: int, no: int, voters: int) -> str:
    head = f"⚖️ Апелляция игрока {bold(p)} («{esc(cut(given, 100))}») "
    if not voters:
        votes = "голосовать некому"
    elif yes or no:
        votes = f"за {yes}, против {no}"
    else:
        votes = "никто не проголосовал"
    if accepted:
        return head + f"принята ✅ {signed(gain)} — {votes}"
    return head + f"отклонена ❌ — {votes}"


# ---------------------------------------------------------------- кот в мешке


def price_list(prices: Sequence[int]) -> str:
    return ", ".join(pts(p) for p in prices) if len(prices) <= 4 else f"от {pts(prices[0])} до {pts(prices[-1])}"


def secret_give(chooser: Player, theme: str | None, prices: Sequence[int] | None, t: float) -> str:
    lines = ["🐱 <b>Кот в мешке!</b>"]
    if theme is not None and prices is not None:
        lines.append(f"📂 Тема: «{esc(theme)}» · стоимость: {price_list(prices)}")
    lines.append(f"{mention(chooser)}, кому отдаёте вопрос? · ⏱ {secs(t)}")
    return "\n".join(lines)


def give_kb(gid: str, step: int, candidates: Sequence[Player]) -> InlineKeyboardMarkup:
    buttons = [btn(f"{p.name} ({pts(p.score)})", ChoiceCb(g=gid, n=step, k="give", v=str(p.id))) for p in candidates]
    return keyboard(chunks(buttons, 2))


def secret_given(chooser: Player, receiver: Player, theme: str, how: str, gift: bool) -> str:
    """how: chosen — отдал выбирающий, timeout — случайно по таймауту, only — других вариантов нет."""
    if how == "only":
        head = f"🐱 <b>Кот в мешке</b> достаётся {bold(receiver)}"
    elif how == "timeout":
        head = f"🐱 Время вышло — кот в мешке случайно достаётся {bold(receiver)}"
    else:
        head = f"🐱 {bold(chooser)} отдаёт кота в мешке игроку {bold(receiver)}"
    return head if gift else f"{head}\n📂 Тема: «{esc(theme)}»"


def secret_price(receiver: Player, prices: Sequence[int], t: float) -> str:
    return f"💵 {mention(receiver)}, выберите стоимость вопроса · ⏱ {secs(t)}"


def price_kb(gid: str, step: int, prices: Sequence[int]) -> InlineKeyboardMarkup:
    return keyboard(chunks([btn(pts(v), ChoiceCb(g=gid, n=step, k="price", v=str(v))) for v in prices], 4))


def secret_price_chosen(receiver: Player, price: int, auto: bool) -> str:
    how = "время вышло, стоимость" if auto else "стоимость"
    return f"💵 {bold(receiver)}: {how} — <b>{pts(price)}</b>"


def secret_gift(receiver: Player, price: int) -> str:
    return f"🎁 Кот без вопроса! {bold(receiver)} просто получает {signed(price)}"


# ---------------------------------------------------------------------- торги


def stake_text(
    theme: str, nominal: int, stake: int, leader: Player | None, current: Player, log: list[str], t: float
) -> str:
    lines = ["💰 <b>Вопрос со ставкой!</b>", f"📂 {esc(theme)} · номинал {pts(nominal)}"]
    if log:
        lines += ["", *log[-8:]]
    lines.append("")
    if leader:
        lines.append(f"Ставка: <b>{pts(stake)}</b> — {bold(leader)}")
    lines.append(f"👉 Ход: {mention(current)} (счёт {pts(current.score)}) · ⏱ {secs(t)}")
    lines.append("<i>Кнопкой или числом в чат.</i>")
    return "\n".join(lines)


def stake_kb(gid: str, step: int, o: BidOptions) -> InlineKeyboardMarkup:
    rows = [[btn(pts(v), ChoiceCb(g=gid, n=step, k="bid", v=str(v))) for v in o.presets]]
    last = []
    if o.allin:
        last.append(btn(f"🔥 Ва-банк ({pts(o.max_bid)})", ChoiceCb(g=gid, n=step, k="bid", v="allin")))
    if o.can_pass:
        last.append(btn("🙅 Пас", ChoiceCb(g=gid, n=step, k="bid", v="pass")))
    return keyboard([*chunks(rows[0], 4), last])


def stake_log(p: Player, action: str, amount: int, auto: bool = False) -> str:
    suffix = " (время вышло)" if auto else ""
    if action == "pass":
        return f"• {esc(p.name)}: пас{suffix}"
    if action == "allin":
        return f"• {esc(p.name)}: 🔥 ва-банк {pts(amount)}{suffix}"
    return f"• {esc(p.name)}: {pts(amount)}{suffix}"


def stake_done(theme: str, nominal: int, leader: Player, stake: int, log: list[str]) -> str:
    lines = ["💰 <b>Вопрос со ставкой</b>", f"📂 {esc(theme)} · номинал {pts(nominal)}"]
    if log:
        lines += ["", *log[-8:]]
    lines += ["", f"🏷 Отвечает {bold(leader)}, ставка <b>{pts(stake)}</b>"]
    return "\n".join(lines)


def norisk_tag(p: Player, price: int) -> str:
    return f"🛡 <b>Вопрос без риска</b> для {mention(p)}: верно — {signed(2 * price)}, неверно — без штрафа"


# ------------------------------------------------------------- для всех / финал


def all_intro(theme: str, price: int, part: Sequence[Player], stake_all: bool) -> str:
    if stake_all:
        return (
            f"💰 <b>Ставки для всех!</b> 📂 {esc(theme)}\n"
            f"Каждый игрок с плюсовым счётом делает ставку и отвечает в личке бота.\nУчаствуют: {names(part)}"
        )
    return (
        f"👥 <b>Вопрос для всех!</b> 📂 {esc(theme)} · {pts(price)}\n"
        f"Отвечают все, в личке бота: верно — {signed(price)}, неверно — {signed(-price)}."
    )


def bet_values(score: int) -> list[int]:
    step = 100 if score >= 400 else 10 if score >= 40 else 1
    values = {1}
    for share in (0.25, 0.5, 0.75):
        v = int(score * share) // step * step
        if 1 <= v < score:
            values.add(v)
    return sorted(values)


def bet_prompt(theme: str, score: int, t: float) -> str:
    return (
        f"💰 Ставка на тему «{esc(theme)}»\nВаш счёт: <b>{pts(score)}</b>, ставка — от 1 до {pts(score)}.\n"
        f"Нажмите кнопку или напишите число · ⏱ {secs(t)}"
    )


def bet_kb(gid: str, step: int, score: int) -> InlineKeyboardMarkup:
    buttons = [btn(pts(v), ChoiceCb(g=gid, n=step, k="bet", v=str(v))) for v in bet_values(score)]
    allin = btn(f"🔥 Ва-банк ({pts(score)})", ChoiceCb(g=gid, n=step, k="bet", v=str(score)))
    return keyboard([*chunks(buttons, 4), [allin]])


def bet_done(amount: int) -> str:
    return f"✅ Ставка принята: <b>{pts(amount)}</b>"


def bet_default() -> str:
    return "⏱ Время вышло — ставка по умолчанию: 1"


def dm_hint(missing: Sequence[Player], bot_username: str) -> str:
    if not missing:
        return ""
    return f"\n⚠️ {names(missing)}: бот не может вам написать — откройте @{esc(bot_username)} и отправьте ответ там."


def bets_wait(part: Sequence[Player], missing: Sequence[Player], t: float, bot_username: str) -> str:
    return f"💰 Делают ставки в личке бота: {names(part)} · ⏱ {secs(t)}" + dm_hint(missing, bot_username)


def bets_done(part: Sequence[Player]) -> str:
    return f"💰 Ставки сделаны: {names(part)}"


def private_question(theme: str, q: Question, t: float, final: bool) -> str:
    title = "🏆 <b>Финальный вопрос</b>" if final else "👥 <b>Вопрос для всех</b>"
    media = "\n<i>(картинка/звук — в общем чате)</i>" if any(c.is_media for c in q.content) else ""
    how = "Выберите вариант кнопкой" if q.answer_type == "select" else "Напишите ответ сюда одним сообщением"
    body = question_text(f"{title}\n📂 {esc(theme)}", q)
    return f"{body}{media}\n\n✍️ {how} · ⏱ {secs(t)}. Изменить ответ потом нельзя."


def answers_wait(part: Sequence[Player], missing: Sequence[Player], t: float, bot_username: str) -> str:
    return f"✍️ Ждём ответы в личке бота: {names(part)} · ⏱ {secs(t)}" + dm_hint(missing, bot_username)


def answer_received(shown: str) -> str:
    return f"✅ Ответ принят: «{esc(cut(shown, 200))}»"


def many_line(p: Player, given: str | None, ok: bool, amount: int) -> str:
    said = f"«{esc(cut(given, 150))}»" if given else "<i>нет ответа</i>"
    return f"{'✅' if ok else '❌'} {bold(p)}: {said} {signed(amount if ok else -amount)}"


def final_intro(rnd: Round, finalists: Sequence[Player], excluded: Sequence[Player]) -> str:
    plain = rnd.name.strip().casefold() in ("финал", "final", "финальный раунд")
    lines = ["🏆 <b>Финал</b>" if plain else f"🏆 <b>Финал: {esc(rnd.name)}</b>"]
    if rnd.comment:
        lines.append(f"<i>{esc(cut(rnd.comment, 300))}</i>")
    if finalists:
        lines.append("Играют: " + ", ".join(f"{bold(p)} ({pts(p.score)})" for p in finalists))
    if excluded:
        lines.append(f"Не прошли (нужен плюсовой счёт): {names(excluded)}")
    if not finalists:
        lines.append("😔 В финал никто не прошёл.")
    return "\n".join(lines)


def final_themes(themes: Sequence[Theme], remaining: Sequence[int], current: Player, t: float) -> str:
    lines = ["🗑 <b>Финал: убираем темы</b>", ""]
    for i, th in enumerate(themes):
        lines.append(f"• {esc(th.name)}" if i in remaining else f"• <s>{esc(th.name)}</s>")
    lines += ["", f"👉 Убирает тему: {mention(current)} · ⏱ {secs(t)}"]
    return "\n".join(lines)


def final_themes_kb(gid: str, step: int, themes: Sequence[Theme], remaining: Sequence[int]) -> InlineKeyboardMarkup:
    return keyboard([[btn(f"❌ {themes[i].name}", ChoiceCb(g=gid, n=step, k="rm", v=str(i)))] for i in remaining])


def final_theme_left(theme: Theme) -> str:
    return f"🎯 Тема финала: <b>{esc(theme.name)}</b>"


def final_results(lines: Sequence[str]) -> str:
    return "🏁 <b>Итоги финала</b>\n" + "\n".join(lines)


def many_results(lines: Sequence[str]) -> str:
    return "👥 <b>Ответы</b>\n" + "\n".join(lines)


# ---------------------------------------------------------------------- ведущий


def host_brief(theme: str, price: int | None, q: Question, tag: str | None = None) -> str:
    lines = ["🎙 <b>Для ведущего</b>", question_header(theme, price, tag), ""]
    body = content_text(q.content)
    if body:
        lines.append(f"❓ {body}")
    media = [c for c in q.content if c.is_media]
    if media:
        lines.append(f"🖼 медиа: {len(media)} шт. — в общем чате")
    if q.options:
        lines.append(options_text(q))
    lines += ["", f"💡 <b>{esc(q.answer or '—')}</b>"]
    if len(q.right) > 1:
        lines.append("➕ Также: " + esc("; ".join(q.right[1:])))
    if q.wrong:
        lines.append("🚫 Не засчитывать: " + esc("; ".join(q.wrong)))
    if q.comment:
        lines.append(f"💬 <i>{esc(cut(q.comment, 500))}</i>")
    return cut("\n".join(lines), 4000)


def judge_text(p: Player, theme: str, price: int | None, q: Question, given: str | None) -> str:
    said = f"«{esc(cut(given, 300))}»" if given else "<i>пока не написан — может ответить голосом</i>"
    lines = [
        f"⚖️ Отвечает {bold(p)} · {question_header(theme, price)}",
        f"Ответ игрока: {said}",
        f"💡 Правильный: <b>{esc(q.answer or '—')}</b>",
    ]
    if len(q.right) > 1:
        lines.append("➕ Также: " + esc("; ".join(q.right[1:])))
    if q.wrong:
        lines.append("🚫 Не засчитывать: " + esc("; ".join(q.wrong)))
    return "\n".join(lines)


def judge_kb(gid: str, step: int, user_id: int) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                btn("✅ Верно", JudgeCb(g=gid, n=step, u=user_id, v=1)),
                btn("❌ Неверно", JudgeCb(g=gid, n=step, u=user_id, v=0)),
            ]
        ]
    )


def judge_done(verdict: bool, auto: bool) -> str:
    result = "✅ засчитан" if verdict else "❌ не засчитан"
    return f"\n\n{'⏱ Решения не было — проверено автоматически: ' if auto else 'Итог: '}{result}"


# ------------------------------------------------------------------------ итоги


def game_over(players: Sequence[Player], completed: bool, reason: str = "") -> str:
    head = (
        "🎉 <b>Игра окончена!</b>" if completed else f"⏹ <b>Игра остановлена</b>{f' ({esc(reason)})' if reason else ''}"
    )
    lines = [head, scoreboard(players)]
    if completed and players:
        best = max(p.score for p in players)
        winners = [p for p in players if p.score == best]
        if best > 0:
            title = "🏆 Победитель" if len(winners) == 1 else "🏆 Победители"
            lines += ["", f"{title}: {names(winners)}!"]
    lines += ["", "Сыграть ещё: /newgame"]
    return "\n".join(lines)


def score_list(s: GameSession) -> str:
    if not s.players:
        return "Игроков пока нет."
    lines = ["📊 <b>Счёт</b>"]
    lines += [f"{i}. {esc(p.name)} — <b>{pts(p.score)}</b>" for i, p in enumerate(s.players.values(), 1)]
    if s.host:
        lines.append(f"🎙 Ведущий: {bold(s.host)}")
    lines.append("\n<i>Номера нужны для /kick N и /setscore N сумма.</i>")
    return "\n".join(lines)


def joined_midgame(p: Player) -> str:
    return f"👋 {bold(p)} присоединяется к игре!"


def left_game(p: Player) -> str:
    return f"🚪 {bold(p)} покидает игру."
