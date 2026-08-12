"""Push the HTML report to Hub (additional_docs/HUB_ACCESS.md).

Hub runs on vision:8800. The SSH/ProxyJump dance in HUB_ACCESS.md is for reaching
it from a laptop outside the LAN - anything already on 192.168.50.0/24 (Bruce, or
vision itself) talks to it directly, so there is no tunnel to manage here.

    python hub.py --file reports/universe_review.html --name universe_review.html

The API has no auth: whoever can reach the port can POST. That is the existing
posture, not something this module chose.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

log = logging.getLogger(__name__)

HUB_URL = os.environ.get("HUB_URL", "http://192.168.50.39:8800")
# "Asset Universe", created empty and clearly meant for this. Override for a
# one-off push somewhere else.
COLLECTION_ID = int(os.environ.get("HUB_COLLECTION_ID", "53"))
TIMEOUT = (10, 300)          # (connect, read) - a 1.4 MB body over LAN is quick


def health(url: str = HUB_URL) -> bool:
    try:
        r = requests.get(f"{url}/api/health", timeout=(5, 10))
        return r.ok and r.json().get("ok") is True
    except requests.RequestException as e:
        log.warning("hub unreachable at %s: %s", url, e)
        return False


def delete_by_name(name: str, collection_id: int, url: str = HUB_URL) -> int:
    """Delete every item in a collection with this exact name. Returns the count.

    The API has no upsert, so a same-day re-run of the nightly job (--load-only,
    --reuse-deep) would leave two items called universe_review_<date>.html with no
    way to tell which is current. Replacing by name keeps one item per run_date.
    """
    try:
        r = requests.get(f"{url}/api/items", params={"collection_id": collection_id},
                         timeout=(5, 30))
        r.raise_for_status()
        body = r.json()
        items = body.get("items", body) if isinstance(body, dict) else body
    except (requests.RequestException, ValueError) as e:
        log.warning("could not list hub collection %s: %s", collection_id, e)
        return 0
    n = 0
    for it in items:
        if it.get("name") == name:
            try:
                requests.delete(f"{url}/api/items/{it['id']}", timeout=(5, 30)
                                ).raise_for_status()
                n += 1
            except requests.RequestException as e:
                log.warning("could not delete hub item %s: %s", it.get("id"), e)
    return n


def upload_file(path: str | Path, name: str, *, collection_id: int = COLLECTION_ID,
                tags: tuple[str, ...] = (), tagged_users: tuple[str, ...] = (),
                description: str = "", mime: str = "text/html",
                replace: bool = False, url: str = HUB_URL) -> dict:
    """Multipart streaming upload. Returns the created item.

    /api/items/upload rather than the JSON endpoint: the JSON path inlines the file
    in the request body and is capped by Express's body parser, which the report is
    already close enough to that it would break as the universe grows.

    `replace=True` removes same-named items in the collection first, so re-running
    a day updates that day rather than duplicating it.
    """
    import json
    path = Path(path)
    if replace:
        gone = delete_by_name(name, collection_id, url)
        if gone:
            log.info("replaced %d existing hub item(s) named %s", gone, name)
    data = {"name": name, "collectionId": str(collection_id)}
    if tags:
        data["tags"] = json.dumps(list(tags))
    if tagged_users:
        data["taggedUsers"] = json.dumps(list(tagged_users))
    if description:
        data["description"] = description
    with path.open("rb") as f:
        r = requests.post(f"{url}/api/items/upload",
                          files={"file": (name, f, mime)}, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True)
    ap.add_argument("--name", default=None, help="display name (default: basename)")
    ap.add_argument("--collection-id", type=int, default=COLLECTION_ID)
    ap.add_argument("--tags", default="", help="comma-separated")
    ap.add_argument("--users", default="", help="comma-separated")
    ap.add_argument("--description", default="")
    ap.add_argument("--replace", action="store_true",
                    help="delete same-named items in the collection first")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    split = lambda s: tuple(x.strip() for x in s.split(",") if x.strip())  # noqa: E731
    item = upload_file(a.file, a.name or Path(a.file).name,
                       collection_id=a.collection_id, tags=split(a.tags),
                       tagged_users=split(a.users), description=a.description,
                       replace=a.replace)
    print(f"uploaded -> id={item.get('item', item).get('id')} "
          f"collection={a.collection_id}  {HUB_URL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
