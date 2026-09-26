"""Relay shairport-sync's metadata pipe into OwnTone's.

shairport-sync writes track metadata (title, artist, album, artwork, the phone's volume) to a named
pipe, but only if something has the pipe open for reading at that moment; otherwise the item is
dropped. OwnTone opens its ``<pipe>.metadata`` reader only once audio playback has started, which is
a second or two after the session begins, so the first track's details never arrived.

The relay sits in between: it always holds shairport-sync's pipe open, remembers the current track,
and whenever OwnTone starts listening it replays that track before streaming live items through.
It also gives the UI the track title.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

CORE = 0x636F7265  # 'core': DMAP items from the source (minm, asar, asal, ...)
SSNC = 0x73736E63  # 'ssnc': shairport-sync's own items (mdst, mden, pvol, PICT, ...)

ITEM_RE = re.compile(
    rb"<item><type>([0-9a-fA-F]+)</type><code>([0-9a-fA-F]+)</code><length>(\d+)</length>"
    rb'(?:\n<data encoding="base64">\n(.*?)</data>)?</item>\n',
    re.DOTALL,
)


def fourcc(code: int) -> str:
    return code.to_bytes(4, "big").decode("latin1")


def ssnc_item(name: str) -> bytes:
    code = int.from_bytes(name.encode("latin1"), "big")
    return f"<item><type>{SSNC:x}</type><code>{code:x}</code><length>0</length></item>\n".encode()


@dataclass
class Item:
    type: int
    code: int
    raw: bytes
    data: bytes | None

    @property
    def name(self) -> str:
        return fourcc(self.code)


class MetadataParser:
    """Splits the pipe's byte stream into items, tolerating items that span reads."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[Item]:
        self.buf += chunk
        items: list[Item] = []
        last = 0
        for m in ITEM_RE.finditer(self.buf):
            data = base64.b64decode(m.group(4)) if m.group(4) else None
            items.append(Item(int(m.group(1), 16), int(m.group(2), 16), bytes(m.group(0)), data))
            last = m.end()
        del self.buf[:last]
        if len(self.buf) > 4_000_000:  # never a valid item; drop garbage rather than grow forever
            self.buf.clear()
        return items


@dataclass
class TrackInfo:
    title: str = ""
    artist: str = ""
    album: str = ""
    has_artwork: bool = False
    artwork_id: str = ""  # short hash of the image bytes, so the UI can cache per image

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "has_artwork": self.has_artwork,
            "artwork_id": self.artwork_id,
        }


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    return None


class TrackState:
    """Remembers the current track bundle so it can be replayed to a late reader."""

    def __init__(self) -> None:
        self.current: dict[str, Item] = {}
        self.pending: dict[str, Item] | None = None
        self.volume: Item | None = None
        self._early_pict: Item | None = None  # a picture that arrived before its track's bundle

    def observe(self, item: Item) -> bool:
        """Record an item. Returns True when the visible track info may have changed."""
        name = item.name
        if item.type == SSNC:
            if name == "mdst":
                self.pending = {}
                return False
            if name == "mden":
                if self.pending is not None:
                    # Phones often send the artwork just before the bundle it belongs to; keep it.
                    if "PICT" not in self.pending and self._early_pict is not None:
                        self.pending["PICT"] = self._early_pict
                    self._early_pict = None
                    self.current = self.pending
                    self.pending = None
                    return True
                return False
            if name == "pvol":
                self.volume = item
                return False
            if name == "PICT":
                target = self.pending if self.pending is not None else self.current
                if item.data:
                    target["PICT"] = item
                    if self.pending is None:
                        self._early_pict = item
                else:
                    target.pop("PICT", None)
                    self._early_pict = None
                return self.pending is None
            return False
        if item.type == CORE:
            target = self.pending if self.pending is not None else self.current
            target[name] = item
            return self.pending is None
        return False

    def replay(self) -> bytes:
        """The current track as a bundle, then the picture, then the volume: the order phones use live."""
        out = bytearray()
        core = [i for name, i in self.current.items() if name != "PICT"]
        if core:
            out += ssnc_item("mdst")
            out += b"".join(i.raw for i in core)
            out += ssnc_item("mden")
        pict = self.current.get("PICT")
        if pict is not None:
            out += pict.raw
        if self.volume is not None:
            out += self.volume.raw
        return bytes(out)

    def info(self) -> TrackInfo:
        def text(name: str) -> str:
            item = self.current.get(name)
            return item.data.decode("utf-8", errors="replace").strip() if item and item.data else ""

        art = self.artwork()
        return TrackInfo(
            title=text("minm"),
            artist=text("asar"),
            album=text("asal"),
            has_artwork=art is not None,
            artwork_id=art[2] if art else "",
        )

    def artwork(self) -> tuple[bytes, str, str] | None:
        """(bytes, mime type, id) of the current cover art, if the source sent a JPEG or PNG."""
        item = self.current.get("PICT")
        if item is None or not item.data:
            return None
        mime = image_mime(item.data)
        if mime is None:
            return None
        return item.data, mime, hashlib.sha1(item.data).hexdigest()[:12]


