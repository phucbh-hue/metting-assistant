"""Lưu dữ liệu xuống máy khi không kết nối được MongoDB Atlas.

Server vẫn chạy trên mongomock (MongoDB trong bộ nhớ, toàn bộ mã đã kiểm thử với nó), nhưng mọi thay đổi được ghi
xuống thư mục data/local_db/ vài giây một lần và khi tắt server; lần chạy sau nạp lại. Nhờ vậy mất kết nối Atlas không
làm mất lịch sử cuộc họp.

Bố cục thư mục:
- <collection>.json                 : collection nhỏ (meetings, voices, counters, settings, ...)
- <collection>/<meeting_id>.json    : collection theo cuộc họp (câu thoại, người nói, sản phẩm AI, ...), mỗi cuộc họp
                                       một tệp để lúc đang họp chỉ ghi lại tệp của cuộc họp đó.

Lưu ý: thư mục này chứa nội dung cuộc họp và vector giọng nói (dữ liệu sinh trắc học theo Nghị định 13/2023/NĐ-CP),
không chia sẻ và không đưa lên git.
"""
import atexit
import logging
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from bson import json_util

log = logging.getLogger("meeting.localstore")

# Collection gắn với từng cuộc họp: ghi theo tệp riêng của cuộc họp
SHARDED = ("meeting_segments", "meeting_speakers", "identity_inferences", "ai_artifacts", "ai_interactions", "llm_usage")
WRITE_METHODS = {"insert_one", "insert_many", "update_one", "update_many", "replace_one", "delete_one", "delete_many",
                 "find_one_and_update", "find_one_and_replace", "find_one_and_delete", "bulk_write", "drop"}
ALL = "*"                       # đánh dấu: ghi lại toàn bộ collection
FLUSH_S = 2.0                   # chu kỳ ghi xuống đĩa
NO_MEETING = "_none"            # tên tệp cho bản ghi không thuộc cuộc họp nào (meeting_id = None)


def _json_default(o: Any) -> Any:
    """Kiểu dữ liệu ngoài JSON chuẩn (numpy, set) -> kiểu Python thường."""
    try:
        import numpy as np
        if isinstance(o, np.generic):
            return o.item()
        if isinstance(o, np.ndarray):
            return o.tolist()
    except ImportError:
        pass
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    raise TypeError(f"Không ghi được kiểu {type(o).__name__}")


def _shard_name(mid: Any) -> str:
    return NO_MEETING if mid is None else re.sub(r"[^0-9A-Za-z_-]", "_", str(mid))


def _mid_of_filter(flt: Any) -> Any:
    """meeting_id cụ thể trong bộ lọc, hoặc ALL nếu không xác định được (phải ghi lại cả collection)."""
    if isinstance(flt, dict) and "meeting_id" in flt and isinstance(flt["meeting_id"], (int, str, type(None))):
        return flt["meeting_id"]
    return ALL


def shards_of(col: str, method: str, args: tuple, kwargs: dict) -> Set[Any]:
    """Cuộc họp nào bị ảnh hưởng bởi một lệnh ghi."""
    if col not in SHARDED or method == "drop":
        return {ALL}
    if method == "insert_one":
        doc = args[0] if args else kwargs.get("document")
        return {doc.get("meeting_id") if isinstance(doc, dict) else ALL}
    if method == "insert_many":
        docs = args[0] if args else kwargs.get("documents") or []
        return {d.get("meeting_id") if isinstance(d, dict) else ALL for d in docs} or {ALL}
    if method == "bulk_write":
        ops = args[0] if args else kwargs.get("requests") or []
        out: Set[Any] = set()
        for op in ops:
            f = getattr(op, "_filter", None)
            if f is None and isinstance(getattr(op, "_doc", None), dict):      # InsertOne
                f = {"meeting_id": op._doc.get("meeting_id")}
            out.add(_mid_of_filter(f))
        return out or {ALL}
    flt = args[0] if args else kwargs.get("filter")
    return {_mid_of_filter(flt)}


