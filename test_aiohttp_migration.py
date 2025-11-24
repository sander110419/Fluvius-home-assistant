import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

# Configure logging
logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

# Constants from auth.py
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
MSAL_CONFIG_URL = "https://mijn.fluvius.be/api/global/msal/config"
DEFAULT_AUTHORITY = "https://login.fluvius.be/klanten.onmicrosoft.com/B2C_1A_customer_signup_signin"
DEFAULT_REDIRECT_URI = "https://mijn.fluvius.be/"
DEFAULT_SCOPE = "https://klanten.onmicrosoft.com/MijnFluvius/user_impersonation"
HTML_VAR_TEMPLATE = r"var {name}\s*=\s*(\{{.*?\}});"
TIMEOUT = 30

# Constants from api.py
CONF_DAYS_BACK = "days_back"
CONF_GRANULARITY = "granularity"
CONF_TIMEZONE = "timezone"
DEFAULT_DAYS_BACK = 7
DEFAULT_GRANULARITY = "4"
DEFAULT_METER_TYPE = "electricity"
DEFAULT_TIMEZONE = "UTC"
GAS_MIN_LOOKBACK_DAYS = 7
GAS_SUPPORTED_GRANULARITY = "4"
METER_TYPE_GAS = "gas"
ALL_METRICS = [
    "consumption_high",
    "consumption_low",
    "injection_high",
    "injection_low",
    "consumption_total",
    "injection_total",
    "net_consumption",
]
CUBIC_METER_UNIT_CODE = 5

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


class FluviusAuthError(RuntimeError):
    """Raised when one of the HTTP steps fails."""


class FluviusApiError(RuntimeError):
    """Raised when the Fluvius API call fails."""


@dataclass
class PKCEPair:
    verifier: str
    challenge: str


@dataclass(slots=True)
class FluviusDailySummary:
    """Container for a single day of energy data."""

    day_id: str
    start: datetime
    end: datetime
    metrics: Dict[str, float]


# --- Helper Functions (Pure Logic) ---

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


# --- Async Authenticator ---

class AsyncFluviusHttpAuthenticator:
    """Async version of FluviusHttpAuthenticator using aiohttp."""

    def __init__(self, session: aiohttp.ClientSession, verbose: bool = False) -> None:
        self.verbose = verbose
        self.session = session
        # Headers are usually set on the session or per request.
        # aiohttp sessions can have default headers.

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
            url = str(auth_resp.url)

        settings = _extract_json_variable("SETTINGS", text)
        sa_fields = _extract_json_variable("SA_FIELDS", text)
        login_field, password_field = self._resolve_attribute_fields(sa_fields)

        csrf_token = settings.get("csrf")
        trans_id = settings.get("transId")
        policy = settings.get("hosts", {}).get("policy")
        tenant_path = settings.get("hosts", {}).get("tenant")
        if not all([csrf_token, trans_id, policy, tenant_path]):
            raise FluviusAuthError("B2C settings payload is missing required keys.")

        tenant_base = self._build_tenant_base(url, tenant_path)
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
        code, redirect_seen = await self._follow_redirects_for_code(confirm_url, state, url)
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
        
        # Remove manual Content-Type and let aiohttp handle it
        if "Content-Type" in headers:
            del headers["Content-Type"]
        
        # Debug logging
        LOGGER.info(f"Submit URL: {submit_url}")
        LOGGER.info(f"Params: {params}")
        LOGGER.info(f"Payload keys: {list(payload.keys())}")
        LOGGER.info(f"Headers: {headers}")
        LOGGER.info(f"Cookies: {list(self.session.cookie_jar)}")

        # Manually encode and set Content-Type to match requests exactly
        payload_encoded = urlencode(payload)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        
        async with self.session.post(submit_url, params=params, data=payload_encoded, headers=headers, timeout=TIMEOUT) as resp:
            resp.raise_for_status()
            try:
                data = await resp.json()
            except aiohttp.ContentTypeError:
                # Some APIs return JSON with wrong content-type, or it might be HTML error
                text = await resp.text()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    raise FluviusAuthError(f"Credential submission returned non-JSON: {text[:200]}...")
        
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