TrackCallback = Callable[[str, TrackInfo], None]


class MetadataRelay:
    """Reads ``src`` (shairport-sync's pipe) forever; writes to ``dst`` (OwnTone's pipe) when it can."""

    def __init__(
        self, group_id: str, src: str, dst: str, on_track: TrackCallback | None = None, retry: float = 0.25
    ) -> None:
        self.group_id = group_id
        self.src = src
        self.dst = dst
        self.on_track = on_track
        self.retry = retry
        self.parser = MetadataParser()
        self.state = TrackState()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._src_fd: int | None = None
        self._src_keep: int | None = None  # a write end we hold so the reader never sees EOF
        self._dst_fd: int | None = None
        self._out = bytearray()
        self._writer_armed = False

    @property
    def connected(self) -> bool:
        return self._dst_fd is not None

    async def run(self, stop: asyncio.Event) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._src_fd = os.open(self.src, os.O_RDONLY | os.O_NONBLOCK)
            self._src_keep = os.open(self.src, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            log.error("[%s] cannot open metadata pipe %s: %s", self.group_id, self.src, exc)
            self._close_src()
            return
        self._loop.add_reader(self._src_fd, self._readable)
        try:
            while not stop.is_set():
                self._try_connect()
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self.retry)
        finally:
            self._disconnect()
            self._close_src()

    # ---- source side -------------------------------------------------------------------------------

    def _readable(self) -> None:
        assert self._src_fd is not None
        try:
            chunk = os.read(self._src_fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            log.warning("[%s] metadata read failed: %s", self.group_id, exc)
            return
        if not chunk:
            return
        for item in self.parser.feed(chunk):
            changed = self.state.observe(item)
            if self._dst_fd is not None:
                self._enqueue(item.raw)
            if changed and self.on_track:
                try:
                    self.on_track(self.group_id, self.state.info())
                except Exception:  # noqa: BLE001
                    log.exception("track callback failed")

    def _close_src(self) -> None:
        if self._loop is not None and self._src_fd is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_reader(self._src_fd)
        for fd in (self._src_fd, self._src_keep):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._src_fd = self._src_keep = None

    # ---- destination side --------------------------------------------------------------------------

    def _try_connect(self) -> None:
        if self._dst_fd is not None:
            return
        try:
            fd = os.open(self.dst, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno != errno.ENXIO:  # ENXIO just means nobody is reading yet
                log.debug("[%s] metadata destination %s: %s", self.group_id, self.dst, exc)
            return
        self._dst_fd = fd
        self._out = bytearray(self.state.replay())
        log.debug("[%s] OwnTone is reading metadata; replaying %d bytes", self.group_id, len(self._out))
        self._drain()

    def _enqueue(self, raw: bytes) -> None:
        self._out += raw
        self._drain()

    def _drain(self) -> None:
        assert self._loop is not None
        while self._out and self._dst_fd is not None:
            try:
                n = os.write(self._dst_fd, self._out)
            except BlockingIOError:
                if not self._writer_armed:
                    self._loop.add_writer(self._dst_fd, self._drain)
                    self._writer_armed = True
                return
            except OSError:  # EPIPE: OwnTone stopped reading
                self._disconnect()
                return
            del self._out[:n]
        if self._writer_armed and self._dst_fd is not None:
            self._loop.remove_writer(self._dst_fd)
            self._writer_armed = False

    def _disconnect(self) -> None:
        if self._dst_fd is None:
            return
        if self._writer_armed and self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.remove_writer(self._dst_fd)
        self._writer_armed = False
        with contextlib.suppress(OSError):
            os.close(self._dst_fd)
        self._dst_fd = None
        self._out.clear()
