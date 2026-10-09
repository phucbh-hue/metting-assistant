"""Google Drive REST v3 cho một người dùng đã kết nối (quyền drive.file), gọi thẳng bằng httpx.

Chỉ dùng những gì việc lưu biên bản cần: tìm / tạo / đổi tên / chuyển thư mục, tải biên bản lên thành Google Docs
(HTML được Drive chuyển đổi), tải tệp lớn (ghi âm) theo kiểu resumable, chia sẻ / gỡ chia sẻ, xóa.
Với drive.file, ứng dụng chỉ thấy file do nó tạo (hoặc người dùng chọn qua Google Picker), nên tìm thư mục theo tên chỉ
ra thư mục của chính ứng dụng.
"""
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from meeting import google_oauth

log = logging.getLogger("meeting.gdrive")

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
FIELDS = "id,name,mimeType,parents,trashed,webViewLink"
ALL = {"supportsAllDrives": "true"}


class DriveError(Exception):
    pass


def _q(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, email: str):
        self.email = (email or "").lower()

    async def _req(self, method: str, url: str, ok404: bool = False, **kw) -> Optional[httpx.Response]:
        for attempt in (1, 2):
            token = await google_oauth.access_token(self.email)
            headers = {"Authorization": f"Bearer {token}", **kw.get("headers", {})}
            async with httpx.AsyncClient(timeout=300, transport=google_oauth._transport) as c:
                r = await c.request(method, url, **{**kw, "headers": headers})
            if r.status_code == 401 and attempt == 1:          # access token vừa hết hạn: làm mới rồi thử lại
                google_oauth._ACCESS.pop(self.email, None)
                continue
            break
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise DriveError(f"Google Drive báo lỗi {r.status_code}: {msg}".rstrip(": "))
        return r

    # ------------------------------------------------------------- thư mục ---
    async def get(self, file_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not file_id:
            return None
        r = await self._req("GET", f"{API}/files/{file_id}", ok404=True, params={"fields": FIELDS, **ALL})
        f = r.json() if r is not None else None
        return None if not f or f.get("trashed") else f

    async def find_folder(self, name: str, parent: Optional[str]) -> Optional[Dict[str, Any]]:
        q = f"mimeType='{FOLDER}' and name='{_q(name)}' and trashed=false and '{_q(parent or 'root')}' in parents"
        r = await self._req("GET", f"{API}/files", params={"q": q, "fields": f"files({FIELDS})", "pageSize": "10",
                                                           "includeItemsFromAllDrives": "true", **ALL})
        files = r.json().get("files") or []
        return files[0] if files else None

    async def create_folder(self, name: str, parent: Optional[str]) -> Dict[str, Any]:
        meta = {"name": name, "mimeType": FOLDER, **({"parents": [parent]} if parent else {})}
        r = await self._req("POST", f"{API}/files", params={"fields": FIELDS, **ALL}, json=meta)
        return r.json()

    async def ensure_folder(self, name: str, parent: Optional[str], known_id: Optional[str] = None) -> Dict[str, Any]:
        """Thư mục đã biết (còn, chưa vào thùng rác) -> dùng lại; không thì tìm theo tên trong parent, rồi mới tạo mới."""
        f = await self.get(known_id)
        if f is not None:
            return f
        return await self.find_folder(name, parent) or await self.create_folder(name, parent)

    async def rename(self, file_id: str, name: str) -> Dict[str, Any]:
        r = await self._req("PATCH", f"{API}/files/{file_id}", params={"fields": FIELDS, **ALL}, json={"name": name})
        return r.json()

    async def move(self, file_id: str, new_parent: str, old_parent: Optional[str]) -> Dict[str, Any]:
        params = {"fields": FIELDS, "addParents": new_parent, **ALL}
        if old_parent:
            params["removeParents"] = old_parent
        r = await self._req("PATCH", f"{API}/files/{file_id}", params=params, json={})
        return r.json()

    # ---------------------------------------------------------------- tệp ---
    async def put_doc(self, name: str, html: str, parent: str, file_id: Optional[str] = None) -> Dict[str, Any]:
        """Tải HTML lên thành Google Docs (Drive tự chuyển đổi). Có file_id còn tồn tại thì ghi đè nội dung."""
        if file_id and await self.get(file_id) is None:
            file_id = None
        meta: Dict[str, Any] = {"name": name}
        if not file_id:
            meta.update(mimeType=DOC, parents=[parent])
        boundary = f"mc{uuid.uuid4().hex}"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n{html}\r\n--{boundary}--").encode("utf-8")
        url, method = (f"{UPLOAD}/files/{file_id}", "PATCH") if file_id else (f"{UPLOAD}/files", "POST")
        r = await self._req(method, url, params={"uploadType": "multipart", "fields": FIELDS, **ALL}, content=body,
                            headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return r.json()

    async def put_file(self, name: str, mime: str, path: str, parent: str, file_id: Optional[str] = None) -> Dict[str, Any]:
        """Tải tệp (ghi âm) theo kiểu resumable: xin địa chỉ phiên tải rồi gửi nội dung. Có file_id thì ghi đè."""
        if file_id and await self.get(file_id) is None:
            file_id = None
        size = os.path.getsize(path)
        meta = {"name": name} if file_id else {"name": name, "parents": [parent]}
        url, method = (f"{UPLOAD}/files/{file_id}", "PATCH") if file_id else (f"{UPLOAD}/files", "POST")
        r = await self._req(method, url, params={"uploadType": "resumable", "fields": FIELDS, **ALL}, json=meta,
                            headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)})
        session = r.headers.get("location")
        if not session:
            raise DriveError("Google Drive không trả về địa chỉ tải tệp")
        with open(path, "rb") as fh:
            data = fh.read()
        r = await self._req("PUT", session, content=data, headers={"Content-Type": mime})
        return r.json()

    async def delete(self, file_id: Optional[str]) -> None:
        if file_id:
            await self._req("DELETE", f"{API}/files/{file_id}", ok404=True, params=ALL)

    # ------------------------------------------------------------- chia sẻ ---
    async def share(self, file_id: str, email: str, role: str = "writer") -> str:
        r = await self._req("POST", f"{API}/files/{file_id}/permissions",
                            params={"sendNotificationEmail": "false", "fields": "id", **ALL},
                            json={"type": "user", "role": role, "emailAddress": email})
        return r.json()["id"]

    async def unshare(self, file_id: str, permission_id: str) -> None:
        await self._req("DELETE", f"{API}/files/{file_id}/permissions/{permission_id}", ok404=True, params=ALL)