# --- Async API Client ---

class AsyncFluviusApiClient:
    """Async version of FluviusApiClient using aiohttp."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        password: str,
        ean: str,
        meter_serial: str,
        meter_type: str = DEFAULT_METER_TYPE,
        remember_me: bool = False,
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._email = email
        self._password = password
        self._ean = ean
        self._meter_serial = meter_serial
        self._meter_type = meter_type
        self._remember_me = remember_me
        self._session = session
        self._options = options or {}

    async def fetch_daily_summaries(self) -> List[FluviusDailySummary]:
        """Retrieve the most recent consumption data and return parsed summaries."""

        payload = await self._fetch_raw_consumption()
        summaries = self._summaries_from_payload(payload)
        if not summaries:
            raise FluviusApiError("No consumption rows returned by the Fluvius API")
        return summaries

    async def _fetch_raw_consumption(self) -> List[Dict[str, Any]]:
        try:
            authenticator = AsyncFluviusHttpAuthenticator(self._session, verbose=True)
            token_response = await authenticator.authenticate(self._email, self._password, remember_me=self._remember_me)
            access_token = token_response.get("access_token")
            if not access_token:
                raise FluviusAuthError("Token response does not contain an access_token")
        except FluviusAuthError as err:
            raise FluviusApiError(f"Authentication failed: {err}") from err
        except aiohttp.ClientError as err:
            raise FluviusApiError(f"Network error while authenticating: {err}") from err

        history_params = self._build_history_range()
        granularity = str(self._options.get(CONF_GRANULARITY, DEFAULT_GRANULARITY))
        if self._meter_type == METER_TYPE_GAS:
            granularity = GAS_SUPPORTED_GRANULARITY
        params = {
            **history_params,
            "granularity": granularity,
            "asServiceProvider": "false",
            "meterSerialNumber": self._meter_serial,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (HomeAssistant-FluviusEnergy)",
        }
        url = f"https://mijn.fluvius.be/verbruik/api/meter-measurement-history/{self._ean}"

        try:
            async with self._session.get(url, params=params, headers=headers, timeout=30) as response:
                response.raise_for_status()
                data = await response.json()
        except aiohttp.ClientError as err:
            raise FluviusApiError(f"Consumption API call failed: {err}") from err
        except ValueError as err:
            raise FluviusApiError(f"Failed to decode Fluvius JSON: {err}") from err

        if not isinstance(data, list):
            raise FluviusApiError("Fluvius API returned an unexpected payload (expected list)")
        return data

    def _build_history_range(self) -> Dict[str, str]:
        tzinfo = self._resolve_timezone(self._options.get(CONF_TIMEZONE, DEFAULT_TIMEZONE))
        days_back = max(int(self._options.get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)), 1)
        if self._meter_type == METER_TYPE_GAS:
            days_back = max(days_back, GAS_MIN_LOOKBACK_DAYS)
        local_now = datetime.now(tzinfo)
        start_date = (local_now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_date = local_now.replace(hour=23, minute=59, second=59, microsecond=999000)
        return {
            "historyFrom": start_date.isoformat(timespec="milliseconds"),
            "historyUntil": end_date.isoformat(timespec="milliseconds"),
        }

    def _resolve_timezone(self, tz_name: Optional[str]):
        if tz_name and ZoneInfo is not None:
            try:
                return ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                pass
        if tz_name:
            pass
        local = datetime.now().astimezone().tzinfo
        if local:
            return local
        return timezone.utc

    def _summaries_from_payload(self, payload: List[Dict[str, Any]]) -> List[FluviusDailySummary]:
        summaries: List[FluviusDailySummary] = []
        for day_data in payload:
            summary = self._summarize_day(day_data)
            if summary:
                summaries.append(summary)
        summaries.sort(key=lambda item: item.start)
        return summaries

    def _summarize_day(self, day_data: Dict[str, Any]) -> Optional[FluviusDailySummary]:
        start = self._parse_datetime(day_data.get("d"))
        if not start:
            return None
        end = self._parse_datetime(day_data.get("de")) or (start + timedelta(days=1))
        metrics: Dict[str, float] = {metric: 0.0 for metric in ALL_METRICS}

        for reading in day_data.get("v", []) or []:
            direction = self._safe_int(reading.get("dc"))
            tariff = self._safe_int(reading.get("t"), default=1)
            unit = self._safe_int(reading.get("u"))
            value = self._safe_float(reading.get("v"))

            if unit == CUBIC_METER_UNIT_CODE:
                continue

            metric_key = self._metric_from_reading(direction, tariff)
            if not metric_key:
                continue
            metrics[metric_key] += value

        metrics["consumption_total"] = metrics["consumption_high"] + metrics["consumption_low"]
        metrics["injection_total"] = metrics["injection_high"] + metrics["injection_low"]
        metrics["net_consumption"] = metrics["consumption_total"] - metrics["injection_total"]

        day_id = start.isoformat()
        return FluviusDailySummary(day_id=day_id, start=start, end=end, metrics=metrics)

    @staticmethod
    def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        fixed = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(fixed)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    @staticmethod
    def _metric_from_reading(direction: int, tariff: int) -> Optional[str]:
        is_high_tariff = tariff == 1
        if direction in (0, 1):
            return "consumption_high" if is_high_tariff else "consumption_low"
        if direction == 2:
            return "injection_high" if is_high_tariff else "injection_low"
        return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return default


async def main():
    # Credentials provided by user
    USERNAME = "sander.hilven@gmail.com"
    PASSWORD = "Llama1llama!"
    
    # Dummy EAN and Serial for testing (these might fail if they don't exist for the user, 
    # but authentication should succeed at least)
    # Ideally we would need real EAN/Serial, but let's see if we can at least authenticate.
    # If the user has these in their config, we could use them, but I don't have access to their secrets.yaml or config entries.
    # I'll use placeholders and expect an API error after auth, or maybe the user can fill them in.
    # Wait, the user just said "test if a migration to aiohttp would work".
    # I will try to authenticate first.
    
    # I'll use the EAN/Serial from the test files if available, or just random ones.
    # In test_api.py: ean="541448800000000000", meter_serial="1SAGTEST"
    EAN = "541448820044159229"
    METER_SERIAL = "1SAG1100042062"

    print(f"Testing aiohttp migration with user: {USERNAME}")
    
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    async def on_request_start(session, trace_config_ctx, params):
        LOGGER.info(f"Sending {params.method} request to {params.url}")
        LOGGER.info(f"Headers: {params.headers}")
        # Check cookies that will be sent
        cookies = session.cookie_jar.filter_cookies(params.url)
        LOGGER.info(f"Cookies to be sent: {cookies}")

    trace_config = aiohttp.TraceConfig()
    trace_config.on_request_start.append(on_request_start)

    # Disable cookie quoting to match requests behavior (Azure B2C dislikes quoted cookies)
    cookie_jar = aiohttp.CookieJar(unsafe=True, quote_cookie=False)
    async with aiohttp.ClientSession(headers=headers, trace_configs=[trace_config], cookie_jar=cookie_jar) as session:
        client = AsyncFluviusApiClient(
            session,
            email=USERNAME,
            password=PASSWORD,
            ean=EAN,
            meter_serial=METER_SERIAL
        )
        
        try:
            print("Attempting to fetch daily summaries (this includes authentication)...")
            summaries = await client.fetch_daily_summaries()
            print(f"Success! Retrieved {len(summaries)} daily summaries.")
            for s in summaries:
                print(f"Day: {s.day_id}, Consumption: {s.metrics['consumption_total']} kWh")
        except FluviusApiError as e:
            print(f"FluviusApiError occurred: {e}")
            # If it's an API error (like invalid EAN), it means Auth succeeded!
            if "Authentication failed" not in str(e):
                print("Authentication likely succeeded, but API call failed (expected with dummy EAN).")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
