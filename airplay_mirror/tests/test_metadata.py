from __future__ import annotations

import asyncio
import base64
import os
import sys

import pytest

from airplay_mirror.metadata import (
    CORE,
    OWNTONE_ITEM_MAX,
    OWNTONE_PICT_MAX,
    SSNC,
    MetadataParser,
    MetadataRelay,
    TrackState,
    ssnc_item,
)


def item(type_: int, code: str, data: bytes | None = None) -> bytes:
    c = int.from_bytes(code.encode("latin1"), "big")
    head = f"<item><type>{type_:x}</type><code>{c:x}</code><length>{len(data or b'')}</length>".encode()
    if data:
        b64 = base64.b64encode(data).decode()
        lines = "\n".join(b64[i : i + 76] for i in range(0, len(b64), 76))
        return head + f'\n<data encoding="base64">\n{lines}</data></item>\n'.encode()
    return head + b"</item>\n"


BUNDLE = (
    ssnc_item("mdst")
    + item(CORE, "minm", b"Blue in Green")
    + item(CORE, "asar", b"Miles Davis")
    + item(CORE, "asal", b"Kind of Blue")
    + ssnc_item("mden")
)


def test_parser_handles_split_items():
    p = MetadataParser()
    data = BUNDLE + item(SSNC, "pvol", b"-15.0,-18.0,-30.0,0.0")
    items = []
    for i in range(0, len(data), 7):
        items.extend(p.feed(data[i : i + 7]))
    assert [i.name for i in items] == ["mdst", "minm", "asar", "asal", "mden", "pvol"]
    assert items[1].data == b"Blue in Green"
    assert items[-1].data == b"-15.0,-18.0,-30.0,0.0"
    assert b"".join(i.raw for i in items) == data
    assert not p.buf


def test_track_state_commits_on_mden_and_replays():
    p, st = MetadataParser(), TrackState()
    changes = [st.observe(i) for i in p.feed(BUNDLE)]
    assert changes == [False, False, False, False, True]
    info = st.info()
    assert (info.title, info.artist, info.album, info.has_artwork) == (
        "Blue in Green",
        "Miles Davis",
        "Kind of Blue",
        False,
    )
    for i in p.feed(item(SSNC, "pvol", b"-15.0,-18.0,-30.0,0.0") + item(SSNC, "PICT", b"\x89PNGfake")):
        st.observe(i)
    assert st.info().has_artwork
    replay = st.replay()
    assert replay.startswith(ssnc_item("mdst")) and b"Blue in Green" not in replay  # base64-encoded
    assert base64.b64encode(b"Blue in Green") in replay
    assert replay.count(b"<item>") == 7  # mdst, 3 core, mden, PICT, pvol
    assert replay.index(ssnc_item("mden")) < replay.index(b"PICT".hex().encode())  # picture after the bundle
    # a partial update outside a bundle (e.g. a lone title) is applied directly
    assert [st.observe(i) for i in p.feed(item(CORE, "minm", b"So What"))] == [True]
    assert st.info().title == "So What"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs fifos")
