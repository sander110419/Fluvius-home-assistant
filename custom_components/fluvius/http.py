"""Session helpers for Fluvius HTTP communication."""
from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .auth import USER_AGENT

_DEFAULT_HEADERS = {
	"User-Agent": USER_AGENT,
	"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
	"Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
}


def async_create_fluvius_session(hass: HomeAssistant) -> aiohttp.ClientSession:
	"""Return a ClientSession configured for the Fluvius endpoints."""

	return aiohttp_client.async_create_clientsession(
		hass,
		headers=_DEFAULT_HEADERS,
		cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
	)