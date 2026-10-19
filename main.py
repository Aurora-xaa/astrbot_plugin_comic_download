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

PLUGIN_NAME = "astrbot_plugin_comic_download"

USAGE = (
    "用法：\n"
    "/jm <本子ID或链接> —— 下载整本（多章节会按章节分别发送 PDF）\n"
    "/jmc <章节ID或链接> —— 只下载指定章节\n"
    "/jm p<章节ID> —— 只下载指定章节（与原项目 CLI 的 p 前缀写法一致）\n"
    "示例：/jm 422866 或 /jm https://18comic.vip/album/422866/\n"
    "示例：/jmc 777001 或 /jm p777001\n"
    "章节ID就是章节页链接 /photo/<章节ID>/ 中的数字。"
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


def _sanitize_filename(name: str, max_length: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name or "comic"


@register(
    PLUGIN_NAME,
    "Aurora",
    "使用 JMComic 下载漫画，按章节转换为 PDF 并发送。",
    "0.1.0",
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
    async def download_album(
        self, event: AstrMessageEvent, first: str = "", second: str = ""
    ):
        """下载整本漫画，多章节会按章节分别发送 PDF。"""
        async for result in self._handle(event, first, second, forced_mode=None):
            yield result

    @filter.command("jmc", alias={"jmchapter", "jm章节"})
    async def download_chapter(
        self, event: AstrMessageEvent, first: str = "", second: str = ""
    ):
        """下载指定章节并发送 PDF。"""
        async for result in self._handle(event, first, second, forced_mode="photo"):
            yield result

    async def _handle(
        self,
        event: AstrMessageEvent,
        first: str,
        second: str,
        forced_mode: str | None,
    ) -> AsyncGenerator:
        target = _parse_target(first, second)
        if target is None:
            yield event.plain_result(USAGE)
            return

        mode, jm_id = target
        if forced_mode:
            mode = forced_mode

        session_key = event.unified_msg_origin
        if session_key in self._running_sessions:
            yield event.plain_result("当前会话已有下载任务正在进行，请等待完成后再试。")
            return

        self._running_sessions.add(session_key)
        workdir: Path | None = None
        try:
            workdir = self._create_workdir()
            kind = "章节" if mode == "photo" else "本子"
            yield event.plain_result(
                f"开始下载{kind} {jm_id}，下载完成后会按章节发送 PDF，请稍候……"
            )

            pdfs, warning = await asyncio.to_thread(
                self._download_sync, mode, jm_id, workdir
            )

            if not pdfs:
                yield event.plain_result(
                    f"没有生成 PDF：下载失败或该{kind}没有可用图片。{warning}"
                )
                return

            max_chapters = self._config_int("max_chapters", 0)
            if max_chapters > 0 and len(pdfs) > max_chapters:
                skipped = len(pdfs) - max_chapters
                pdfs = pdfs[:max_chapters]
                warning += f"（按配置仅发送前 {max_chapters} 个章节，跳过 {skipped} 个）"

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

    def _download_sync(
        self, mode: str, jm_id: str, workdir: Path
    ) -> tuple[list[tuple[str, Path]], str]:
        """在线程中执行同步下载并返回 PDF 列表。"""
        option = self._build_option(workdir)

        if mode == "photo":
            result = jmcomic.download_photo(jm_id, option, check_exception=False)
        else:
            result = jmcomic.download_album(jm_id, option, check_exception=False)

        warning = ""
        downloader = result.downloader
        if downloader.has_download_failures:
            failed_photos = len(downloader.download_failed_photo)
            failed_images = len(downloader.download_failed_image)
            warning = (
                f"（注意：{failed_photos} 个章节、{failed_images} 张图片下载失败）"
            )

        pdf_paths = [
            Path(p)
            for p in result.manifest.get_export_filepath_list("pdf")
            if p and Path(p).is_file()
        ]
        return self._rename_pdfs(result, pdf_paths), warning

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
