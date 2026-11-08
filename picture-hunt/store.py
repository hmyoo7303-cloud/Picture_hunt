"""Where rooms and photos are saved.

- SUPABASE_URL + SUPABASE_SECRET_KEY set  -> Supabase (Postgres table + Storage bucket)
- otherwise                                -> local disk (DATA_DIR, default ./data), good for testing
"""
import json
import os
import shutil
from pathlib import Path

import requests


class LocalStore:
    kind = "local"

    def __init__(self, root):
        self.root = Path(root) / "rooms"
        self.root.mkdir(parents=True, exist_ok=True)

    def save_room(self, code, room):
        folder = self.root / code
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "state.tmp"
        tmp.write_text(json.dumps(room))
        tmp.replace(folder / "state.json")

    def load_rooms(self):
        rooms = {}
        for f in self.root.glob("*/state.json"):
            try:
                rooms[f.parent.name] = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        return rooms

    def put_image(self, code, kind, name, data):
        folder = self.root / code / kind
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    def delete_image(self, code, kind, name):
        (self.root / code / kind / name).unlink(missing_ok=True)

    def delete_room(self, code):
        shutil.rmtree(self.root / code, ignore_errors=True)

    def url(self, code, kind, name):
        return f"/media/{code}/{kind}/{name}" if name else None

    def local_path(self, code, kind):
        return self.root / code / kind


class SupabaseStore:
    kind = "supabase"
    TABLE = "bingo_rooms"

    def __init__(self, url, key, bucket):
        self.base = url.rstrip("/")
        self.bucket = bucket
        self.http = requests.Session()
        self.http.headers.update({"apikey": key, "Authorization": f"Bearer {key}"})

    def _check(self, res):
        if res.status_code >= 400:
            raise RuntimeError(f"Supabase {res.status_code}: {res.text[:300]}")
        return res

    # --- rooms (one JSON row per room)
    def save_room(self, code, room):
        self._check(self.http.post(
            f"{self.base}/rest/v1/{self.TABLE}",
            json={"code": code, "state": room},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=15,
        ))

    def load_rooms(self):
        res = self._check(self.http.get(
            f"{self.base}/rest/v1/{self.TABLE}", params={"select": "code,state"}, timeout=30))
        return {row["code"]: row["state"] for row in res.json()}

    def delete_room(self, code):
        self._check(self.http.delete(
            f"{self.base}/rest/v1/{self.TABLE}", params={"code": f"eq.{code}"}, timeout=15))
        paths = []
        for kind in ("locations", "selfies"):
            res = self._check(self.http.post(
                f"{self.base}/storage/v1/object/list/{self.bucket}",
                json={"prefix": f"{code}/{kind}", "limit": 1000}, timeout=15))
            paths += [f"{code}/{kind}/{o['name']}" for o in res.json()]
        if paths:
            self._remove(paths)

    # --- photos (public bucket, served straight from Supabase)
    def put_image(self, code, kind, name, data):
        self._check(self.http.post(
            f"{self.base}/storage/v1/object/{self.bucket}/{code}/{kind}/{name}",
            data=data,
            headers={"Content-Type": "image/jpeg", "x-upsert": "true",
                     "Cache-Control": "max-age=86400"},
            timeout=60,
        ))

    def delete_image(self, code, kind, name):
        self._remove([f"{code}/{kind}/{name}"])

    def _remove(self, paths):
        self._check(self.http.delete(
            f"{self.base}/storage/v1/object/{self.bucket}", json={"prefixes": paths}, timeout=15))

    def url(self, code, kind, name):
        if not name:
            return None
        return f"{self.base}/storage/v1/object/public/{self.bucket}/{code}/{kind}/{name}"


def make_store(default_dir):
    url = os.environ.get("SUPABASE_URL")
    # new secret key (sb_secret_...) or legacy service_role key both work
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return SupabaseStore(url, key, os.environ.get("SUPABASE_BUCKET", "bingo"))
    return LocalStore(os.environ.get("DATA_DIR", default_dir))
