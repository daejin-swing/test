import json
import urllib.request
import urllib.error
import mimetypes
import uuid


def post_json(url: str, json_data: dict, headers: dict | None = None, timeout: int = 5) -> tuple[int, dict | None]:
    """Posts JSON payload to URL using standard library urllib."""
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)

    body_bytes = json.dumps(json_data).encode("utf-8")
    req = urllib.request.Request(url, data=body_bytes, headers=req_headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            content = resp.read().decode("utf-8")
            try:
                parsed = json.loads(content) if content else {}
            except json.JSONDecodeError:
                parsed = {"raw": content}
            return status, parsed
    except urllib.error.HTTPError as e:
        content = e.read().decode("utf-8")
        try:
            parsed = json.loads(content) if content else {}
        except json.JSONDecodeError:
            parsed = {"raw": content}
        return e.code, parsed
    except Exception as e:
        raise e


def post_multipart(url: str, fields: dict, files: dict, headers: dict | None = None, timeout: int = 60) -> tuple[int, str]:
    """Posts multipart/form-data payload (fields and files) using standard library urllib."""
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    body = bytearray()

    # Add form fields
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    # Add files
    for field_name, (filename, file_data, content_type) in files.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8"))
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        if isinstance(file_data, (bytes, bytearray)):
            body.extend(file_data)
        else:
            body.extend(file_data.read())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    req_headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, data=bytes(body), headers=req_headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        raise e

