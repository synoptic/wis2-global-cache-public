"""Core rules for WIS2 Monitoring Events: topic, required fields, size limit and removal of presigned tokens."""
import json

import pytest

import wis2_monitor as m


def test_topic_is_four_levels_on_subject():
    assert m.monitor_topic("xx-test-node") == "monitor/a/wis2/xx-test-node"


@pytest.mark.parametrize("bad", ["", "nodash", "Upper-case", "xx/test", None])
def test_invalid_centre_id_rejected(bad):
    with pytest.raises(ValueError):
        m.monitor_topic(bad)


def test_build_wme_sets_required_fields():
    wme = m.build_wme(m.EVENT_ITEM_DOWNLOAD, "ERROR", source="xx-test-global-cache", subject="xx-test-node",
                      title="Cannot download data item")
    for key in ("id", "specversion", "type", "source", "subject", "time", "datacontenttype", "dataschema", "data"):
        assert key in wme
    assert wme["data"]["severity"] == "ERROR"
    assert wme["data"]["conformsTo"] == [m.WME_CONFORMANCE]


def test_build_wme_rejects_unknown_severity():
    with pytest.raises(ValueError):
        m.build_wme(m.EVENT_ITEM_CACHE, "FATAL", source="xx-test-global-cache", subject="xx-test-node", title="t")


def test_encode_truncates_description_to_size_limit():
    wme = m.build_wme(m.EVENT_ITEM_CACHE, "INFO", source="xx-test-global-cache", subject="xx-test-node",
                      title="t", description="x" * 20000)
    payload = m.encode_wme(wme)
    assert len(payload) <= m.WME_MAX_BYTES
    assert json.loads(payload)["data"]["content"]["description"].endswith("...")


def test_presigned_query_removed_from_text_and_tracebacks():
    text = "GET https://node.example/f.bufr?X-Amz-Signature=SECRET failed\nurl: /f.bufr?X-Amz-Signature=SECRET"
    cleaned = m.strip_url_queries(text)
    assert "SECRET" not in cleaned
    assert "https://node.example/f.bufr failed\n" in cleaned


def test_sanitize_text_removes_control_characters_and_bounds_length():
    assert m.sanitize_text("a\x00b\nc" + "z" * 100, 10) == "a b c" + "z" * 5
