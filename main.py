from __future__ import annotations

import asyncio
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator

import jmcomic
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, StarTools, register

try:
    from astrbot.core.star.filter.command import GreedyStr
except ImportError:  # 兼容没有 GreedyStr 的旧版本
    GreedyStr = str

PLUGIN_NAME = "astrbot_plugin_comic_download"
MAX_ALBUM_CHAPTERS = 5

USAGE = (
    "用法：\n"
    "/jm <本子ID或链接> —— 下载整本（多章节会按章节分别发送 PDF）\n"
    "/jm <本子ID或链接> p<章节序号> —— 下载该本子的第 x 话\n"
    "/jmv <本子ID或链接> —— 查询本子详情"
)

_URL_ID_RE = re.compile(r"/(album|photo)/(\d+)", re.IGNORECASE)
_QUERY_ID_RE = re.compile(r"[?&]id=(\d+)")
_NUMBER_RE = re.compile(r"(\d+)")

_PHOTO_ALIASES = {"photo", "chapter", "章节", "单章", "章"}
_ALBUM_ALIASES = {"album", "本子", "整本", "全部"}


def _parse_target(first: str, second: str) -> tuple[str, str] | None:
    """解析用户输入，返回 (album|photo, id)。"""
    first = (first or "").strip()
    second = (second or "").strip()

    mode = "album"
    if first.lower() in _PHOTO_ALIASES:
        mode, raw = "photo", second
    elif first.lower() in _ALBUM_ALIASES:
        mode, raw = "album", second
    else:
        raw = first or second

    if not raw:
        return None

    # 与原项目 CLI 一致：p<id> 表示章节(photo)，a<id> 表示本子(album)
    if len(raw) > 1 and raw[0].lower() in ("p", "a") and raw[1:].isdigit():
        return ("photo" if raw[0].lower() == "p" else "album"), raw[1:]

    url_match = _URL_ID_RE.search(raw)
    if url_match:
        mode = "photo" if url_match.group(1).lower() == "photo" else "album"
        return mode, url_match.group(2)

    query_match = _QUERY_ID_RE.search(raw)
    if query_match:
        return mode, query_match.group(1)

    number_match = _NUMBER_RE.search(raw)
    if number_match:
        return mode, number_match.group(1)

    return None


def _parse_jm_command(
    text: str,
) -> tuple[tuple[str, str, str | None] | None, str | None]:
    """解析 /jm 参数：<本子ID> 或 <本子ID> p<章节序号>。

    返回 ((mode, jm_id, chapter_index), error)。
    """
    tokens = (text or "").split()
    if not tokens:
        return None, USAGE

    if len(tokens) == 1:
        parsed = _parse_target(tokens[0], "")
        if parsed is None:
            return None, USAGE
        mode, jm_id = parsed
        if mode != "album":
            return None, "下载指定章节请使用：/jm <本子ID或链接> p<章节序号>。"
        return ("album", jm_id, None), None

    if len(tokens) == 2:
        parsed = _parse_target(tokens[0], "")
        chapter_match = re.fullmatch(r"[pP](\d+)", tokens[1])
        if parsed is None or parsed[0] != "album" or chapter_match is None:
            return None, "用法：/jm <本子ID或链接> p<章节序号>"
        return ("chapter", parsed[1], chapter_match.group(1)), None

    return None, USAGE


def _parse_album_query(raw: str) -> str | None:
    """按原项目 jmv 的习惯解析本子 ID：支持 /album/ 链接、a 前缀、纯文本中的数字。"""
    raw = (raw or "").strip()
    if not raw:
        return None

    url_match = _URL_ID_RE.search(raw)
    if url_match:
        if url_match.group(1).lower() != "album":
            return None
        return url_match.group(2)

    if raw[0].lower() == "a" and raw[1:].isdigit():
        return raw[1:]
    if raw[0].lower() == "p":
        return None

    numbers = re.findall(r"\d+", raw)
    if numbers:
        # 与原项目 jmv 一致："350谁还没看过234" -> "350234"
        return "".join(numbers)
    return None


def _split_text(text: str, limit: int = 2500) -> list[str]:
    """按行切分长文本，避免超出聊天平台单条消息长度限制。"""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if buffer:
                chunks.append(buffer.rstrip("\n"))
                buffer = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buffer) + len(line) > limit:
            chunks.append(buffer.rstrip("\n"))
            buffer = ""
        buffer += line
    if buffer.strip():
        chunks.append(buffer.rstrip("\n"))
    return chunks