class LocalStore:
    """Ghi mongomock xuống đĩa theo collection / cuộc họp, chỉ ghi lại phần vừa thay đổi."""

    def __init__(self, root: Path, raw_db):
        self.root = Path(root)
        self.raw = raw_db                     # mongomock Database thật (không qua lớp theo dõi)
        self.lock = threading.RLock()         # ghi và chụp dữ liệu không chen nhau
        self.dirty: Dict[str, Set[Any]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_flush_at = 0.0
        self.last_error = ""

    # ---------------------------------------------------------------- nạp ---
    def _files(self) -> Iterable[Path]:
        if not self.root.is_dir():
            return []
        out = sorted(self.root.glob("*.json"))
        for col in SHARDED:
            folder = self.root / col
            if folder.is_dir():
                out.extend(sorted(folder.glob("*.json")))
        return out

    def load(self) -> int:
        """Nạp toàn bộ dữ liệu đã lưu vào mongomock. Trả về số bản ghi."""
        total = 0
        for path in self._files():
            col = path.parent.name if path.parent != self.root else path.stem
            try:
                docs = json_util.loads(path.read_text(encoding="utf-8"))
            except Exception as e:      # tệp hỏng (tắt máy giữa chừng...): bỏ qua tệp đó, không làm hỏng cả server
                log.error("meeting.localstore: không đọc được %s (%s), bỏ qua tệp này", path, e)
                continue
            if isinstance(docs, list) and docs:
                self.raw[col].insert_many(docs)
                total += len(docs)
        return total

    # ---------------------------------------------------------------- ghi ---
    def mark(self, col: str, shards: Set[Any]):
        with self.lock:
            cur = self.dirty.setdefault(col, set())
            cur |= set(shards)

    def _read(self, col: str, flt: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self.lock:
            return list(self.raw[col].find(flt))

    def _write(self, path: Path, docs: List[Dict[str, Any]]):
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json_util.dumps(docs, ensure_ascii=False, default=_json_default)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(5):              # Windows: tệp đích có thể đang bị antivirus mở trong chốc lát
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.1 * (attempt + 1))
        os.replace(tmp, path)

    @staticmethod
    def _remove(path: Path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _dump(self, col: str, shards: Set[Any]):
        if col not in SHARDED:
            self._write(self.root / f"{col}.json", self._read(col, {}))
            return
        folder = self.root / col
        if ALL in shards:
            groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for d in self._read(col, {}):
                groups[_shard_name(d.get("meeting_id"))].append(d)
            for name, docs in groups.items():
                self._write(folder / f"{name}.json", docs)
            if folder.is_dir():
                for p in folder.glob("*.json"):
                    if p.stem not in groups:
                        self._remove(p)
            return
        for mid in shards:
            docs = self._read(col, {"meeting_id": mid})
            path = folder / f"{_shard_name(mid)}.json"
            if docs:
                self._write(path, docs)
            else:
                self._remove(path)

    def flush(self) -> int:
        """Ghi các phần đã thay đổi xuống đĩa. Trả về số collection đã ghi."""
        with self.lock:
            dirty, self.dirty = self.dirty, {}
        n = 0
        for col, shards in dirty.items():
            try:
                self._dump(col, shards)
                n += 1
            except Exception as e:
                self.last_error = f"{col}: {e}"
                log.warning("meeting.localstore: ghi %s lỗi (%s), sẽ thử lại", col, e)
                self.mark(col, shards)
        if n:
            self.last_flush_at = time.time()
        return n

    # ------------------------------------------------------------ chạy nền ---
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="localstore-flush", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _loop(self):
        while not self._stop.wait(FLUSH_S):
            self.flush()

    def close(self):
        self._stop.set()
        self.flush()


class StoreCollection:
    """Collection mongomock có theo dõi: lệnh ghi được đánh dấu để ghi xuống đĩa."""

    __slots__ = ("_store", "_name", "_col")

    def __init__(self, store: LocalStore, name: str, col):
        self._store, self._name, self._col = store, name, col

    def __getattr__(self, attr: str):
        target = getattr(self._col, attr)
        if attr not in WRITE_METHODS or not callable(target):
            return target
        store, name = self._store, self._name

        def write(*args, **kwargs):
            with store.lock:
                try:
                    return target(*args, **kwargs)
                finally:
                    store.mark(name, shards_of(name, attr, args, kwargs))
        return write

    def __repr__(self):
        return f"StoreCollection({self._name!r})"


class StoreDatabase:
    """Database mongomock có theo dõi, thay cho _db trong meeting.db khi lưu trên máy."""

    def __init__(self, store: LocalStore, raw_db):
        self._store, self._raw = store, raw_db

    @property
    def store(self) -> LocalStore:
        return self._store

    def __getitem__(self, name: str) -> StoreCollection:
        return StoreCollection(self._store, name, self._raw[name])

    def get_collection(self, name: str, **kwargs) -> StoreCollection:
        return StoreCollection(self._store, name, self._raw.get_collection(name, **kwargs))

    def drop_collection(self, name, *args, **kwargs):
        col = name if isinstance(name, str) else getattr(name, "name", str(name))
        with self._store.lock:
            try:
                return self._raw.drop_collection(col, *args, **kwargs)
            finally:
                self._store.mark(col, {ALL})

    def __getattr__(self, attr: str):
        val = getattr(self._raw, attr)
        try:
            import mongomock
            if isinstance(val, mongomock.Collection):
                return StoreCollection(self._store, attr, val)
        except ImportError:
            pass
        return val


def open_store(root: Path, db_name: str = "meeting_assistant"):
    """Mở kho trên máy: mongomock + dữ liệu đã lưu. Trả về (StoreDatabase, số bản ghi đã nạp)."""
    import mongomock
    raw = mongomock.MongoClient()[db_name]
    store = LocalStore(root, raw)
    n = store.load()
    return StoreDatabase(store, raw), n


def pending_meetings(root: Path) -> List[Dict[str, Any]]:
    """Cuộc họp trong kho trên máy chưa được đồng bộ lên Atlas (đọc thẳng tệp, không mở kho)."""
    path = Path(root) / "meetings.json"
    if not path.exists():
        return []
    try:
        docs = json_util.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [{"id": d.get("id"), "title": d.get("title", ""), "started_at": d.get("started_at"), "status": d.get("status")}
            for d in docs if isinstance(d, dict) and not d.get("synced_at")]


# ------------------------------------------------------------ đồng bộ Atlas ---
# Trường id toàn cục của từng collection theo cuộc họp: kiểm tra trùng trước khi chép lên Atlas
ID_FIELDS = {"meeting_segments": "id", "identity_inferences": "id", "ai_artifacts": "id", "ai_interactions": "id"}


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    d = dict(doc)
    d.pop("_id", None)
    return d


def sync_to(target, root: Path, dry_run: bool = False, db_name: str = "meeting_assistant") -> Dict[str, Any]:
    """Chép các cuộc họp ghi lúc mất kết nối (kho trên máy) lên target (Database Atlas).

    - Cuộc họp đã có trên Atlas (cùng id, cùng thời điểm tạo) chỉ được đánh dấu đã đồng bộ.
    - Cuộc họp trùng id nhưng khác nội dung, hoặc có bản ghi trùng id: bỏ qua và báo lại, không ghi đè Atlas.
    - Hồ sơ giọng trùng tên trên Atlas: dùng hồ sơ của Atlas, đổi liên kết trong cuộc họp sang hồ sơ đó.
    """
    sdb, _ = open_store(root, db_name)
    src = sdb._raw
    report: Dict[str, Any] = {"meetings": [], "already": [], "conflicts": [], "voices": [], "dry_run": dry_run}

    vmap: Dict[Any, Any] = {}
    for v in src["voices"].find({}):
        name = str(v.get("name") or "").strip()
        same = target["voices"].find_one({"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}}) if name else None
        if same:
            vmap[v["id"]] = same["id"]
            continue
        if target["voices"].find_one({"id": v["id"]}):
            report["conflicts"].append({"voice": name, "reason": f"mã giọng {v['id']} đã có trên Atlas"})
            vmap[v["id"]] = None
            continue
        if not dry_run:
            target["voices"].insert_one(_clean(v))
        vmap[v["id"]] = v["id"]
        report["voices"].append(name)

    synced_ids = []
    for m in src["meetings"].find({}).sort("id", 1):
        if m.get("synced_at"):
            continue
        mid = m["id"]
        tm = target["meetings"].find_one({"id": mid})
        if tm:
            if abs(float(tm.get("created_at") or 0) - float(m.get("created_at") or 0)) < 1.0:
                report["already"].append(mid)
                synced_ids.append(mid)
            else:
                report["conflicts"].append({"meeting": mid, "title": m.get("title", ""),
                                            "reason": "Atlas đã có cuộc họp khác cùng mã"})
            continue
        related = {col: list(src[col].find({"meeting_id": mid})) for col in SHARDED}
        clash = ""
        for col, field in ID_FIELDS.items():
            ids = [d[field] for d in related[col] if d.get(field) is not None]
            if ids and target[col].count_documents({field: {"$in": ids}}):
                clash = f"{col} có mã trùng trên Atlas"
                break
        if clash:
            report["conflicts"].append({"meeting": mid, "title": m.get("title", ""), "reason": clash})
            continue
        for d in related["meeting_speakers"]:
            if d.get("voice_id") in vmap:
                d["voice_id"] = vmap[d["voice_id"]]
        for d in related["meeting_segments"]:
            if d.get("speaker_id") in vmap:
                d["speaker_id"] = vmap[d["speaker_id"]]
        if not dry_run:
            target["meetings"].insert_one(_clean(m))
            for col, docs in related.items():
                if docs:
                    target[col].insert_many([_clean(d) for d in docs])
        synced_ids.append(mid)
        report["meetings"].append({"id": mid, "title": m.get("title", ""),
                                   "segments": len(related["meeting_segments"]),
                                   "artifacts": len(related["ai_artifacts"])})

    if synced_ids and not dry_run:
        sdb["meetings"].update_many({"id": {"$in": synced_ids}}, {"$set": {"synced_at": time.time()}})
        sdb.store.flush()
    return report
