import asyncio
import httpx
import math
import pickle
import io
import netrc
import http.cookiejar

from pathlib import Path
from datetime import datetime, timezone, timedelta
from fiona.io import ZipMemoryFile
from tqdm import tqdm
from typing import List, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


EDL_HOST = "urs.earthdata.nasa.gov"
EDL_COOKIE_PATH = Path("storage/edl_cookies.txt")


class EarthdataChallengeAuth(httpx.Auth):
    requires_response_body = False

    def __init__(self, username, password):
        self._auth_header = httpx.BasicAuth(username, password)._auth_header

    def auth_flow(self, request):
        if request.url.host == EDL_HOST:
            request.headers["Authorization"] = self._auth_header

        response = yield request

        while response.is_redirect:
            next_request = response.next_request
            if next_request is None:
                break
            if next_request.url.host == EDL_HOST:
                next_request.headers["Authorization"] = self._auth_header
            else:
                next_request.headers.pop("Authorization", None)
            response = yield next_request


def load_edl_cookie_jar() -> http.cookiejar.MozillaCookieJar:
    jar = http.cookiejar.MozillaCookieJar(EDL_COOKIE_PATH)
    if EDL_COOKIE_PATH.exists():
        jar.load(ignore_discard=True, ignore_expires=False)
    return jar


def setup_earthdata_client():
    username, _, password = netrc.netrc().authenticators(EDL_HOST)
    jar = load_edl_cookie_jar()
    client = httpx.AsyncClient(
        follow_redirects=False,
        auth=EarthdataChallengeAuth(username, password),
        cookies=jar,
        headers={'User-Agent': f'podaac-subscriber-1.0.0'},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )
    return client, jar


async def get_lake_attributes_only(client, semaphore, url: str, log_tag: int, retries: int = 3) -> dict:
    async with semaphore:
        zip_buffer = io.BytesIO()
        for attempt in range(retries):
            zip_buffer.seek(0)
            zip_buffer.truncate()
            try:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        zip_buffer.write(chunk)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 503 and attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
        else:
            raise Exception(f"Could not download {url}")

        zip_bytes = zip_buffer.getvalue()

        extracted_data = {}

        with ZipMemoryFile(zip_bytes) as zip_memfile:
            with zip_memfile.open() as collection:
                for feature in collection:
                    attributes = feature['properties']
                    lake_id = attributes.get('lake_id')
                    lake_name = attributes.get('lake_name')

                    if lake_id:
                        extracted_data.setdefault(lake_id, []).append(lake_name)
        return extracted_data


async def ensure_cookie(client, jar, sample_url: str) -> None:
    if any("earthdata.nasa.gov" in c.domain for c in jar):
        print("Reusing cached EDL cookie")
        return

    print("Warming up EDL session...")
    response = await client.head(sample_url)
    if response.status_code in (401, 403):
        raise RuntimeError(f"EDL auth failed (status {response.status_code})")
    jar.save(ignore_discard=True)
    print("EDL session ready")


