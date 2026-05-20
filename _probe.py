"""RTSP DESCRIBE probe on PORT 80 (with digest/basic auth)."""
import base64
import hashlib
import re
import socket

HOST, PORT = "192.168.2.108", 80
USER, PASS = "admin", "Baap@2025"


def describe(path, timeout=6):
    url = f"rtsp://{HOST}:{PORT}{path}"
    try:
        s = socket.create_connection((HOST, PORT), timeout=timeout)
        s.settimeout(timeout)
    except Exception as e:
        return f"CONN-FAIL {e}"
    state = {"cseq": 1}

    def send(extra=""):
        req = (f"DESCRIBE {url} RTSP/1.0\r\nCSeq: {state['cseq']}\r\n"
               f"User-Agent: probe\r\nAccept: application/sdp\r\n{extra}\r\n")
        state["cseq"] += 1
        try:
            s.sendall(req.encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            return data.decode(errors="replace")
        except Exception as e:
            return f"__ERR__ {e}"

    resp = send()
    if resp.startswith("__ERR__"):
        s.close()
        return resp
    status = resp.split("\r\n", 1)[0].strip()

    if "401" in status:
        m = re.search(r'WWW-Authenticate:\s*(\w+)\s+(.*)', resp, re.I)
        if m and m.group(1).lower() == "digest":
            p = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
            realm, nonce = p.get("realm", ""), p.get("nonce", "")
            ha1 = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
            ha2 = hashlib.md5(f"DESCRIBE:{url}".encode()).hexdigest()
            rsp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            resp = send(f'Authorization: Digest username="{USER}", realm="{realm}", '
                        f'nonce="{nonce}", uri="{url}", response="{rsp}"\r\n')
        elif m and m.group(1).lower() == "basic":
            tok = base64.b64encode(f"{USER}:{PASS}".encode()).decode()
            resp = send(f"Authorization: Basic {tok}\r\n")
        status = (resp.split("\r\n", 1)[0].strip()
                  if not resp.startswith("__ERR__") else resp)
    s.close()
    return status


PATHS = [
    "/video/live?channel=1&subtype=0&proto=Private3",
    "/video/live", "/stream1", "/stream2", "/live", "/live/0",
    "/cam/realmonitor?channel=1&subtype=0",
    "/h264/ch1/main/av_stream", "/11", "/video1",
]

print(f"Probing rtsp://{USER}:****@{HOST}:{PORT}\n")
for p in PATHS:
    try:
        print(f"  {p:48s} -> {describe(p)}")
    except Exception as e:
        print(f"  {p:48s} -> EXC {e}")
