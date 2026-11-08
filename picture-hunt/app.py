"""Picture Hunt (photo lockout bingo) - Flask server for Render.

Flow
1. PICTURE LIBRARY (/setup, admin password): upload location pictures once (easy / medium / hard).
   They stay in the database and are reused by every game.
2. CREATE BOARD: 25 pictures are mixed at random from the library into a 5x5 board,
   difficulty balanced per row/column/diagonal. The creator's browser is the room host.
3. Players pick their team, tap a square, take a selfie at that place and upload it.
   The square is claimed and locked for every other team (lockout).
4. First BINGO (row, column or diagonal) wins. No BINGO? The team with the most tiles wins.
5. REFRESH clears every claim on the same board. START makes a new random mix from the library.
History keeps every claim with its selfie.

Storage: Supabase (Postgres + Storage) when SUPABASE_URL / SUPABASE_SECRET_KEY are set,
otherwise local disk (./data). See store.py and supabase_setup.sql.
"""
import hmac
import io
import os
import random
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

from store import make_store

BASE = Path(__file__).parent
store = make_store(BASE / "data")  # Supabase if SUPABASE_URL is set, else local disk
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # protects the picture library

LIB = "LIBRARY"  # storage key for the shared picture library (room codes are 4 letters)
DIFFICULTY = {"easy": 1, "medium": 2, "hard": 3}
TEAM_COLORS = ["#e5484d", "#3e63dd", "#30a46c", "#8e4ec6"]
SIZE = 5
CELLS = SIZE * SIZE
MAX_SIDE = 1280  # uploaded photos are resized to this
ROOM_TTL = 3 * 24 * 3600

