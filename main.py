from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .game_builder import GuessProcess, Operator, OperatorPool, OperatorRepository


PLUGIN_ID = "astrbot_plugin_wordle2"
CONTROL_WORDS = {"提示", "线索", "跳过", "下一个", "结束", "不玩了"}
START_TRIGGER_REGEX = (
    r"^(?:"
    r"(?:我想玩|想玩|我要玩|来一局|来一把|开一局|开一把|玩一下|玩一把|启动|开始)?\s*"
    r"(?:大帝(?:的)?\s*(?:[Cc][Yy][Pp][Hh][Ee][Rr])?\s*挑战|干员猜盐|明日方舟猜盐|方舟猜盐)"
    r")"
    r"(?:\s*(?:普通|硬核))?"
    r"[。.!！?？~～]*$"
)
START_TRIGGER_PATTERN = re.compile(START_TRIGGER_REGEX)


@dataclass
class GameSession:
    umo: str
    pool: OperatorPool
    hardcode: bool | None = None
    previous: Operator | None = None
    process: GuessProcess | None = None
    timeout_task: asyncio.Task | None = None


@register(PLUGIN_ID, "ahuai", "大帝的CYPHER挑战 AstrBot 迁移版", "1.0.0")
class Wordle2Plugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config
        self.base_dir = Path(__file__).resolve().parent
        self.repository = OperatorRepository.load(
            self._operator_data_path(),
            self.base_dir / "data" / "operator_aliases.json",
        )
        self.sessions: dict[str, GameSession] = {}
        self.board_template = self._load_board_template()
        self._playwright = None
        self._browser = None
        self._browser_lock = asyncio.Lock()

    @filter.command("猜干员")
    async def guess_operator_command(self, event: AstrMessageEvent):
        async for result in self._handle_start(event):
            yield result

    @filter.regex(START_TRIGGER_REGEX)
    async def guess_operator_keyword(self, event: AstrMessageEvent):
        async for result in self._handle_start(event):
            yield result

    @filter.regex(r"^[\s\S]*$")
    async def on_game_message(self, event: AstrMessageEvent):
        session = self.sessions.get(event.unified_msg_origin)
        if not session:
            return

        text = event.message_str.strip()
        if not text:
            return

        if session.hardcode is None:
            hardcode = self._parse_mode(text)
            if hardcode is None:
                return

            event.stop_event()
            session.hardcode = hardcode
            messages = self._start_round(session)
            self._reset_timeout(session)
            yield await self._compose_result(event, messages, session)
            return

        if not self._is_game_input(text, session):
            return

        event.stop_event()
        self._reset_timeout(session)
        async for result in self._handle_game_input(event, session, text):
            yield result

    async def terminate(self):
        for session in list(self.sessions.values()):
            self._cancel_timeout(session)
        self.sessions.clear()
        await self._close_browser()

    async def _handle_start(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        text = event.message_str.strip()
        payload = self._command_payload(text)
        hardcode = self._parse_mode(payload or text)

        if umo in self.sessions:
            event.stop_event()
            session = self.sessions[umo]
            if session.hardcode is None:
                if hardcode is None:
                    yield event.plain_result("游戏已经在准备中了，请选择难度：【普通】或【硬核】")
                else:
                    session.hardcode = hardcode
                    messages = self._start_round(session)
                    self._reset_timeout(session)
                    yield await self._compose_result(event, messages, session)
            else:
                if self._is_start_trigger(payload):
                    messages = ["游戏已经开始啦。发送干员名竞猜，或发送【提示】【跳过】【结束】。"]
                    yield await self._compose_result(event, messages, session)
                    return
                game_text = self._game_text_from_command_payload(payload)
                if game_text and self._is_game_input(game_text, session):
                    self._reset_timeout(session)
                    async for result in self._handle_game_input(event, session, game_text):
                        yield result
                elif game_text:
                    yield event.plain_result(f"没有找到干员【{game_text}】，请换一个名字试试。")
                else:
                    messages = ["游戏已经开始啦。发送干员名竞猜，或发送【提示】【跳过】【结束】。"]
                    yield await self._compose_result(event, messages, session)
            return

        event.stop_event()
        session = GameSession(umo=umo, pool=self.repository.fresh_pool(), hardcode=hardcode)
        self.sessions[umo] = session

        if hardcode is None:
            yield event.plain_result(self._intro_text())
            return

        messages = self._start_round(session)
        self._reset_timeout(session)
        yield await self._compose_result(event, messages, session)

    async def _handle_game_input(self, event: AstrMessageEvent, session: GameSession, text: str):
        process = session.process
        if process is None:
            return

        messages: list[str] = []

        if text in {"提示", "线索"}:
            tips = process.get_tips()
            if tips is False:
                yield event.plain_result("已经没有可揭示的线索了")
                return
            if tips is None:
                yield event.plain_result("不能连续揭示两个线索哦，请猜一次后继续~")
                return

            process.display = True
            messages.append(f"揭示了一个线索：【{tips.title}】{tips.display_value}")
            yield await self._compose_result(event, messages, session)
            return

        if text in {"下一个", "跳过"}:
            process.reveal()
            messages.append(f"答案是{process.operator.name}")
            session.previous = process.operator
            next_messages = self._start_round(session)
            next_process = session.process
            self._reset_timeout(session)
            if next_process is None:
                yield await self._compose_process_result(event, messages, process)
            else:
                yield await self._compose_round_transition_result(event, messages, process, next_messages, next_process)
            return

        if text in {"不玩了", "结束"}:
            process.reveal()
            self._end_session(session.umo)
            yield await self._compose_process_result(event, [f"答案是{process.operator.name}，游戏结束~"], process)
            return

        answer = self.repository.find(text, target=process.operator)
        if answer is None:
            return

        if answer.id in process.wrongs:
            yield event.plain_result(f"干员【{answer.name}】已经猜过啦，换一个试试吧~")
            return

        match, unlock = process.guess(answer)
        if match == -1:
            reward = 300 * (len(process.closed_tags) + 1)
            messages.append(f"回答正确！本轮理论奖励合成玉{reward}")
            process.reveal()
            session.previous = process.operator
            next_messages = self._start_round(session)
            next_process = session.process
            self._reset_timeout(session)
            if next_process is None:
                yield await self._compose_process_result(event, messages, process)
            else:
                yield await self._compose_round_transition_result(event, messages, process, next_messages, next_process)
            return

        if process.count >= process.max_count:
            process.reveal()
            messages.append(f"机会耗尽，答案是{process.operator.name}")
            session.previous = process.operator
            next_messages = self._start_round(session)
            next_process = session.process
            self._reset_timeout(session)
            if next_process is None:
                yield await self._compose_process_result(event, messages, process)
            else:
                yield await self._compose_round_transition_result(event, messages, process, next_messages, next_process)
            return

        text_result = f"匹配了{match}个线索"
        if match >= 5:
            text_result += "，基本可以肯定是哪位干员了"
        elif match >= 3:
            text_result += "，离答案非常接近了"
        elif match >= 1:
            text_result += "，已经有一些头绪了"
        else:
            text_result = "所有的线索都对不上哦"

        if unlock:
            messages.append(f"解锁了{unlock}个线索，理论奖励{unlock * 100}合成玉")

        messages.append(f"{text_result}，请再猜猜吧~（{process.count}/{process.max_count}）")
        yield await self._compose_result(event, messages, session)

    def _start_round(self, session: GameSession) -> list[str]:
        messages: list[str] = []
        if session.pool.is_empty:
            messages.append("竟然把所有干员都猜完了...这可怕的毅力，不愧是巴别塔的恶灵！再来！")
            session.pool = self.repository.fresh_pool()

        operator = session.pool.pick_one()
        previous = session.previous
        messages.append(f"题目准备中...共有{self._max_guesses()}次机会竞猜！")

        if not session.hardcode:
            if previous is None:
                previous = session.pool.pick_one() if not session.pool.is_empty else None
            if previous is not None:
                messages.append(f"{previous.name}为博士们提供了帮助！")

        session.process = GuessProcess(operator, previous, bool(session.hardcode), self._max_guesses())
        return messages

    async def _compose_result(self, event: AstrMessageEvent, messages: list[str], session: GameSession):
        process = session.process
        if process is None:
            text = "\n\n".join(item for item in messages if item)
            return event.plain_result(text or "题目还没有准备好。")
        return await self._compose_process_result(event, messages, process)

    async def _compose_process_result(self, event: AstrMessageEvent, messages: list[str], process: GuessProcess):
        text = "\n\n".join(item for item in messages if item)

        board_text = process.render_text_board()
        if not self._config_get("enable_image_board", True):
            return event.plain_result(self._join_text(text, board_text))

        image_path = await self._render_board_image(process)
        if not image_path:
            return event.plain_result(self._join_text(text, board_text))

        chain = []
        if text:
            chain.append(Comp.Plain(text=f"{text}\n"))
        if str(image_path).startswith(("http://", "https://")):
            chain.append(Comp.Image.fromURL(str(image_path)))
        else:
            chain.append(Comp.Image.fromFileSystem(str(image_path)))
        return event.chain_result(chain)

    async def _compose_round_transition_result(
        self,
        event: AstrMessageEvent,
        reveal_messages: list[str],
        reveal_process: GuessProcess,
        next_messages: list[str],
        next_process: GuessProcess,
    ):
        reveal_text = "\n\n".join(item for item in reveal_messages if item)
        next_text = "\n\n".join(item for item in next_messages if item)
        reveal_board = reveal_process.render_text_board()
        next_board = next_process.render_text_board()

        if not self._config_get("enable_image_board", True):
            return event.plain_result(
                self._join_text(
                    reveal_text,
                    f"【本题揭晓】\n{reveal_board}",
                    next_text,
                    f"【下一题】\n{next_board}",
                )
            )

        reveal_image, next_image = await asyncio.gather(
            self._render_board_image(reveal_process),
            self._render_board_image(next_process),
        )
        if not reveal_image and not next_image:
            return event.plain_result(
                self._join_text(
                    reveal_text,
                    f"【本题揭晓】\n{reveal_board}",
                    next_text,
                    f"【下一题】\n{next_board}",
                )
            )

        chain = []
        if reveal_text:
            chain.append(Comp.Plain(text=f"{reveal_text}\n"))
        chain.append(Comp.Plain(text="【本题揭晓】\n"))
        if reveal_image:
            chain.append(Comp.Image.fromFileSystem(str(reveal_image)))
        else:
            chain.append(Comp.Plain(text=f"{reveal_board}\n"))

        if next_text:
            chain.append(Comp.Plain(text=f"\n{next_text}\n"))
        chain.append(Comp.Plain(text="【下一题】\n"))
        if next_image:
            chain.append(Comp.Image.fromFileSystem(str(next_image)))
        else:
            chain.append(Comp.Plain(text=next_board))
        return event.chain_result(chain)

    async def _render_board_image(self, process: GuessProcess) -> str | None:
        try:
            from jinja2 import BaseLoader, Environment, select_autoescape
        except Exception:
            return None

        try:
            environment = Environment(
                loader=BaseLoader(),
                autoescape=select_autoescape(enabled_extensions=("html", "xml"), default_for_string=True),
            )
            html = environment.from_string(self.board_template).render(data=process.view_data)
            output_dir = self._render_temp_dir()
            output_dir.mkdir(parents=True, exist_ok=True)
            image_path = output_dir / f"wordle2_board_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.png"
            html_path = self.base_dir / "template" / f"wordle2_board_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.html"
            html_path.write_text(html, encoding="utf-8")

            browser = await self._ensure_browser()
            page = await browser.new_page(
                viewport={"width": 1280, "height": 720},
                device_scale_factor=1,
            )
            try:
                await page.goto(html_path.as_uri(), wait_until="load", timeout=5000)
                await page.screenshot(
                    path=str(image_path),
                    type="png",
                    full_page=True,
                    timeout=5000,
                )
            finally:
                await page.close()
                try:
                    html_path.unlink(missing_ok=True)
                except Exception:
                    pass

            return str(image_path)
        except Exception:
            if self._browser and not self._browser.is_connected():
                await self._reset_browser()
            return None

    async def _ensure_browser(self):
        async with self._browser_lock:
            if self._browser and self._browser.is_connected():
                return self._browser

            try:
                from playwright.async_api import async_playwright
            except Exception as exc:
                raise RuntimeError("playwright is not available") from exc

            await self._close_browser()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--allow-file-access-from-files"],
            )
            return self._browser

    async def _reset_browser(self) -> None:
        async with self._browser_lock:
            await self._close_browser()

    async def _close_browser(self) -> None:
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None

        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        if playwright:
            try:
                await playwright.stop()
            except Exception:
                pass

    def _render_temp_dir(self) -> Path:
        configured = self._config_get("local_render_temp_dir", "")
        if configured:
            return Path(str(configured))
        return Path("/AstrBot/data/temp")

    def _reset_timeout(self, session: GameSession) -> None:
        self._cancel_timeout(session)
        session.timeout_task = asyncio.create_task(self._timeout_watch(session.umo))

    def _cancel_timeout(self, session: GameSession) -> None:
        if session.timeout_task and not session.timeout_task.done():
            session.timeout_task.cancel()
        session.timeout_task = None

    async def _timeout_watch(self, umo: str) -> None:
        timeout = self._round_timeout()
        checkpoints = []
        if timeout > 60:
            checkpoints.append((timeout - 60, "还剩60秒..."))
        if timeout > 30:
            checkpoints.append((timeout - 30, "还剩30秒..."))
        if timeout > 10:
            checkpoints.append((timeout - 10, "还剩10秒...>.<"))
        checkpoints.append((timeout, None))

        elapsed = 0
        try:
            for target, message in checkpoints:
                await asyncio.sleep(max(target - elapsed, 0))
                elapsed = target
                session = self.sessions.get(umo)
                if not session or not session.process:
                    return
                if message:
                    await self.context.send_message(umo, MessageChain().message(message))

            session = self.sessions.get(umo)
            if not session or not session.process:
                return
            process = session.process
            process.reveal()
            answer = process.operator.name
            text = self._join_text(
                f"答案是{answer}，没有博士回答吗？那游戏结束咯~",
                "【本题揭晓】",
                process.render_text_board(),
            )
            self._end_session(umo)
            await self.context.send_message(umo, MessageChain().message(text))
        except asyncio.CancelledError:
            return

    def _end_session(self, umo: str) -> None:
        session = self.sessions.pop(umo, None)
        if session:
            self._cancel_timeout(session)

    def _is_game_input(self, text: str, session: GameSession | None = None) -> bool:
        target = session.process.operator if session and session.process else None
        return text in CONTROL_WORDS or self.repository.find(text, target=target) is not None

    def _parse_mode(self, text: str) -> bool | None:
        if "硬核" in text:
            return True
        if "普通" in text:
            return False
        return None

    def _command_payload(self, text: str) -> str:
        normalized = text.strip()
        prefixes = ("/猜干员", "猜干员")
        lowered = normalized.lower()
        for prefix in prefixes:
            if lowered == prefix:
                return ""
            if lowered.startswith(prefix + " "):
                return normalized[len(prefix):].strip()
        return normalized

    def _game_text_from_command_payload(self, payload: str) -> str:
        if not payload or self._parse_mode(payload) is not None:
            return ""
        return payload.strip(" .。\t\r\n")

    @staticmethod
    def _is_start_trigger(text: str) -> bool:
        return START_TRIGGER_PATTERN.match(text.strip()) is not None

    def _intro_text(self) -> str:
        return (
            "承蒙大家的热情好意！感谢大帝的不胜酒意（？）！\n"
            "WHATEVER，《大帝的CYPHER挑战》AstrBot 版启动！\n"
            "所有群员均可参与游戏。发送干员名竞猜，也可以发送【提示】【跳过】【结束】。\n"
            "PUT UR HANDS UP！\n\n"
            "请选择难度：\n\n【普通】\n【硬核】"
        )

    def _load_board_template(self) -> str:
        template_dir = self.base_dir / "template"
        template = (template_dir / "hardcode.html").read_text(encoding="utf-8")
        return template

    def _operator_data_path(self) -> Path:
        configured = self._config_get("operator_data_file", "")
        if configured:
            configured_path = Path(str(configured))
            candidates = [configured_path]
            if not configured_path.is_absolute():
                candidates.extend([self.base_dir / configured_path, Path.cwd() / configured_path])
                candidates.append(Path.cwd() / "data" / "plugins" / PLUGIN_ID / configured_path)
            for candidate in candidates:
                if candidate.is_file():
                    return candidate
        return self.base_dir / "data" / "operators.json"

    def _round_timeout(self) -> int:
        return max(int(self._config_get("round_timeout", 120)), 15)

    def _max_guesses(self) -> int:
        return max(int(self._config_get("max_guesses", 10)), 1)

    def _config_get(self, key: str, default: Any) -> Any:
        if self.config is None:
            return default
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return getattr(self.config, key, default)

    @staticmethod
    def _join_text(*parts: str) -> str:
        return "\n\n".join(part for part in parts if part)