async def setup_download_tasks(file_id: str, urls: set, desc: str = "Downloading") -> None:
    semaphore = asyncio.Semaphore(5)
    client, jar = setup_earthdata_client()
    url_list = list(urls)
    tasks: List[asyncio.Task] = []

    try:
        async with client:
            await ensure_cookie(client, jar, url_list[0])
            tasks = [
                asyncio.create_task(
                    get_lake_attributes_only(
                        client=client,
                        semaphore=semaphore,
                        url=url,
                        log_tag=i+1,
                    )
                ) for i, url in enumerate(url_list)
            ]
            with tqdm(total=len(tasks), desc=desc, unit="file") as pbar:
                def make_callback(url):
                    fname = url.rsplit('/', 1)[-1]
                    def _cb(task):
                        pbar.update(1)
                        if task.cancelled():
                            return
                        if (exc := task.exception()) is not None:
                            tqdm.write(f"  ✗ {fname}: {exc!r}")
                    return _cb

                for url, t in zip(url_list, tasks):
                    t.add_done_callback(make_callback(url))
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        pending = [t for t in tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        successes, failures = [], []
        for url, t in zip(url_list, tasks):
            if t.cancelled():
                failures.append((url, 'cancelled'))
            elif (exc := t.exception()) is not None:
                failures.append((url, repr(exc)))
            else:
                successes.append(t.result())

        if successes:
            save(file_id=file_id, data=successes, prefix='dt')
            print(f'{len(successes)} results saved to storage/dt_{file_id} 🔥')
        if failures:
            save(file_id=file_id, data=failures, prefix='failed')
            print(f'{len(failures)} URLs failed — saved to storage/failed_{file_id}')

        jar.save(ignore_discard=True)


async def cmr_search(client, semaphore, page_size, page_num, start_date: str='', end_date: str=''):
    url = (f"https://cmr.earthdata.nasa.gov/search/granules.umm_json?page_size={page_size}&page_num={page_num}&sort_key=-start_date&"
           f"provider=POCLOUD&ShortName=SWOT_L2_HR_LakeSP_D&temporal={start_date},{end_date}")

    async with semaphore:
        print(f'Requesting page_size: {page_size}, page_num: {page_num}...')
        response = await client.get(url)
        response.raise_for_status()
        body = response.json()

    items = body['items']
    urls = set()

    for item in items:
        if 'umm' in item and (related_urls := item['umm'].get('RelatedUrls')):
            for subitem in related_urls:
                if subitem['Type'] == 'GET DATA':
                    urls.add(subitem['URL'])
                    break
    print(f'Fetched page_size: {page_size}, page_num: {page_num} ✅')
    return body['hits'], urls


async def gather_urls(start_date: str, end_date: str) -> set:
    page_size = 100
    results = set()
    semaphore = asyncio.Semaphore(10)   # 10 concurrent connection to NASA's API

    async with httpx.AsyncClient() as client:
        # first request to get the total number of hits
        hits, urls = await cmr_search(
            client=client,
            semaphore=semaphore,
            page_size=page_size,
            page_num=1,
            start_date=start_date,
            end_date=end_date
        )
        results.update(urls)

        if hits > page_size:
            total_pages = math.ceil(hits / page_size)
            tasks = [
                asyncio.create_task(
                    cmr_search(
                        client=client,
                        semaphore=semaphore,
                        page_size=page_size,
                        page_num=i,
                        start_date=start_date,
                        end_date=end_date
                    )
                )
                for i in range(2, total_pages+1)
            ]

            gathered_data = await asyncio.gather(*tasks)
            for _, page_urls in gathered_data:
                results.update(page_urls)
    return results


def make_fid(start_time, end_time):
    pattern = "%Y-%m-%dT%H:%M:%SZ"

    if start_time:
        sdt = datetime.strptime(
            start_time, pattern
        ).replace(tzinfo=timezone.utc).timestamp()
    else:
        sdt = .0

    if end_time:
        edt = datetime.strptime(
            end_time, pattern
        ).replace(tzinfo=timezone.utc).timestamp()
    else:
        edt = .0

    return f'{sdt.hex()}_{edt.hex()}'


def fetch(file_id: str) -> Optional[set]:
    lookup = Path(f'storage/{file_id}')
    data = None
    if lookup.exists():
        with open(lookup, 'rb') as f:
            data = pickle.load(f)
    return data


def save(file_id: str, data: set, prefix:str) -> None:
    with open(f'storage/{prefix}_{file_id}', 'wb') as f:
        pickle.dump(data, f)


def add_query_param(url, key, value) -> str:
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query[key] = [value]
    new_query = urlencode(query, doseq=True)
    return str(
        urlunparse(parts._replace(query=new_query))
    )


DATE_PATTERN = "%Y-%m-%dT%H:%M:%SZ"


async def process_window(start_date: str, end_date: str) -> None:
    file_id = make_fid(start_date, end_date)

    if Path(f'storage/dt_{file_id}').exists():
        print(f'Already processed {start_date} → {end_date}, skipping')
        return

    if urls := fetch(f'fl_{file_id}'):
        print(f'Found {len(urls)} URLs in storage')
    else:
        urls = await gather_urls(start_date, end_date)
        if not urls:
            print(f'No granules found for {start_date} → {end_date}')
            return
        print(f'Saving {len(urls)} URLs to storage')
        save(file_id=file_id, data=urls, prefix='fl')

    await setup_download_tasks(
        file_id=file_id,
        urls=set(urls),
        desc=f'{start_date[:10]} ({len(urls)} files)',
    )


async def main():
    overall_start = '2026-04-03T00:00:00Z'
    overall_end = '2026-05-24T00:00:00Z'

    current = datetime.strptime(overall_start, DATE_PATTERN).replace(tzinfo=timezone.utc)
    end = datetime.strptime(overall_end, DATE_PATTERN).replace(tzinfo=timezone.utc)

    while current < end:
        next_day = current + timedelta(days=1)
        day_start = current.strftime(DATE_PATTERN)
        day_end = next_day.strftime(DATE_PATTERN)

        print(f'\n=== {day_start} → {day_end} ===')
        await process_window(day_start, day_end)

        current = next_day


if __name__ == '__main__':
    asyncio.run(main())