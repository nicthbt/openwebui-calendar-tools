import logging

import requests

from src.common.exceptions import CustomToolException

log = logging.getLogger(__name__)


class OpenTerminalException(CustomToolException):
    pass


class OpenTerminalClient:
    def __init__(self, __request__, __metadata__):
        self.http = requests.Session()
        self.http.cookies.update(__request__.cookies)

        # Forward auth header
        authorization = __request__.headers.get("Authorization", None)
        if authorization is not None:
            self.http.headers.update({"Authorization": authorization})

        # Forward chat ID
        chat_id = __metadata__.get("chat_id", None)
        if chat_id is not None:
            self.http.headers.update({"X-Session-Id": chat_id})

        # Extract terminal ID
        terminal_id = __metadata__.get("terminal_id", None)
        if terminal_id is None:
            raise OpenTerminalException("No terminal set for this chat")

        # Extract internal server
        host, port = __request__.scope.get("server", None) or (None, None)
        if host is None or port is None:
            raise OpenTerminalException("Unable to detect internal server")

        # Build API routes
        self.server = f"http://{host}:{port}"
        self.api = {
            "CWD": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/cwd",
            ).make_absolute_url(base_url=self.server),
            "UPLOAD": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/upload",
            ).make_absolute_url(base_url=self.server),
            "VIEW": __request__.app.url_path_for(
                "proxy_terminal",
                server_id=terminal_id,
                path="files/view",
            ).make_absolute_url(base_url=self.server),
        }

    def __del__(self):
        self.http.close()

    def get_cwd(self, timeout: int = 10) -> str:
        response = self.http.get(
            self.api.get("CWD"),
            timeout=timeout,
        )
        response.raise_for_status()

        # Load response as JSON
        response_json = response.json()

        return response_json.get("cwd", None)

    def upload_file(
        self,
        filename: str,
        mimetype: str,
        content: bytes,
        directory: str = None,
        timeout: int = 60,
    ) -> dict:
        response = self.http.post(
            self.api.get("UPLOAD"),
            params={"directory": directory or self.get_cwd()},
            files={"file": (filename, content, mimetype or "application/octet-stream")},
            timeout=timeout,
        )
        response.raise_for_status()

        # Load response as JSON
        response_json = response.json()

        path = response_json.get("path", None)
        size = response_json.get("size", None)

        return {
            "path": path,
            "name": filename,
            "size": size,
            "content_type": mimetype,
        }

    def download_file(self, path: str, timeout: int = 60) -> bytes:
        response = self.http.get(
            self.api.get("VIEW"),
            params={"path": path},
            stream=True,
            timeout=timeout,
        )
        response.raise_for_status()

        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            content.extend(chunk)
        return bytes(content)