async def test_relay_replays_current_track_when_reader_appears(tmp_path):
    src, dst = str(tmp_path / "meta"), str(tmp_path / "pipe.metadata")
    os.mkfifo(src)
    os.mkfifo(dst)
    seen = []
    relay = MetadataRelay("g", src, dst, on_track=lambda gid, info: seen.append((gid, info.title)), retry=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(stop))
    await asyncio.sleep(0.1)
    # shairport-sync writes the bundle while nobody reads OwnTone's pipe: it must not be lost
    w = os.open(src, os.O_WRONLY | os.O_NONBLOCK)
    os.write(w, BUNDLE)
    os.close(w)
    await asyncio.sleep(0.15)
    assert seen == [("g", "Blue in Green")]
    assert not relay.connected
    # OwnTone starts playback and opens its metadata pipe: the current track is replayed
    r = os.open(dst, os.O_RDONLY | os.O_NONBLOCK)
    await asyncio.sleep(0.2)
    assert relay.connected
    got = os.read(r, 65536)
    assert base64.b64encode(b"Blue in Green") in got and got.startswith(ssnc_item("mdst"))
    # live items stream straight through
    w = os.open(src, os.O_WRONLY | os.O_NONBLOCK)
    os.write(w, item(CORE, "minm", b"So What"))
    os.close(w)
    await asyncio.sleep(0.15)
    assert base64.b64encode(b"So What") in os.read(r, 65536)
    assert seen[-1] == ("g", "So What")
    # OwnTone stops: the next write notices, and a later reader gets a replay again
    os.close(r)
    w = os.open(src, os.O_WRONLY | os.O_NONBLOCK)
    os.write(w, item(SSNC, "pvol", b"-10.0,-12.0,-30.0,0.0"))
    os.close(w)
    await asyncio.sleep(0.15)
    assert not relay.connected
    r = os.open(dst, os.O_RDONLY | os.O_NONBLOCK)
    await asyncio.sleep(0.2)
    got = os.read(r, 65536)
    assert (
        base64.b64encode(b"So What") in got and b"pvol" not in got and base64.b64encode(b"-10.0,-12.0,-30.0,0.0") in got
    )
    os.close(r)
    # the reader going away is noticed without a write (poll reports POLLERR on Linux, where the
    # add-on runs; macOS does not), so the next reader gets a replay promptly
    await asyncio.sleep(0.2)
    if sys.platform.startswith("linux"):
        assert not relay.connected
    else:
        relay._disconnect()
    r = os.open(dst, os.O_RDONLY | os.O_NONBLOCK)
    await asyncio.sleep(0.2)
    assert relay.connected
    os.read(r, 65536)
    # play-begin makes the relay resend the current track to a reader that may have reset
    w = os.open(src, os.O_WRONLY | os.O_NONBLOCK)
    os.write(w, ssnc_item("pbeg"))
    os.close(w)
    await asyncio.sleep(0.15)
    got = os.read(r, 65536)
    assert got.startswith(ssnc_item("pbeg")) and base64.b64encode(b"So What") in got
    os.close(r)
    stop.set()
    await task


def test_picture_sent_before_its_bundle_is_kept():
    p, st = MetadataParser(), TrackState()
    png = b"\x89PNG\r\n\x1a\nfirst"
    for i in p.feed(item(SSNC, "PICT", png) + BUNDLE):
        st.observe(i)
    assert st.info().title == "Blue in Green" and st.artwork()[0] == png
    # next track: bundle first, then its own picture, as phones do on track change
    png2 = b"\x89PNG\r\n\x1a\nsecond"
    for i in p.feed(ssnc_item("mdst") + item(CORE, "minm", b"So What") + ssnc_item("mden")):
        st.observe(i)
    assert st.info().title == "So What" and st.artwork() is None  # old art is not carried over
    for i in p.feed(item(SSNC, "PICT", png2)):
        st.observe(i)
    assert st.artwork()[0] == png2
    # a bundle that carries its own picture wins over an early one
    for i in p.feed(
        item(SSNC, "PICT", png)
        + ssnc_item("mdst")
        + item(CORE, "minm", b"X")
        + item(SSNC, "PICT", png2)
        + ssnc_item("mden")
    ):
        st.observe(i)
    assert st.artwork()[0] == png2


def _big_jpeg(side: int = 1400) -> bytes:
    from io import BytesIO

    from PIL import Image

    im = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    out = BytesIO()
    im.save(out, format="JPEG", quality=97)
    assert len(out.getvalue()) > OWNTONE_PICT_MAX
    return out.getvalue()


def test_big_artwork_is_shrunk_for_owntone_but_kept_for_us():
    big = _big_jpeg()
    p, st = MetadataParser(), TrackState()
    for i in p.feed(BUNDLE + item(SSNC, "PICT", big)):
        st.observe(i)
    assert st.artwork()[0] == big  # the panel and MQTT get the original
    replay = st.replay()
    assert len(replay) < OWNTONE_ITEM_MAX
    assert replay.count(b"<item>") == 6  # mdst, 3 core, mden, shrunk PICT
    pict_raw = st.current["PICT"].for_owntone()
    assert pict_raw is not None and len(pict_raw) < len(big)
    decoded = base64.b64decode(pict_raw.split(b'<data encoding="base64">\n')[1].split(b"</data>")[0])
    assert decoded.startswith(b"\xff\xd8") and len(decoded) <= OWNTONE_PICT_MAX


def test_oversized_non_picture_item_is_not_forwarded():
    p = MetadataParser()
    (it,) = p.feed(item(CORE, "minm", b"x" * (OWNTONE_ITEM_MAX + 10)))
    assert it.for_owntone() is None
    (small,) = p.feed(item(CORE, "minm", b"So What"))
    assert small.for_owntone() == small.raw
