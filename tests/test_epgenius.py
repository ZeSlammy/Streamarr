"""
Unit tests for services/epgenius.py

Run from the repo root:
    pip install pytest pytest-asyncio
    pytest tests/test_epgenius.py -v

No network calls are made — all httpx interactions are mocked.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_client(status_code: int = 200, text: str = "ok"):
    """Return a fake httpx.AsyncClient context manager that yields a response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = text

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client


def _fake_settings(**overrides):
    """Minimal settings object for testing."""
    defaults = {
        "epgenius_enabled": True,
        "epgenius_api_key": "test-api-key",
        "epgenius_discord_id": "123456789",
        "epgenius_playlist_id": "17365",
        "xtream_username": "user",
        "xtream_password": "pass",
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPushCredentials:

    def test_disabled(self):
        """Returns (False, 'disabled') when epgenius_enabled=False."""
        from services import epgenius

        with patch.object(epgenius, "settings", _fake_settings(epgenius_enabled=False)):
            ok, detail = asyncio.run(epgenius.push_credentials("http://new.provider.com:8080"))

        assert ok is False
        assert detail == "disabled"

    def test_missing_api_key(self):
        """Returns (False, 'not configured') when API key is empty."""
        from services import epgenius

        with patch.object(epgenius, "settings", _fake_settings(epgenius_api_key="")):
            ok, detail = asyncio.run(epgenius.push_credentials("http://new.provider.com:8080"))

        assert ok is False
        assert detail == "not configured"

    def test_missing_playlist_id(self):
        """Returns (False, 'not configured') when playlist ID is empty."""
        from services import epgenius

        with patch.object(epgenius, "settings", _fake_settings(epgenius_playlist_id="")):
            ok, detail = asyncio.run(epgenius.push_credentials("http://new.provider.com:8080"))

        assert ok is False
        assert detail == "not configured"

    def test_success_sends_correct_payload(self):
        """On 200, returns (True, 'ok') and sends the expected JSON payload."""
        from services import epgenius

        new_url = "http://new.provider.com:8080"
        mock_client = _make_mock_client(200)

        with patch.object(epgenius, "settings", _fake_settings()), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            ok, detail = asyncio.run(epgenius.push_credentials(new_url))

        assert ok is True
        assert detail == "ok"

        # Verify the POST was called with the right URL, headers, and payload.
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.args[0] == "https://epgenius.org/api/public/update_creds"

        sent_payload = call_kwargs.kwargs["json"]
        assert sent_payload["playlist_id"] == 17365  # must be int
        assert sent_payload["dns"] == new_url
        assert sent_payload["username"] == "user"
        assert sent_payload["password"] == "pass"

        sent_headers = call_kwargs.kwargs["headers"]
        assert sent_headers["Authorization"] == "test-api-key"
        assert sent_headers["X-Discord-ID"] == "123456789"

    def test_playlist_id_is_sent_as_int(self):
        """playlist_id stored as string in settings must arrive as int in the payload."""
        from services import epgenius

        mock_client = _make_mock_client(200)
        with patch.object(epgenius, "settings", _fake_settings(epgenius_playlist_id="17365")), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(epgenius.push_credentials("http://p.com"))

        sent = mock_client.post.call_args.kwargs["json"]
        assert isinstance(sent["playlist_id"], int)
        assert sent["playlist_id"] == 17365

    def test_playlist_id_non_numeric_sent_as_string(self):
        """Non-numeric playlist_id falls back to string (forward-compat)."""
        from services import epgenius

        mock_client = _make_mock_client(200)
        with patch.object(epgenius, "settings", _fake_settings(epgenius_playlist_id="abc-playlist")), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(epgenius.push_credentials("http://p.com"))

        sent = mock_client.post.call_args.kwargs["json"]
        assert sent["playlist_id"] == "abc-playlist"

    def test_http_4xx_returns_failure(self):
        """A 4xx from EPGenius returns (False, 'HTTP 4xx')."""
        from services import epgenius

        mock_client = _make_mock_client(401, "Unauthorized")
        with patch.object(epgenius, "settings", _fake_settings()), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            ok, detail = asyncio.run(epgenius.push_credentials("http://p.com"))

        assert ok is False
        assert "401" in detail

    def test_http_5xx_returns_failure(self):
        """A 5xx from EPGenius returns (False, 'HTTP 5xx')."""
        from services import epgenius

        mock_client = _make_mock_client(503, "Service Unavailable")
        with patch.object(epgenius, "settings", _fake_settings()), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            ok, detail = asyncio.run(epgenius.push_credentials("http://p.com"))

        assert ok is False
        assert "503" in detail

    def test_network_error_returns_failure(self):
        """Network errors return (False, <exception message>)."""
        import httpx as _httpx
        from services import epgenius

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(
            side_effect=_httpx.ConnectError("Connection refused")
        )

        with patch.object(epgenius, "settings", _fake_settings()), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            ok, detail = asyncio.run(epgenius.push_credentials("http://p.com"))

        assert ok is False
        assert "Connection refused" in detail

    def test_timeout_returns_failure(self):
        """Timeout returns (False, <exception message>)."""
        import httpx as _httpx
        from services import epgenius

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(
            side_effect=_httpx.TimeoutException("timed out")
        )

        with patch.object(epgenius, "settings", _fake_settings()), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            ok, detail = asyncio.run(epgenius.push_credentials("http://p.com"))

        assert ok is False


# ---------------------------------------------------------------------------
# verify_m3u tests
# ---------------------------------------------------------------------------

_SAMPLE_M3U = """\
#EXTM3U
#EXTINF:-1 tvg-id="foo" group-title="Bar",Channel Name
http://provider.example.com:8080/live/user/pass/12345.ts
#EXTINF:-1 tvg-id="baz" group-title="Qux",Another Channel
http://provider.example.com:8080/live/user/pass/99999.ts
"""

_SAMPLE_M3U_NO_STREAMS = """\
#EXTM3U
#EXTINF:-1,Just a comment line
not-a-url
"""


class TestVerifyM3u:

    def _make_get_client(self, status_code: int = 200, text: str = _SAMPLE_M3U):
        mock_response = MagicMock()
        mock_response.status_code = status_code
        mock_response.text = text
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_response)
        return mock_client

    def test_m3u_url_not_configured(self):
        from services import epgenius
        s = _fake_settings()
        s.epgenius_m3u_url = ""
        with patch.object(epgenius, "settings", s):
            result = asyncio.run(epgenius.verify_m3u("http://provider.example.com:8080"))
        assert result["ok"] is False
        assert "not configured" in result["detail"]

    def test_match(self):
        """M3U base URL matches active URL → ok=True."""
        from services import epgenius
        s = _fake_settings()
        s.epgenius_m3u_url = "https://drive.example.com/playlist.m3u"
        mock_client = self._make_get_client(200, _SAMPLE_M3U)
        with patch.object(epgenius, "settings", s), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(epgenius.verify_m3u("http://provider.example.com:8080"))
        assert result["ok"] is True
        assert result["m3u_base"] == "http://provider.example.com:8080"
        assert result["active_base"] == "http://provider.example.com:8080"

    def test_mismatch(self):
        """M3U still points at old URL → ok=False with both bases in result."""
        from services import epgenius
        s = _fake_settings()
        s.epgenius_m3u_url = "https://drive.example.com/playlist.m3u"
        mock_client = self._make_get_client(200, _SAMPLE_M3U)
        with patch.object(epgenius, "settings", s), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(epgenius.verify_m3u("http://new-provider.com:8080"))
        assert result["ok"] is False
        assert result["m3u_base"] == "http://provider.example.com:8080"
        assert result["active_base"] == "http://new-provider.com:8080"

    def test_http_error_on_fetch(self):
        from services import epgenius
        s = _fake_settings()
        s.epgenius_m3u_url = "https://drive.example.com/playlist.m3u"
        mock_client = self._make_get_client(403)
        with patch.object(epgenius, "settings", s), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(epgenius.verify_m3u("http://provider.example.com:8080"))
        assert result["ok"] is False
        assert "403" in result["detail"]

    def test_no_stream_urls_in_m3u(self):
        from services import epgenius
        s = _fake_settings()
        s.epgenius_m3u_url = "https://drive.example.com/playlist.m3u"
        mock_client = self._make_get_client(200, _SAMPLE_M3U_NO_STREAMS)
        with patch.object(epgenius, "settings", s), \
             patch("services.epgenius.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(epgenius.verify_m3u("http://provider.example.com:8080"))
        assert result["ok"] is False
        assert result["m3u_base"] is None
