"""HTTP-only authentication helpers reused by the integration."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MSAL_CONFIG_URL = "https://mijn.fluvius.be/api/global/msal/config"
DEFAULT_AUTHORITY = "https://login.fluvius.be/klanten.onmicrosoft.com/B2C_1A_customer_signup_signin"
DEFAULT_REDIRECT_URI = "https://mijn.fluvius.be/"
DEFAULT_SCOPE = "https://klanten.onmicrosoft.com/MijnFluvius/user_impersonation"
HTML_VAR_TEMPLATE = r"var {name}\s*=\s*(\{{.*?\}});"
TIMEOUT = 30  # seconds


class FluviusAuthError(RuntimeError):
    """Raised when one of the HTTP steps fails."""


@dataclass
class PKCEPair:
    verifier: str
    challenge: str


class AsyncFluviusHttpAuthenticator:
    """Async PKCE client used by both the CLI and HA integration."""

    def __init__(self, session: aiohttp.ClientSession, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.session = session

    async def authenticate(self, username: str, password: str, remember_me: bool = False) -> Dict[str, Any]:
        metadata = await self._fetch_msal_metadata()
        authority = (metadata.get("authority") or metadata.get("auth", {}).get("authority") or DEFAULT_AUTHORITY).rstrip("/")
        client_id = metadata.get("clientId") or metadata.get("auth", {}).get("clientId")
        if not client_id:
            raise FluviusAuthError("MSAL config does not expose a clientId")
        redirect_uri = metadata.get("redirectUri") or metadata.get("auth", {}).get("redirectUri") or DEFAULT_REDIRECT_URI
        scopes = _normalise_scopes(metadata)

        pkce = _generate_pkce_pair()
        state = _random_urlsafe(32)
        nonce = _random_urlsafe(32)
        authorize_url = self._build_authorize_url(authority, client_id, redirect_uri, scopes, pkce.challenge, state, nonce, username)

        self._log("Fetching B2C authorize page...")
        async with self.session.get(authorize_url, timeout=TIMEOUT) as auth_resp:
            auth_resp.raise_for_status()
            text = await auth_resp.text()
            current_url = str(auth_resp.url)

        settings = _extract_json_variable("SETTINGS", text)
        sa_fields = _extract_json_variable("SA_FIELDS", text)
        login_field, password_field = self._resolve_attribute_fields(sa_fields)

        csrf_token = settings.get("csrf")
        trans_id = settings.get("transId")
        policy = settings.get("hosts", {}).get("policy")
        tenant_path = settings.get("hosts", {}).get("tenant")
        if not all([csrf_token, trans_id, policy, tenant_path]):
            raise FluviusAuthError("B2C settings payload is missing required keys.")

        tenant_base = self._build_tenant_base(current_url, tenant_path)
        self._log("Submitting credentials to SelfAsserted endpoint...")
        await self._submit_credentials(
            tenant_base,
            login_field,
            password_field,
            username,
            password,
            csrf_token,
            trans_id,
            policy,
        )

        self._log("Finalising session at CombinedSigninAndSignup/confirmed...")
        confirm_url = self._build_confirm_url(tenant_base, settings.get("api", "CombinedSigninAndSignup"), csrf_token, trans_id, policy, remember_me)
        code, redirect_seen = await self._follow_redirects_for_code(confirm_url, state, current_url)
        self._log("Exchanging authorization code for tokens...")
        token_response = await self._exchange_code_for_tokens(authority, client_id, redirect_uri or redirect_seen, scopes, pkce.verifier, code)
        return token_response

    # -- helpers ---------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            LOGGER.info(message)

    async def _fetch_msal_metadata(self) -> Dict[str, Any]:
        async with self.session.get(MSAL_CONFIG_URL, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()

    @staticmethod
    def _build_authorize_url(
        authority: str,
        client_id: str,
        redirect_uri: str,
        scopes: str,
        code_challenge: str,
        state: str,
        nonce: str,
        login_hint: Optional[str],
    ) -> str:
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "response_mode": "query",
            "scope": scopes,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "nonce": nonce,
            "prompt": "login",
            "client_info": "1",
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{authority}/oauth2/v2.0/authorize?{urlencode(params)}"

    @staticmethod
    def _resolve_attribute_fields(sa_fields: Dict[str, Any]) -> Tuple[str, str]:
        attributes = sa_fields.get("AttributeFields", [])
        if not attributes:
            raise FluviusAuthError("SA_FIELDS.AttributeFields is empty")

        login_field = attributes[0].get("ID")
        pwd_field = None
        for candidate in attributes:
            if candidate.get("IS_PASSWORD"):
                pwd_field = candidate.get("ID")
                break

        if not login_field or not pwd_field:
            raise FluviusAuthError("Unable to detect login/password field identifiers")

        return login_field, pwd_field

    @staticmethod
    def _build_tenant_base(current_url: str, tenant_path: str) -> str:
        parsed = urlparse(current_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if tenant_path.startswith("http"):
            return tenant_path.rstrip("/")
        return urljoin(origin + "/", tenant_path.lstrip("/")).rstrip("/")

    async def _submit_credentials(
        self,
        tenant_base: str,
        login_field: str,
        password_field: str,
        username: str,
        password: str,
        csrf_token: str,
        trans_id: str,
        policy: str,
    ) -> None:
        submit_url = f"{tenant_base}/SelfAsserted"
        params = {"tx": trans_id, "p": policy}
        payload = {
            "request_type": "RESPONSE",
            login_field: username,
        }
        payload[password_field] = password
        tenant_origin = self._extract_origin(tenant_base)
        headers = {
            "X-CSRF-TOKEN": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Origin": tenant_origin,
            "Referer": tenant_base,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        payload_encoded = urlencode(payload)
        async with self.session.post(
            submit_url,
            params=params,
            data=payload_encoded,
            headers=headers,
            timeout=TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError as exc:  # pragma: no cover - defensive log
                    raise FluviusAuthError(f"Credential submission returned non-JSON: {text[:200]}") from exc
        status_value = data.get("status")
        if str(status_value) not in {"200", "success"}:
            raise FluviusAuthError(f"Credential submission failed: {data}")

    def _build_confirm_url(
        self,
        tenant_base: str,
        combined_api: str,
        csrf_token: str,
        trans_id: str,
        policy: str,
        remember_me: bool,
    ) -> str:
        api_base = f"{tenant_base}/api/{combined_api.strip('/')}"
        remember_value = "true" if remember_me else "false"
        base_path = f"confirmed?rememberMe={remember_value}"
        separator = "&" if "?" in base_path else "?"
        path_with_tokens = f"{base_path}{separator}{urlencode({'csrf_token': csrf_token, 'tx': trans_id})}"
        return f"{api_base}/{path_with_tokens}&{urlencode({'p': policy})}"

    async def _follow_redirects_for_code(self, start_url: str, expected_state: str, origin_url: str) -> Tuple[str, str]:
        origin = self._extract_origin(origin_url)
        next_url = start_url
        for _ in range(6):
            async with self.session.get(next_url, allow_redirects=False, timeout=TIMEOUT) as resp:
                if resp.status not in (301, 302, 303, 307, 308):
                    raise FluviusAuthError("Authorization pipeline did not redirect to redirect_uri")
                location = resp.headers.get("Location")
            if not location:
                raise FluviusAuthError("Redirect response missing Location header")
            absolute = urljoin(origin + "/", location)
            parsed = urlparse(absolute)
            query = parse_qs(parsed.query)
            if "code" in query:
                state = query.get("state", [None])[0]
                if state and state != expected_state:
                    raise FluviusAuthError("State returned by B2C does not match request state")
                return query["code"][0], f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            next_url = absolute
        raise FluviusAuthError("Failed to capture authorization code after multiple redirects")

    async def _exchange_code_for_tokens(
        self,
        authority: str,
        client_id: str,
        redirect_uri: str,
        scopes: str,
        code_verifier: str,
        code: str,
    ) -> Dict[str, Any]:
        token_url = f"{authority}/oauth2/v2.0/token"
        data = {
            "client_id": client_id,
            "scope": scopes,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }
        async with self.session.post(token_url, data=data, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise FluviusAuthError(f"Token endpoint error ({resp.status}): {text}")
            return await resp.json()

    @staticmethod
    def _extract_origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"


def _random_urlsafe(length: int = 40) -> str:
    raw = secrets.token_urlsafe(length)
    return raw[:length]


def _generate_pkce_pair() -> PKCEPair:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return PKCEPair(verifier=verifier, challenge=challenge)


def _extract_json_variable(name: str, html: str) -> Dict[str, Any]:
    pattern = re.compile(HTML_VAR_TEMPLATE.format(name=name), re.DOTALL)
    match = pattern.search(html)
    if not match:
        raise FluviusAuthError(f"Unable to locate `{name}` payload inside the B2C HTML page.")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise FluviusAuthError(f"Failed to parse `{name}` JSON payload: {exc}") from exc


def _normalise_scopes(metadata: Dict[str, Any]) -> str:
    candidates: List[Iterable[str]] = []
    raw_candidates: List[Any] = [
        metadata.get("scopes"),
        metadata.get("defaultScopes"),
        metadata.get("apiScopes"),
        metadata.get("authRequest", {}).get("scopes"),
        metadata.get("protectedResourceMap"),
    ]

    for candidate in raw_candidates:
        if not candidate:
            continue
        if isinstance(candidate, dict):
            for value in candidate.values():
                if isinstance(value, (list, tuple, set)):
                    candidates.append(value)
                elif isinstance(value, str):
                    candidates.append(value.split())
        elif isinstance(candidate, (list, tuple, set)):
            candidates.append(candidate)
        elif isinstance(candidate, str):
            candidates.append(candidate.split())

    flat: List[str] = []
    for chunk in candidates:
        for scope in chunk:
            if scope not in flat:
                flat.append(scope)

    for required in ("openid", "offline_access", DEFAULT_SCOPE):
        if required not in flat:
            flat.append(required)

    return " ".join(flat)


async def async_get_bearer_token(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    remember_me: bool = False,
    verbose: bool = False,
) -> tuple[str, Dict[str, Any]]:
    authenticator = AsyncFluviusHttpAuthenticator(session, verbose=verbose)
    token_response = await authenticator.authenticate(email, password, remember_me=remember_me)
    access_token = token_response.get("access_token")
    if not access_token:
        raise FluviusAuthError("Token response does not contain an access_token")
    return access_token, token_response
