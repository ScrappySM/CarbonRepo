import requests, bs4

class GitHub():
    def __init__(self, token: str = None):
        self.token = token
        
        if token:
            self.headers = {
                "Authorization": f"Bearer {token}"
            }
        else:
            self.headers = {}
            
    def set_repo(self, owner: str, repo: str):
        self.uowner = owner
        self.urepo = repo
        
    def get(self, url: str):
        try:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"Failed to get data from {url}: {e}")
            return None
        
    def repo(self, owner: str = None, repo: str = None):
        owner = owner or self.uowner
        repo = repo or self.urepo
        repo_url = f"https://api.github.com/repos/{owner}/{repo}"
        return self.get(repo_url)
    
    def releases(self, owner: str = None, repo: str = None):
        owner = owner or self.uowner
        repo = repo or self.urepo
        releases_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
        return self.get(releases_url)
    
    def contributors(self, owner: str = None, repo: str = None):
        owner = owner or self.uowner
        repo = repo or self.urepo
        contributors_url = f"https://api.github.com/repos/{owner}/{repo}/contributors"
        return self.get(contributors_url)
    
    def social_preview(self, owner: str = None, repo: str = None):
        owner = owner or self.uowner
        repo = repo or self.urepo
        html_url = f"https://github.com/{owner}/{repo}"
        html = requests.get(html_url).text
        
        soup = bs4.BeautifulSoup(html, "html.parser")
        meta_tags = soup.find_all("meta", property="og:image")
        if meta_tags:
            return meta_tags[0].get("content")
        else:
            return None
        