LINES = (
    [[r * SIZE + c for c in range(SIZE)] for r in range(SIZE)]
    + [[r * SIZE + c for r in range(SIZE)] for c in range(SIZE)]
    + [[i * SIZE + i for i in range(SIZE)], [i * SIZE + (SIZE - 1 - i) for i in range(SIZE)]]
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024  # a batch of phone photos
lock = threading.Lock()
rooms = {}
library = {"locations": [], "version": 1}


# ---------------------------------------------------------------- storage
def save_room(code):
    store.save_room(code, rooms[code])


def save_library():
    store.save_room(LIB, library)


def load_all():
    data = store.load_rooms()
    lib = data.pop(LIB, None)
    if lib:
        library.update(lib)
    rooms.update(data)


def save_image(file_storage, owner, kind):
    """Fix phone rotation, shrink, save as JPEG. Returns the file name."""
    try:
        img = Image.open(file_storage.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception:
        abort(400, description=f"Not an image: {file_storage.filename}")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    name = uuid.uuid4().hex[:12] + ".jpg"
    store.put_image(owner, kind, name, buf.getvalue())
    return name


CAMERA_NAME = re.compile(r"^(img|dsc|dscn|pxl|photo|image|screenshot|kakaotalk)?[\s_\-]*[\d_\-\s]+$", re.I)


def label_from_filename(name):
    """Optional place name. Phone camera names like IMG_1234 give no name."""
    stem = Path(name or "").stem
    if CAMERA_NAME.match(stem):
        return ""
    return re.sub(r"[_\-]+", " ", stem).strip()[:40]


def square_name(idx):
    return "BINGO"[idx % SIZE] + str(idx // SIZE + 1)


# ---------------------------------------------------------------- board
def line_cost(weights):
    sums = [sum(weights[i] for i in line) for line in LINES]
    mean = sum(sums) / len(sums)
    return sum((s - mean) ** 2 for s in sums)


def pick_counts(pool, rng):
    """Aim for an even split of difficulties (8 easy / 9 medium / 8 hard)."""
    avail = {d: sum(1 for p in pool if p["difficulty"] == d) for d in DIFFICULTY}
    target = {"easy": 8, "medium": 9, "hard": 8}
    counts = {d: min(target[d], avail[d]) for d in DIFFICULTY}
    while sum(counts.values()) < CELLS:
        spare = [d for d in DIFFICULTY if counts[d] < avail[d]]
        counts[rng.choice(spare)] += 1
    return counts


def build_board():
    """Pick 25 library pictures at random, then shuffle so every line has a similar difficulty total.
    The room keeps its own copy, so editing the library later never breaks a running game."""
    pool = library["locations"]
    if len(pool) < CELLS:
        raise ValueError(f"The picture library needs at least {CELLS} pictures "
                         f"({len(pool)} now). Add more on the Pictures page.")
    rng = random.Random(secrets.randbits(32))
    chosen = []
    for diff, n in pick_counts(pool, rng).items():
        chosen += rng.sample([p for p in pool if p["difficulty"] == diff], n)
    rng.shuffle(chosen)

    weights = [DIFFICULTY[p["difficulty"]] for p in chosen]
    cost = line_cost(weights)
    for _ in range(6000):
        a, b = rng.randrange(CELLS), rng.randrange(CELLS)
        if weights[a] == weights[b]:
            continue
        weights[a], weights[b] = weights[b], weights[a]
        new = line_cost(weights)
        if new <= cost:
            cost = new
            chosen[a], chosen[b] = chosen[b], chosen[a]
        else:
            weights[a], weights[b] = weights[b], weights[a]
        if cost == 0:
            break
    return [dict(p) for p in chosen]


# ---------------------------------------------------------------- game rules
def can_still_bingo(claims, team):
    return any(all(claims[i] in (None, team) for i in line) for line in LINES)


def finish_most_tiles(room, why):
    counts = [room["claims"].count(t) for t in range(len(room["teams"]))]
    best = max(counts)
    leaders = [t for t, c in enumerate(counts) if c == best]
    room["status"] = "finished"
    if len(leaders) == 1:
        room["winner"] = leaders[0]
        room["reason"] = f"Most tiles ({best}) · {why}"
    else:
        room["winner"] = None
        room["reason"] = f"Tie at {best} tiles · {why}"
    add_history(room, "end", text=room["reason"])


def check_game_over(room):
    claims = room["claims"]
    n = len(room["teams"])
    room["note"] = ""

    for line in LINES:  # BINGO wins instantly
        owner = claims[line[0]]
        if owner is not None and all(claims[i] == owner for i in line):
            room.update(status="finished", winner=owner, reason="BINGO!", winning_line=line)
            add_history(room, "end", team=owner, text="BINGO!")
            return

    open_cells = claims.count(None)
    if open_cells == 0:
        finish_most_tiles(room, "board complete")
        return

    counts = [claims.count(t) for t in range(n)]
    for t in range(n):  # clinch: can't be caught on tiles and nobody else can still bingo
        others = [o for o in range(n) if o != t]
        if all(counts[o] + open_cells < counts[t] for o in others) and \
                not any(can_still_bingo(claims, o) for o in others):
            room.update(status="finished", winner=t, reason=f"Clinched with {counts[t]} tiles")
            add_history(room, "end", team=t, text=room["reason"])
            return

    if not any(can_still_bingo(claims, t) for t in range(n)):
        room["note"] = "No BINGO possible anymore. Most tiles wins."


def add_history(room, kind, **data):
    room["history"].append({"id": uuid.uuid4().hex[:8], "kind": kind, "t": time.time(), **data})


def clear_board_claims(room):
    room.update(claims=[None] * CELLS, selfies=[None] * CELLS, claimed_at=[None] * CELLS,
                status="playing", winner=None, reason="", note="", winning_line=[])


# ---------------------------------------------------------------- helpers
def get_room(code):
    code = code.upper()
    room = rooms.get(code)
    if not room:
        abort(404, description="Room not found.")
    return code, room


def is_host(room):
    return request.headers.get("X-Host-Token") == room["host_token"]


def require_host(room):
    if not is_host(room):
        abort(403, description="Only the host can do that.")


def require_admin():
    given = request.headers.get("X-Admin-Password", "")
    if ADMIN_PASSWORD and not hmac.compare_digest(given, ADMIN_PASSWORD):
        abort(403, description="Wrong admin password.")


def cell_label(room, idx):
    return room["board"][idx]["label"] or square_name(idx)


def public_state(code, room, host=False):
    board = []
    for i, p in enumerate(room["board"]):
        revealed = host or room["claims"][i] is not None  # place name shows after a claim
        board.append({
            "square": square_name(i),
            "label": p["label"] if revealed else "",
            "difficulty": p["difficulty"],
            "src": store.url(LIB, "locations", p["file"]),
            "selfie": store.url(code, "selfies", room["selfies"][i]),
            "claimed_at": room["claimed_at"][i],
        })
    history = []
    for h in room["history"][-300:]:
        item = dict(h)
        if item.get("selfie"):
            item["selfie"] = store.url(code, "selfies", item["selfie"])
        history.append(item)
    return {
        "code": code,
        "status": room["status"],
        "teams": room["teams"],
        "board": board,
        "claims": room["claims"],
        "scores": [room["claims"].count(t) for t in range(len(room["teams"]))],
        "winner": room["winner"],
        "reason": room["reason"],
        "note": room["note"],
        "winning_line": room["winning_line"],
        "history": history,
        "library_size": len(library["locations"]),
        "version": room["version"],
    }


def room_changed(code, room):
    room["version"] += 1
    save_room(code)
    return jsonify(public_state(code, room, is_host(room)))


def library_state():
    locs = library["locations"]
    return {
        "locations": [{"id": p["id"], "label": p["label"], "difficulty": p["difficulty"],
                       "src": store.url(LIB, "locations", p["file"])} for p in locs],
        "counts": {d: sum(1 for p in locs if p["difficulty"] == d) for d in DIFFICULTY},
        "needed": CELLS,
        "version": library["version"],
    }


def library_changed():
    library["version"] += 1
    save_library()
    return jsonify(library_state())


def file_in_use(name):
    return any(p["file"] == name for r in rooms.values() for p in r["board"])


def cleanup():
    now = time.time()
    for code in [c for c, r in rooms.items() if now - r["created"] > ROOM_TTL]:
        rooms.pop(code, None)
        try:
            store.delete_room(code)  # removes the room row and its selfies, never library pictures
        except Exception as e:  # cleanup is best effort
            app.logger.warning("cleanup %s failed: %s", code, e)


# ---------------------------------------------------------------- pages
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/setup")
def setup_page():
    return render_template("setup.html", needs_password=bool(ADMIN_PASSWORD))


@app.get("/room/<code>")
def room_page(code):
    code, _ = get_room(code)
    return render_template("room.html", code=code)


@app.get("/media/<owner>/<kind>/<name>")
def media_file(owner, kind, name):
    """Only used in local-disk mode. With Supabase, photos come from the public bucket."""
    if kind not in ("locations", "selfies") or store.kind != "local":
        abort(404)
    return send_from_directory(store.local_path(owner.upper(), kind), name, max_age=86400)


@app.get("/healthz")
def health():
    return "ok"


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def json_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error=e.description), e.code
    return f"<h2>{e.code}</h2><p>{e.description}</p><a href='/'>Home</a>", e.code


# ---------------------------------------------------------------- API: picture library (admin)
@app.get("/api/library/summary")
def library_summary():
    """Public: just how many pictures there are, for the lobby."""
    s = library_state()
    return jsonify(total=len(library["locations"]), counts=s["counts"], needed=CELLS)


@app.get("/api/library")
def library_list():
    require_admin()
    with lock:
        return jsonify(library_state())


@app.post("/api/library")
def library_upload():
    require_admin()
    diff = request.form.get("difficulty", "medium")
    if diff not in DIFFICULTY:
        abort(400, description="Bad difficulty.")
    files = request.files.getlist("photos")
    if not files:
        abort(400, description="No photos selected.")
    new = [{"id": uuid.uuid4().hex[:8], "label": label_from_filename(f.filename),
            "difficulty": diff, "file": save_image(f, LIB, "locations")} for f in files]
    with lock:
        library["locations"] += new
        return library_changed()


@app.patch("/api/library/<loc_id>")
def library_edit(loc_id):
    require_admin()
    data = request.get_json(silent=True) or {}
    with lock:
        loc = next((p for p in library["locations"] if p["id"] == loc_id), None)
        if not loc:
            abort(404, description="Picture not found.")
        if data.get("difficulty") in DIFFICULTY:
            loc["difficulty"] = data["difficulty"]
        if "label" in data:  # empty is allowed: names are optional
            loc["label"] = str(data["label"]).strip()[:40]
        return library_changed()


@app.delete("/api/library/<loc_id>")
def library_delete(loc_id):
    require_admin()
    with lock:
        loc = next((p for p in library["locations"] if p["id"] == loc_id), None)
        if loc:
            library["locations"].remove(loc)
            if not file_in_use(loc["file"]):  # a running game may still show it
                store.delete_image(LIB, "locations", loc["file"])
        return library_changed()


# ---------------------------------------------------------------- API: rooms
@app.post("/api/rooms")
def create_room():
    """New game: teams + a random board mixed from the library."""
    data = request.get_json(silent=True) or {}
    names = [str(n).strip()[:20] for n in data.get("teams", []) if str(n).strip()]
    if not 2 <= len(names) <= 4:
        abort(400, description="Use 2 to 4 teams.")
    with lock:
        try:
            board = build_board()
        except ValueError as e:
            abort(400, description=str(e))
        cleanup()
        alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ"
        code = "".join(random.choices(alphabet, k=4))
        while code in rooms or code == LIB:
            code = "".join(random.choices(alphabet, k=4))
        token = secrets.token_urlsafe(16)
        room = {
            "host_token": token, "created": time.time(),
            "teams": [{"name": n, "color": TEAM_COLORS[i]} for i, n in enumerate(names)],
            "board": board, "history": [], "version": 1,
        }
        clear_board_claims(room)
        add_history(room, "start", text="Game created")
        rooms[code] = room
        save_room(code)
    return jsonify(code=code, host_token=token)


@app.get("/api/rooms/<code>")
def room_state(code):
    with lock:
        code, room = get_room(code)
        if request.args.get("v", type=int) == room["version"]:
            return jsonify(unchanged=True)
        return jsonify(public_state(code, room, is_host(room)))


@app.post("/api/rooms/<code>/start")
def start(code):
    """New random mix from the library and a clean board."""
    with lock:
        code, room = get_room(code)
        require_host(room)
        try:
            room["board"] = build_board()
        except ValueError as e:
            abort(400, description=str(e))
        clear_board_claims(room)
        add_history(room, "start", text="New random board")
        return room_changed(code, room)


@app.post("/api/rooms/<code>/refresh")
def refresh(code):
    """Clear every claim, keep the same board."""
    with lock:
        code, room = get_room(code)
        require_host(room)
        clear_board_claims(room)
        add_history(room, "refresh", text="Board refreshed")
        return room_changed(code, room)


@app.post("/api/rooms/<code>/end")
def end(code):
    with lock:
        code, room = get_room(code)
        require_host(room)
        if room["status"] == "playing":
            finish_most_tiles(room, "ended by host")
        return room_changed(code, room)


# ---------------------------------------------------------------- API: play
@app.post("/api/rooms/<code>/cells/<int:idx>/claim")
def claim(code, idx):
    """Upload a selfie for a square. First valid upload takes it (lockout)."""
    code, room = get_room(code)
    team = request.form.get("team", type=int)
    photo = request.files.get("selfie")

    def validate():
        if room["status"] != "playing":
            abort(409, description="The game is not running.")
        if not 0 <= idx < CELLS:
            abort(400, description="Bad square.")
        if team is None or not 0 <= team < len(room["teams"]):
            abort(400, description="Pick your team first.")
        owner = room["claims"][idx]
        if owner is not None:
            abort(409, description=f"Already taken by {room['teams'][owner]['name']}.")

    with lock:
        validate()
    if not photo:
        abort(400, description="Take a selfie first.")
    name = save_image(photo, code, "selfies")
    with lock:
        try:
            validate()  # another team may have uploaded while we were saving
        except Exception:
            store.delete_image(code, "selfies", name)
            raise
        room["claims"][idx] = team
        room["selfies"][idx] = name
        room["claimed_at"][idx] = time.time()
        add_history(room, "claim", team=team, cell=idx, label=cell_label(room, idx), selfie=name)
        check_game_over(room)
        return room_changed(code, room)


@app.post("/api/rooms/<code>/cells/<int:idx>/clear")
def clear_cell(code, idx):
    """Undo a claim. The team that owns it or the host can do this."""
    data = request.get_json(silent=True) or {}
    with lock:
        code, room = get_room(code)
        if not 0 <= idx < CELLS or room["claims"][idx] is None:
            abort(400, description="That square is not claimed.")
        owner = room["claims"][idx]
        host = is_host(room)
        if not host and (room["status"] != "playing" or data.get("team") != owner):
            abort(403, description="Only that team or the host can undo it.")
        room["claims"][idx] = None
        room["selfies"][idx] = None
        room["claimed_at"][idx] = None
        add_history(room, "unclaim", team=owner, cell=idx, label=cell_label(room, idx),
                    text="cleared by host" if host else "undone by team")
        if room["status"] == "finished":
            room.update(status="playing", winner=None, reason="", winning_line=[])
        check_game_over(room)
        return room_changed(code, room)


load_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
