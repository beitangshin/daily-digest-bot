import pytest

from daily_digest.channels import get_channel, load_channels, sources_file_for

_YAML = """
channels:
  - key: ai
    name: "AI 行业日报"
    domain_desc: "人工智能（AI）行业"
    topics: [模型发布, 产品应用]
  - key: autonomous_driving
    name: "自动驾驶日报"
    domain_desc: "自动驾驶行业"
    topics: [车企与新车动态]
"""


def _write(tmp_path, content=_YAML):
    path = tmp_path / "channels.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_channels_appends_implicit_topics(tmp_path):
    path = _write(tmp_path)
    channels = load_channels(path)
    assert [c.key for c in channels] == ["ai", "autonomous_driving"]
    ai = channels[0]
    assert ai.topics == ["模型发布", "产品应用", "其他", "无关"]


def test_load_missing_file_returns_empty_list(tmp_path):
    assert load_channels(tmp_path / "does-not-exist.yaml") == []


def test_get_channel_returns_match(tmp_path):
    path = _write(tmp_path)
    channel = get_channel("autonomous_driving", path)
    assert channel.name == "自动驾驶日报"


def test_get_channel_raises_for_unknown_key(tmp_path):
    path = _write(tmp_path)
    with pytest.raises(ValueError, match="未知频道"):
        get_channel("nope", path)


def test_sources_file_for_uses_channel_key(tmp_path):
    path = _write(tmp_path)
    channel = get_channel("ai", path)
    sources_dir = tmp_path / "sources"
    assert sources_file_for(channel, sources_dir) == sources_dir / "ai.yaml"
