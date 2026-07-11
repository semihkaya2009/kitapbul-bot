import urllib.parse
from pathlib import Path
path_str = "/downloads/kitap%20adı.pdf"
unquoted = urllib.parse.unquote(path_str.removeprefix("/downloads/"))
print(f"Unquoted: {unquoted}")
