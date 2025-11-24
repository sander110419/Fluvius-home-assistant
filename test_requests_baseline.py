import asyncio
import logging

import aiohttp

from custom_components.fluvius.api import FluviusApiClient, FluviusApiError
from custom_components.fluvius.auth import USER_AGENT

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    username = "sander.hilven@gmail.com"
    password = "Llama1llama!"
    ean = "541448820044159229"
    meter_serial = "1SAG1100042062"

    print(f"Testing aiohttp baseline with user: {username}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    cookie_jar = aiohttp.CookieJar(unsafe=True, quote_cookie=False)
    async with aiohttp.ClientSession(headers=headers, cookie_jar=cookie_jar) as session:
        client = FluviusApiClient(
            session=session,
            email=username,
            password=password,
            ean=ean,
            meter_serial=meter_serial,
        )
        try:
            print("Attempting to fetch daily summaries...")
            summaries = await client.fetch_daily_summaries()
            print(f"Success! Retrieved {len(summaries)} daily summaries.")
            for summary in summaries:
                print(f"Day: {summary.day_id}, Consumption: {summary.metrics['consumption_total']} kWh")
        except FluviusApiError as exc:
            print(f"FluviusApiError occurred: {exc}")
        except Exception as exc:  # pragma: no cover - manual script
            print(f"An unexpected error occurred: {exc}")
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
