"""Token reuse, refresh and failure classification."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.fluvius.api import (
    FluviusApiClient,
    FluviusApiError,
    FluviusAuthenticationError,
)
from custom_components.fluvius.auth import (
    AsyncFluviusHttpAuthenticator,
    FluviusAuthError,
    FluviusInvalidCredentialsError,
)
from custom_components.fluvius.coordinator import FluviusEnergyDataUpdateCoordinator

LOGIN = "custom_components.fluvius.api.async_get_bearer_token"
REFRESH = "custom_components.fluvius.api.async_refresh_bearer_token"


def client():
    return FluviusApiClient(
        session=MagicMock(),
        email="test@example.com",
        password="test",
        ean="541448800000000000",
        meter_serial="TEST",
    )


def tokens(access, refresh=None):
    payload = {"access_token": access, "expires_in": 3600}
    if refresh:
        payload["refresh_token"] = refresh
    return access, payload


def expire(obj):
    obj._token_expires = 0.0


class Response:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                MagicMock(), (), status=self.status, message="Service Unavailable"
            )

    async def json(self):
        return self._payload

    async def text(self):
        return str(self._payload)


async def test_expired_token_is_refreshed_without_login():
    obj = client()
    login = AsyncMock(return_value=tokens("first", "refresh-1"))
    refresh = AsyncMock(return_value=tokens("second", "refresh-2"))
    with patch(LOGIN, login), patch(REFRESH, refresh):
        assert await obj._async_get_access_token() == "first"
        expire(obj)
        assert await obj._async_get_access_token() == "second"
        expire(obj)
        assert await obj._async_get_access_token() == "second"
    login.assert_awaited_once()
    assert [call.args[1] for call in refresh.await_args_list] == ["refresh-1", "refresh-2"]


async def test_rejected_refresh_token_falls_back_to_login():
    obj = client()
    login = AsyncMock(side_effect=[tokens("first", "refresh-1"), tokens("second", "refresh-2")])
    refresh = AsyncMock(side_effect=FluviusAuthError("invalid_grant"))
    with patch(LOGIN, login), patch(REFRESH, refresh):
        await obj._async_get_access_token()
        expire(obj)
        assert await obj._async_get_access_token() == "second"
    assert login.await_count == 2
    assert obj._refresh_token == "refresh-2"


async def test_unreachable_refresh_keeps_refresh_token():
    obj = client()
    with (
        patch(LOGIN, AsyncMock(return_value=tokens("first", "refresh-1"))),
        patch(REFRESH, AsyncMock(side_effect=TimeoutError)),
    ):
        await obj._async_get_access_token()
        expire(obj)
        with pytest.raises(FluviusApiError) as err:
            await obj._async_get_access_token()
    assert not isinstance(err.value, FluviusAuthenticationError)
    assert obj._refresh_token == "refresh-1"


async def test_refresh_server_error_keeps_refresh_token():
    session = MagicMock()
    session.get = MagicMock(return_value=Response(200, {"clientId": "client"}))
    session.post = MagicMock(return_value=Response(503))
    obj = client()
    obj._session = session
    obj._refresh_token = "refresh-1"
    login = AsyncMock()
    with patch(LOGIN, login):
        with pytest.raises(FluviusApiError, match="HTTP 503") as err:
            await obj._async_get_access_token()
    assert not isinstance(err.value, FluviusAuthenticationError)
    login.assert_not_awaited()
    assert obj._refresh_token == "refresh-1"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TimeoutError(), "TimeoutError"),
        (aiohttp.ClientConnectionError("connection reset"), "connection reset"),
        (aiohttp.ClientResponseError(MagicMock(), (), status=502, message="Bad Gateway"), "502"),
    ],
)
async def test_unreachable_login_is_retryable(failure, reason):
    obj = client()
    with patch(LOGIN, AsyncMock(side_effect=failure)):
        with pytest.raises(FluviusApiError, match=reason) as err:
            await obj._async_get_access_token()
    assert not isinstance(err.value, FluviusAuthenticationError)
    assert "test@example.com" not in str(err.value)


async def test_broken_login_flow_is_not_an_auth_failure():
    obj = client()
    failure = FluviusAuthError("Unable to locate `SETTINGS` payload inside the B2C HTML page.")
    with patch(LOGIN, AsyncMock(side_effect=failure)):
        with pytest.raises(FluviusApiError) as err:
            await obj._async_get_access_token()
    assert not isinstance(err.value, FluviusAuthenticationError)
    assert "SETTINGS" in str(err.value)


async def test_rejected_credentials_are_an_auth_failure():
    obj = client()
    failure = FluviusInvalidCredentialsError("Credentials rejected (AADB2C90053)")
    with patch(LOGIN, AsyncMock(side_effect=failure)):
        with pytest.raises(FluviusAuthenticationError, match="AADB2C90053"):
            await obj._async_get_access_token()


async def test_repeated_401_is_retryable_and_forces_next_login():
    obj = client()
    obj._session.get = MagicMock(return_value=Response(401))
    obj._access_token = "stale"
    obj._refresh_token = "refresh-1"
    with patch(REFRESH, AsyncMock(return_value=tokens("fresh", "refresh-2"))):
        with pytest.raises(FluviusApiError) as err:
            await obj._fetch_raw_consumption("stale")
    assert not isinstance(err.value, FluviusAuthenticationError)
    assert obj._session.get.call_count == 2
    assert obj._refresh_token is None

    login = AsyncMock(return_value=tokens("next", "refresh-3"))
    refresh = AsyncMock()
    with patch(LOGIN, login), patch(REFRESH, refresh):
        assert await obj._async_get_access_token() == "next"
    login.assert_awaited_once()
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (FluviusAuthError("Authorization pipeline did not redirect to redirect_uri"), UpdateFailed),
        (
            FluviusInvalidCredentialsError("Credentials rejected (AADB2C90053)"),
            ConfigEntryAuthFailed,
        ),
    ],
)
async def test_coordinator_reauths_only_on_rejected_credentials(hass, failure, expected):
    obj = client()
    coordinator = FluviusEnergyDataUpdateCoordinator(hass, obj, MagicMock())
    with patch(LOGIN, AsyncMock(side_effect=failure)):
        with pytest.raises(expected) as err:
            await coordinator._async_update_data()
    assert str(failure) in str(err.value)


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (
            {"status": "400", "errorCode": "AADB2C90053", "message": "ongeldig"},
            FluviusInvalidCredentialsError,
        ),
        ({"status": "500", "message": "busy"}, FluviusAuthError),
    ],
)
async def test_credential_submission_classification(payload, error):
    session = MagicMock()
    session.post = MagicMock(return_value=Response(200, payload))
    authenticator = AsyncFluviusHttpAuthenticator(session)
    with pytest.raises(error) as err:
        await authenticator._submit_credentials(
            "https://login.fluvius.be/tenant", "email", "password", "a", "b", "csrf", "tx", "p"
        )
    assert isinstance(err.value, FluviusInvalidCredentialsError) == (
        error is FluviusInvalidCredentialsError
    )


async def test_refresh_posts_refresh_grant():
    session = MagicMock()
    session.get = MagicMock(return_value=Response(200, {"clientId": "client"}))
    session.post = MagicMock(return_value=Response(200, {"access_token": "token"}))
    authenticator = AsyncFluviusHttpAuthenticator(session)
    assert await authenticator.refresh("refresh-1") == {"access_token": "token"}
    url = session.post.call_args.args[0]
    data = session.post.call_args.kwargs["data"]
    assert url.endswith("/oauth2/v2.0/token")
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "refresh-1"
    assert data["client_id"] == "client"
    assert "offline_access" in data["scope"].split()