def _sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or "comic"


@register(
    PLUGIN_NAME,
    "Aurora",
    "使用 JMComic 下载漫画并按章节发送 PDF，支持查询本子详情。",
    "0.5.0",
)
class ComicDownloadPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._running_sessions: set[str] = set()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._temp_root: Path | None = None

    # ------------------------------------------------------------------ lifecycle

    async def initialize(self):
        try:
            self._temp_root = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
            self._temp_root.mkdir(parents=True, exist_ok=True)
            self._cleanup_stale_dirs(self._temp_root)
        except Exception:
            logger.exception("[comic_download] 初始化临时目录失败")

    async def terminate(self):
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()

    # ------------------------------------------------------------------ commands

    @filter.command("jm", alias={"jmcomic", "漫画下载"})
    async def download(self, event: AstrMessageEvent, args: GreedyStr):
        """下载整本，或下载本子的第 x 话（/jm <本子ID> p<序号>）。"""
        target, error = _parse_jm_command(str(args))
        if target is None:
            yield event.plain_result(error or USAGE)
            return
        async for result in self._handle(event, *target):
            yield result

    @filter.command("jmv", alias={"jm查询", "jm详情", "本子详情"})
    async def view_album(self, event: AstrMessageEvent, query: str = ""):
        """查询本子详情，输出样式与原项目 jmv 一致。"""
        album_id = _parse_album_query(query)
        if album_id is None:
            yield event.plain_result("用法：/jmv <本子ID或链接>")
            return

        yield event.plain_result(f"正在查询本子 {album_id} 的详情……")
        try:
            text = await asyncio.to_thread(self._query_album_sync, album_id)
        except Exception as e:
            logger.exception("[comic_download] 查询本子详情失败: %s", e)
            yield event.plain_result(f"查询失败：{e}")
            return

        for chunk in _split_text(text):
            yield event.plain_result(chunk)

    async def _handle(
        self,
        event: AstrMessageEvent,
        mode: str,
        jm_id: str,
        chapter: str | None = None,
    ) -> AsyncGenerator:
        session_key = event.unified_msg_origin
        if session_key in self._running_sessions:
            yield event.plain_result("当前会话已有下载任务正在进行，请等待完成后再试。")
            return

        self._running_sessions.add(session_key)
        workdir: Path | None = None
        try:
            if mode == "chapter":
                label = f"本子 {jm_id} 的第{chapter}话"
            else:
                label = f"本子 {jm_id}"
            yield event.plain_result(
                f"开始下载{label}，下载完成后会按章节发送 PDF，请稍候……"
            )

            photo_ids, notice = await asyncio.to_thread(
                self._prepare_sync, mode, jm_id, chapter
            )
            if notice:
                yield event.plain_result(notice)

            workdir = self._create_workdir()
            pdfs, warning = await asyncio.to_thread(
                self._download_photos_sync, photo_ids, workdir
            )

            if not pdfs:
                yield event.plain_result(
                    f"没有生成 PDF：下载失败或该目标没有可用图片。{warning}"
                )
                return

            yield event.plain_result(
                f"下载完成，共 {len(pdfs)} 个章节 PDF，正在发送……{warning}"
            )

            chain = [File(name=name, file=str(path)) for name, path in pdfs]
            yield event.chain_result(chain)
        except Exception as e:
            logger.exception("[comic_download] 下载失败: %s", e)
            yield event.plain_result(f"下载失败：{e}")
        finally:
            self._running_sessions.discard(session_key)
            if workdir is not None:
                self._schedule_cleanup(workdir)

    # ------------------------------------------------------------------ downloading

    def _prepare_sync(
        self, mode: str, jm_id: str, chapter: str | None = None
    ) -> tuple[list[str], str]:
        """查询本子章节列表，确定要下载的章节（整本最多 5 话）。"""
        option = self._build_query_option()
        client = option.new_jm_client()
        album = client.get_album_detail(jm_id)

        if mode == "chapter":
            return [self._resolve_photo_id(album, chapter)], ""

        return self._select_album_photo_ids(album)

    @staticmethod
    def _select_album_photo_ids(album) -> tuple[list[str], str]:
        photo_ids = [str(entry[0]) for entry in album.episode_list]
        selected = photo_ids[:MAX_ALBUM_CHAPTERS]
        if len(photo_ids) > MAX_ALBUM_CHAPTERS:
            notice = (
                f"该本子共 {len(photo_ids)} 话，最多发送前 "
                f"{MAX_ALBUM_CHAPTERS} 个章节，正在下载……"
            )
            return selected, notice
        return selected, ""

    @staticmethod
    def _resolve_photo_id(album, chapter: str | None) -> str:
        """把“本子 + 第 x 话”解析为该话的 photo_id。"""
        try:
            target_index = str(int(str(chapter)))
        except (TypeError, ValueError):
            target_index = str(chapter)

        for entry in album.episode_list:
            pid, pindex = entry[0], entry[1]
            try:
                if str(int(pindex)) == target_index:
                    return str(pid)
            except (TypeError, ValueError):
                if str(pindex) == target_index:
                    return str(pid)

        raise ValueError(
            f"本子 {album.album_id} 没有第{chapter}话"
            f"（该本子共 {len(album.episode_list)} 话）"
        )

    def _download_photos_sync(
        self, photo_ids: list[str], workdir: Path
    ) -> tuple[list[tuple[str, Path]], str]:
        """在线程中批量下载章节并返回 PDF 列表。"""
        option = self._build_option(workdir)
        batch = jmcomic.download_photo(photo_ids, option, check_exception=False)

        pdfs: list[tuple[str, Path]] = []
        notes: list[str] = []
        for item in batch:
            pdf_paths = [
                Path(p)
                for p in item.manifest.get_export_filepath_list("pdf")
                if p and Path(p).is_file()
            ]
            pdfs.extend(self._rename_pdfs(item, pdf_paths))

            downloader = item.downloader
            if downloader.has_download_failures:
                notes.append(
                    f"{len(downloader.download_failed_photo)} 个章节、"
                    f"{len(downloader.download_failed_image)} 张图片下载失败"
                )

        if batch.failed:
            notes.append(f"{len(batch.failed)} 个章节请求失败")

        warning = f"（注意：{'；'.join(notes)}）" if notes else ""
        return pdfs, warning

    def _build_option(self, workdir: Path) -> jmcomic.JmOption:
        option_dict = {
            "dir_rule": {
                "base_dir": str(workdir / "images"),
                "rule": "Bd_Pname",
            },
            "download": {
                "cache": False,
                "threading": {"image": 8, "photo": 4},
            },
            "plugins": {
                "after_photo": [
                    {
                        "plugin": "img2pdf",
                        "kwargs": {
                            "pdf_dir": str(workdir / "pdf"),
                            "filename_rule": "Pid",
                            "delete_original_file": True,
                        },
                    }
                ]
            },
        }

        proxy = self._config_str("proxy", "")
        if proxy:
            option_dict["client"] = {"postman": {"meta_data": {"proxies": proxy}}}

        return jmcomic.JmOption.construct(option_dict)

    # ------------------------------------------------------------------ querying

    def _query_album_sync(self, album_id: str) -> str:
        option = self._build_query_option()
        client = option.new_jm_client()
        album = client.get_album_detail(album_id)
        return self._format_album_detail(album)

    def _build_query_option(self) -> jmcomic.JmOption:
        option_dict = {}
        proxy = self._config_str("proxy", "")
        if proxy:
            option_dict["client"] = {"postman": {"meta_data": {"proxies": proxy}}}
        return jmcomic.JmOption.construct(option_dict)

    @staticmethod
    def _truncate_list(items, limit: int = 10) -> str:
        if len(items) <= limit:
            return ", ".join(items)
        return ", ".join(items[:limit]) + f" ...等{len(items)}个"

    def _format_album_detail(self, album) -> str:
        """完全沿用原项目 jmv (JmViewUI.print_album_detail) 的输出样式。"""
        sep = "─" * 50
        lines = [
            sep,
            f"  📖 标题:  {album.name}",
            f"  🆔 ID:    JM{album.album_id}",
            f"  🔗 链接:  {jmcomic.JmcomicText.format_album_url(album.album_id)}",
            f"  🎨 封面:  {jmcomic.JmcomicText.get_album_cover_url(album.album_id)}",
            f"  ✍️ 作者:  {self._truncate_list(album.authors) if album.authors else '未知'}",
            sep,
            f"  📅 发布日期:  {album.pub_date}",
            f"  📅 更新日期:  {album.update_date}",
            f"  📄 总页数:    {album.page_count}",
            f"  👀 观看:      {album.views}",
            f"  ❤️ 点赞:     {album.likes}",
            f"  💬 评论:      {album.comment_count}",
            sep,
        ]

        if album.tags:
            lines.append(f"  🏷️ 标签:  {self._truncate_list(album.tags)}")
        if album.actors:
            lines.append(f"  🎭 人物:  {self._truncate_list(album.actors)}")
        if album.works:
            lines.append(f"  📚 作品:  {self._truncate_list(album.works)}")
        if album.description:
            lines.append(f"  📝 简介:  {album.description}")

        lines.append(sep)
        lines.append(f"  📑 章节 ({len(album.episode_list)}):")
        for pid, pindex, pname in album.episode_list:
            lines.append(f"     第{pindex}話  {pname.strip()}  (id: {pid})")
        lines.append(sep)

        return "\n".join(lines)

    def _rename_pdfs(self, result, pdf_paths: list[Path]) -> list[tuple[str, Path]]:
        """把以章节 ID 命名的 PDF 重命名为可读的章节名，并按章节顺序排列。"""
        album = result.album
        photo_map = {}

        if result.photo is not None:
            photo_map[result.photo.photo_id] = result.photo
        if album is not None:
            try:
                for photo in album:
                    photo_map[photo.photo_id] = photo
            except Exception:
                logger.debug("[comic_download] 遍历章节列表失败", exc_info=True)

        entries: list[tuple[int, str, Path]] = []
        for pdf_path in pdf_paths:
            photo = photo_map.get(pdf_path.stem)
            if photo is not None:
                order = getattr(photo, "album_index", 0) or 0
                title = self._photo_display_name(photo, album)
            else:
                order = len(entries)
                title = pdf_path.stem
            entries.append((order, title, pdf_path))

        entries.sort(key=lambda item: item[0])

        renamed: list[tuple[str, Path]] = []
        used_names: set[str] = set()
        for _, title, pdf_path in entries:
            base = _sanitize_filename(title)
            final_name = f"{base}.pdf"
            index = 2
            while final_name in used_names:
                final_name = f"{base} ({index}).pdf"
                index += 1
            used_names.add(final_name)

            final_path = pdf_path.with_name(final_name)
            if final_path != pdf_path:
                try:
                    pdf_path.replace(final_path)
                except OSError:
                    logger.warning(
                        "[comic_download] 重命名 PDF 失败: %s", pdf_path, exc_info=True
                    )
                    final_path = pdf_path
                    final_name = pdf_path.name
            renamed.append((final_name, final_path))

        return renamed

    @staticmethod
    def _photo_display_name(photo, album) -> str:
        title = getattr(photo, "indextitle", "") or getattr(photo, "name", "") or getattr(photo, "photo_id", "chapter")
        album_title = ""
        if album is not None:
            album_title = getattr(album, "oname", "") or getattr(album, "title", "")
        if album_title and album_title not in title:
            return f"{album_title} - {title}"
        return title

    # ------------------------------------------------------------------ temp files

    def _create_workdir(self) -> Path:
        root = self._temp_root
        if root is None:
            root = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
            root.mkdir(parents=True, exist_ok=True)
            self._temp_root = root
        workdir = root / uuid.uuid4().hex
        workdir.mkdir(parents=True, exist_ok=True)
        return workdir

    def _cleanup_stale_dirs(self, root: Path, max_age_seconds: int = 86400) -> None:
        cutoff = time.time() - max_age_seconds
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                logger.debug("[comic_download] 清理旧目录失败: %s", child, exc_info=True)

    def _schedule_cleanup(self, workdir: Path) -> None:
        delay = max(self._config_int("cleanup_seconds", 1800), 60)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            shutil.rmtree(workdir, ignore_errors=True)
            return

        task = loop.create_task(self._cleanup_later(workdir, delay))
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    @staticmethod
    async def _cleanup_later(workdir: Path, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        shutil.rmtree(workdir, ignore_errors=True)

    # ------------------------------------------------------------------ config helpers

    def _config_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        return str(value).strip() if value is not None else default

    def _config_int(self, key: str, default: int = 0) -> int:
        value = self.config.get(key, default) if hasattr(self.config, "get") else default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
