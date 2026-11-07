import pytest

from app.config import ConfigError, settings_from_raw, settings_to_raw
from app.peer_address import peer_url


@pytest.mark.parametrize("address,url", [
    ("192.0.2.1", "http://192.0.2.1:8000"),
    ("192.0.2.1:9000", "http://192.0.2.1:9000"),
    ("2001:db8::1", "http://[2001:db8::1]:8000"),
    ("[2001:db8::1]", "http://[2001:db8::1]:8000"),
    ("[2001:db8::1]:9000", "http://[2001:db8::1]:9000"),
    ("https://[2001:db8::1]:8443", "https://[2001:db8::1]:8443"),
])
def test_transport_conversion_preserves_config(address, url):
    assert peer_url(address) == url
    settings = settings_from_raw({"peers": [address], "paused_peers": [address]})
    assert settings_to_raw(settings)["peers"] == [address]
    assert settings_to_raw(settings)["paused_peers"] == [address]


@pytest.mark.parametrize("address", ["", "bad ip", "192.0.2.1:0", "192.0.2.1:65536",
    "[2001:db8::1]:bad", "192.0.2.1/path", "user@192.0.2.1", "http://user:pass@host"])
def test_invalid_peer_rejected(address):
    with pytest.raises(ConfigError):
        settings_from_raw({"peers": [address]})
