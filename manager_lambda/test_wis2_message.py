"""Core behaviour of Wis2Message: parsing, dedup, embedded content, integrity and the republished notification.

Run inside a container (see README "Operations"); importing the module creates a boto3 client.
"""
import base64
import hashlib

import pytest

from wis2_message import Wis2Message, IntegrityError, ContentDecodeError

DATA = b"hello world"


def sha512(data: bytes) -> str:
    return base64.b64encode(hashlib.sha512(data).digest()).decode()


def wnm(links=None, content=None, integrity=None, cache=None, pubtime="2026-10-13T10:00:00Z"):
    props = {"data_id": "xx-test/data/1", "pubtime": pubtime}
    if content is not None:
        props["content"] = content
    if integrity is not None:
        props["integrity"] = {"method": "sha512", "value": integrity}
    if cache is not None:
        props["cache"] = cache
    return {"id": "msg-1", "type": "Feature", "geometry": None, "properties": props,
            "topic": "origin/a/wis2/xx-test/data/core/weather/surface-based-observations/synop",
            "links": links or [{"rel": "canonical", "href": "https://node.example/data/file.bufr?token=abc",
                                "type": "application/x-bufr"}]}


def test_parses_source_link_dataserver_and_cache_topic():
    m = Wis2Message(wnm())
    assert m.dataserver == "node.example"
    assert m.filename == "file.bufr"
    assert m.new_topic.startswith("cache/a/wis2/xx-test/")


def test_update_link_preferred_over_canonical():
    links = [{"rel": "canonical", "href": "https://node.example/old.bufr"},
             {"rel": "update", "href": "https://node.example/new.bufr"}]
    assert Wis2Message(wnm(links=links)).src_link == "https://node.example/new.bufr"


def test_missing_required_key_raises():
    msg = wnm()
    del msg["properties"]["data_id"]
    with pytest.raises(Exception):
        Wis2Message(msg)


def test_cache_false_is_honoured():
    assert Wis2Message(wnm(cache=False)).do_cache is False
    assert Wis2Message(wnm()).do_cache is True


def test_dedup_by_pubtime():
    m = Wis2Message(wnm())
    assert m.is_unique(None)
    assert not m.is_unique(m.pubtime_epoch)
    assert not m.is_unique(m.pubtime_epoch + 1)


def test_newer_pubtime_with_update_link_is_unique():
    links = [{"rel": "update", "href": "https://node.example/new.bufr"}]
    m = Wis2Message(wnm(links=links))
    assert m.is_unique(m.pubtime_epoch - 1)


@pytest.mark.parametrize("encoding,value", [
    ("utf-8", DATA.decode()),
    ("base64", base64.b64encode(DATA).decode()),
])
def test_embedded_content_decodes_without_download(encoding, value, monkeypatch):
    m = Wis2Message(wnm(content={"encoding": encoding, "value": value, "size": len(DATA)}, integrity=sha512(DATA)))
    monkeypatch.setattr(m, "download_file", lambda href: pytest.fail("should not download"))
    assert m.cache_msg_data(use_content=True) == DATA
    assert m.used_content
    assert m.validate_integrity()


def test_line_wrapped_base64_is_accepted():
    wrapped = base64.encodebytes(DATA * 10).decode()
    m = Wis2Message(wnm(content={"encoding": "base64", "value": wrapped, "size": len(DATA) * 10}))
    assert m.cache_msg_data(use_content=True) == DATA * 10


@pytest.mark.parametrize("content", [
    {"encoding": "base64", "value": "!!not base64!!", "size": 11},
    {"encoding": "utf-8", "value": "hello world", "size": 99},
    {"encoding": "latin-1", "value": "hello world", "size": 11},
    {"encoding": "utf-8", "value": 12345, "size": 5},
    {"encoding": "base64", "value": "aGVsbG8=\u00e9", "size": 5},
    {"encoding": "utf-8", "value": "\ud800", "size": 3},
])
def test_invalid_embedded_content_raises_decode_error(content):
    m = Wis2Message(wnm(content=content))
    with pytest.raises(ContentDecodeError):
        m.cache_msg_data(use_content=True)


def test_gzip_content_is_downloaded_not_decoded(tmp_path, monkeypatch):
    path = tmp_path / "file.bufr"
    path.write_bytes(DATA)
    m = Wis2Message(wnm(content={"encoding": "gzip", "value": "H4sI", "size": len(DATA)}))
    monkeypatch.setattr(m, "download_file", lambda href: str(path))
    assert m.cache_msg_data(use_content=True) == DATA
    assert not m.used_content


def test_integrity_mismatch_raises(tmp_path, monkeypatch):
    path = tmp_path / "file.bufr"
    path.write_bytes(DATA)
    m = Wis2Message(wnm(integrity=sha512(b"other")))
    monkeypatch.setattr(m, "download_file", lambda href: str(path))
    m.cache_msg_data()
    with pytest.raises(IntegrityError):
        m.validate_integrity()


def test_missing_integrity_is_computed_from_data(tmp_path, monkeypatch):
    path = tmp_path / "file.bufr"
    path.write_bytes(DATA)
    m = Wis2Message(wnm())
    monkeypatch.setattr(m, "download_file", lambda href: str(path))
    m.cache_msg_data()
    assert m.integrity_block == {"method": "sha512", "value": sha512(DATA)}


def test_republished_notification_changes_id_link_and_global_cache(monkeypatch):
    monkeypatch.setenv("REPORT_BY", "xx-test-global-cache")
    m = Wis2Message(wnm())
    m.dnld_url = "https://cache.example/file.bufr"
    out = m.format_cache_msg()
    assert out["id"] == m.new_uuid != "msg-1"
    assert out["properties"]["global-cache"] == "xx-test-global-cache"
    assert [l["href"] for l in out["links"] if l["rel"] == "canonical"] == ["https://cache.example/file.bufr"]
    assert "topic" not in out
