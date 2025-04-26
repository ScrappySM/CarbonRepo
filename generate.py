import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
if GITHUB_TOKEN:
    if GITHUB_TOKEN.startswith("github_pat_"):
        HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}"}
    else:
        HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}
else:
    HEADERS = {}

urlList = [
    "VeraDev0/SM-NoAutoSmartPhysics",
	"ScrappySM/Network-Checksum-Disabler",
	"ScrappySM/DevCheckBypass",
	"ScrappySM/SM-ForceDevMode",
	"ScrappySM/SM-KeyAPI",
	"ScrappySM/SM-LuaConsole",
	"ScrappySM/SM-LoadString",
	"ReDoIngMods/SM-NoFileExistError",
    "StoodJarguar657/SMGuiRefresh"
]

manualMods = [
    {
		"name": "Networking Fix",
		"description": "Stops Scrap Mechanic client from stalling packets. Fixes pretty much all the networking issues.",
		"authors": [
			"QuestionableM",
			"ColdMeekly"
		],
		"ghUser": "Scrap-Mods",
		"ghRepo": "Networking-Fix"
	},
	{
		"name": "Proximity Voice Chat",
		"description": "A Scrap Mechanic DLL mod which adds the Proximity Voice Chat into the game",
		"authors": [
			"QuestionableM"
		],
		"ghUser": "QuestionableM",
		"ghRepo": "SM-ProximityVoiceChat"
	},
	{
		"name": "Better Paint Tool",
		"description": "A DLL mod for Scrap Mechanic which enhances the functionality of the vanilla Paint Tool and allows you to pick any color you want!",
		"authors": [
			"QuestionableM"
		],
		"ghUser": "QuestionableM",
		"ghRepo": "SM-BetterPaintTool"
	},
	{
		"name": "Dynamic Sun",
		"description": "A Scrap Mechanic DLL mod which makes the sun dynamic by letting you adjust the angle!",
		"authors": [
			"QuestionableM"
		],
		"ghUser": "QuestionableM",
		"ghRepo": "SM-DynamicSun"
	},
	{
		"name": "Custom audio extensions",
		"description": "A mod to add custom audio support for Scrap Mechanic workshop mods",
		"authors": [
			"QuestionableM"
		],
		"ghUser": "QuestionableM",
		"ghRepo": "SM-CustomAudioExtension"
	}
]

class Manifest():
    def __init__(self, name, url, authors, description):
        self.name = name
        self.url = url
        self.authors = authors
        self.description = description

    def __repr__(self):
        return f"Manifest (name={self.name}, url={self.url}, authors={self.authors}, description={self.description})"
    
class DownloadData():
    # TODO: hashes
    def __init__(self, urls, names):
        self.urls = urls
        self.names = names

    def __repr__(self):
        return f"DownloadData (urls={self.urls}, names={self.names})"
    
class Mod():
    def __init__(self, url=None):
        self.url = url

        if url is not None:
            self.owner = url.split("/")[0]
            self.repo = url.split("/")[1]

        self.default_branch = None
        self.manifest = None
        self.downloaddata = None

        self.manuallyPopulated = False

    def populate_details_manually(self, name, description, authors):
        self.manifest = Manifest(
            name,
            self.url,
            authors,
            description
        )

        self.manuallyPopulated = True

    def _get_default_branch(self):
        repo_data_url = f"https://api.github.com/repos/{self.owner}/{self.repo}"

        try:
            response = requests.get(repo_data_url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()
            return data.get("default_branch", "main")
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"Failed to get default branch: {e}")
            return "main"

    def _populate_manifest(self):
        url = f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.default_branch}/manifest.json"
        try:
            response = requests.get(url, headers=HEADERS)
            response.raise_for_status()

            data = response.json()
            self.manifest = Manifest(
                name=data.get("name"),
                url=data.get("url"),
                authors=data.get("authors"),
                description=data.get("description")
            )
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"Failed to get manifest: {e}")
            return None

    def _populate_downloaddata(self):
        latest_url = f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/latest"
        urls = []
        names = []

        try:
            response = requests.get(latest_url, headers=HEADERS)
            response.raise_for_status()
            data = response.json()

            for asset in data.get("assets", []):
                download_url = asset["browser_download_url"]
                urls.append(download_url)
                names.append(asset["name"])
                break

            self.downloaddata = DownloadData(urls, names)
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"Failed to get download data: {e}")
            return None

    def populate(self):
        self.default_branch = self._get_default_branch()
        if not self.manuallyPopulated:
            self._populate_manifest()
        self._populate_downloaddata()

    def __repr__(self):
        return f"Mod ({self.owner}/{self.repo}, default_branch={self.default_branch}, manifest={self.manifest}, downloaddata={self.downloaddata})"

mods = [Mod(url) for url in urlList]

for manualMod in manualMods:
    url = f"https://github.com/{manualMod['ghUser']}/{manualMod['ghRepo']}"
    mod = Mod(url)
    mod.populate_details_manually(
        name=manualMod["name"],
        description=manualMod["description"],
        authors=manualMod["authors"]
    )
    mod.owner = manualMod["ghUser"]
    mod.repo = manualMod["ghRepo"]
    mods.append(mod)

for mod in mods:
    mod.populate()

# Save every mod to repos-gen.json
with open("repos-gen.json", "w") as f:
    json.dump([mod.__dict__ for mod in mods], f, indent=4, default=lambda o: o.__dict__)
