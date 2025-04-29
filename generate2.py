import os
import sys
import json
import hashlib
import asyncio
import aiohttp
import aiofiles
import re
from tqdm.asyncio import tqdm
from dotenv import load_dotenv
from datetime import datetime

# Constants
GITHUB_API = "https://api.github.com"
HEADERS = lambda token: {
    "Authorization": f"token {token}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "carbonrepo-generator"
}

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# --- Utilities ---

async def fetch_json(session, url, **kwargs):
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"HTTP {resp.status} for {url}: {text}")
        return await resp.json()

async def fetch_content(session, url, **kwargs):
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"Download failed {resp.status} {url}: {text}")
        return await resp.read()

async def fetch_stream_hash(session, url, **kwargs):
    sha256 = hashlib.sha256()
    
    # Add cache control headers to get the most up-to-date version
    headers = kwargs.get('headers', {})
    headers['Cache-Control'] = 'no-cache, no-store'
    headers['Pragma'] = 'no-cache'
    
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise Exception(f"Download failed {resp.status} {url}: {text}")
            
        async for chunk in resp.content.iter_chunked(1024):
            sha256.update(chunk)
    
    return sha256.hexdigest()

async def fetch_html(session, url, **kwargs):
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            return None
        return await resp.text()

def get_social_preview_url(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("meta", property="og:image")
    return tag.get("content") if tag else None

def format_repo_name(name):
    # First, remove `SM-` prefix if it exists
    name = re.sub(r"^SM-", "", name)
    name = re.sub(r"^SM", "", name)
    
    # Then, replace either dashes or underscores with spaces
    name = re.sub(r"[-_]", " ", name)
    
    # Add a space for every capital letter that is not followed by another capital letter
    name = re.sub(r"(?<!^)(?=[A-Z])", " ", name)
    
    # Finally, capitalize the first letter of each word
    name = name.title()
    
    # Replace "A P I" with "API"
    name = re.sub(r"\bA P I\b", "API", name)
    
    # Replace double spaces with single spaces
    name = re.sub(r"\s+", " ", name).strip()
    
    return name

# --- Repo Processing ---

async def get_repo_data(session, repo_full_name):
    url = f"{GITHUB_API}/repos/{repo_full_name}"
    return await fetch_json(session, url, headers=HEADERS(GITHUB_TOKEN))

async def get_releases(session, repo_full_name):
    # First try to get the release marked as "latest" by the repo owner
    latest_url = f"{GITHUB_API}/repos/{repo_full_name}/releases/latest"
    try:
        latest_release = await fetch_json(session, latest_url, headers=HEADERS(GITHUB_TOKEN))
        # Return the latest release as the first item in an array for consistency with process_repo
        return [latest_release]
    except Exception as e:
        print(f"[WARNING] No 'latest' release found for {repo_full_name}, falling back to all releases: {e}")
        # Fall back to getting all releases if "latest" is not available
        all_releases_url = f"{GITHUB_API}/repos/{repo_full_name}/releases"
        return await fetch_json(session, all_releases_url, headers=HEADERS(GITHUB_TOKEN))

async def get_contributors(session, repo_full_name, max_count=30):
    url = f"{GITHUB_API}/repos/{repo_full_name}/contributors?per_page={max_count}"
    users = await fetch_json(session, url, headers=HEADERS(GITHUB_TOKEN))
    return [user['login'] for user in users if user.get('type') == "User"]

async def get_social_preview(session, repo_full_name):
    html = await fetch_html(session, f"https://github.com/{repo_full_name}")
    if not html:
        return None
    return get_social_preview_url(html)

async def download_asset_and_hash(session, asset, expected_hash=None):
    try:
        h = await fetch_stream_hash(session, asset["browser_download_url"], headers=HEADERS(GITHUB_TOKEN))
        return {
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "currentHash": h,
            "validHash": expected_hash,
            "hashMatch": expected_hash == h if expected_hash else None
        }
    except Exception as e:
        return {
            "name": asset["name"],
            "url": asset["browser_download_url"],
            "currentHash": None,
            "validHash": expected_hash,
            "hashMatch": False,
            "error": str(e)
        }

async def process_repo(session, mod, sema=None):
    repo_name = list(mod.keys())[0]
    repo_data_spec = mod[repo_name]
    error = False
    try:
        if sema: await sema.acquire()
        repo_json = await get_repo_data(session, repo_name)
        releases = await get_releases(session, repo_name)

        downloads = []
        total_downloads = 0

        if releases:
            latest = releases[0]
            assets = latest.get("assets", [])
            # Download assets concurrently with their expected hashes
            asset_hashes = await asyncio.gather(*[
                download_asset_and_hash(
                    session, 
                    asset, 
                    repo_data_spec.get("assets", {}).get(asset["name"])
                ) for asset in assets
            ])
            downloads.extend(asset_hashes)
            total_downloads += sum(asset.get("download_count", 0) for asset in assets)

            # Count download counts from other releases (no asset download, just tally)
            for rel in releases[1:]:
                for asset in rel.get("assets", []):
                    total_downloads += asset.get("download_count", 0)

        # Fetch contributors and social preview concurrently
        contrib_task = asyncio.create_task(get_contributors(session, repo_name))
        social_task = asyncio.create_task(get_social_preview(session, repo_name))
        contributors, icon = await asyncio.gather(contrib_task, social_task)

        # Validate asset hashes
        mismatched_hashes = []
        for download in downloads:
            expected_hash = repo_data_spec.get("assets", {}).get(download["name"])
            if expected_hash:
                download["validHash"] = expected_hash
                if download["currentHash"] != expected_hash:
                    print(f"\n[❌ HASH MISMATCH] {repo_name}/{download['name']}")
                    print(f"    Expected: {expected_hash}")
                    print(f"    Actual:   {download['currentHash']}")
                    mismatched_hashes.append(download["name"])
                    error = True

        # Get the raw name and format it
        raw_name = repo_json.get("name", repo_name.split("/")[-1])
        formatted_name = format_repo_name(raw_name)

        result = {
            "name": formatted_name,
            "full_name": repo_json.get("full_name", repo_name),
            "url": repo_json.get("html_url"),
            "description": repo_json.get("description") or "No description available.",
            "downloads": downloads,
            "total_downloads": total_downloads,
            "stars": repo_json.get("stargazers_count", 0),
            "contributors": contributors,
            "icon": icon,
            "mismatched_hashes": mismatched_hashes,
        }
        return result, error
    except Exception as e:
        print(f"\n[⚠️  ERROR] Failed to process {repo_name}: {e}")
        return None, True
    finally:
        if sema: sema.release()

async def main():
    print("\n📦 Starting mod processing...\n")
    start_time = datetime.now()
    async with aiofiles.open("config.json", "r") as c:
        config = json.loads(await c.read())

    output_file = "repos-gen2.json"
    jsons = []
    error_found = False
    processed_count = 0
    mismatch_count = 0
    details = []

    connector = aiohttp.TCPConnector(limit=16)
    sema = asyncio.Semaphore(8)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_repo(session, mod, sema) for mod in config]
        for coro in tqdm.as_completed(tasks, total=len(tasks), desc="🔍 Processing mods"):
            repo_json, error = await coro
            if repo_json:
                jsons.append(repo_json)
                processed_count += 1
                if repo_json.get("mismatched_hashes"):
                    mismatch_count += len(repo_json["mismatched_hashes"])
                    details.append(
                        f"  - {repo_json['full_name']} mismatched: {', '.join(repo_json['mismatched_hashes'])}"
                    )
            if error:
                error_found = True

    print(f"\n📝 Writing output to {output_file}...\n")
    async with aiofiles.open(output_file, "w") as f:
        await f.write(json.dumps(jsons, indent=4))

    elapsed = datetime.now() - start_time
    print("\n====== Mod Processing Complete ======")
    print(f"🗂  Total mods processed: {processed_count}")
    print(f"⭐  Total stars (sum): {sum(r['stars'] for r in jsons)}")
    print(f"⬇️  Total downloads (sum): {sum(r['total_downloads'] for r in jsons)}")
    print(f"❓  Total hash mismatches: {mismatch_count}")
    if details:
        print("    Details:")
        print("\n".join(details))
    print(f"⏱  Time elapsed: {elapsed}\n")
    print(f"📝 Output written to: {output_file}\n")

    if error_found:
        print("❗ One or more issues were encountered. Exiting with code 1.\n")
        sys.exit(1)
    else:
        print("✅ All mods processed successfully!\n")

if __name__ == "__main__":
    asyncio.run(main())