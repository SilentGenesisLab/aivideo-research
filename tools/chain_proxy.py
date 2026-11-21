"""本地链式代理：对外提供普通 HTTP 代理，内部按
   客户端 → 本机 → 韩国 SOCKS5 → 住宅 HTTP 代理 → 目标站
依次建隧道。

为什么需要它：yt-dlp 只支持单级 --proxy，不支持 preproxy；而住宅代理
明确拒绝中国大陆 IP（403 "Mainland China IP ... banned"），必须先经境外出口。

两种用法：
  单条（老用法，保持兼容）：
    python chain_proxy.py --listen 8899 --socks 127.0.0.1:1080 \
        --upstream <住宅代理IP>:443 --user <用户名> --password <密码>
  多条（一个进程起多个区域出口）：
    python chain_proxy.py --config /etc/videodata-chainproxy/regions.json
    # config: {"socks": "127.0.0.1:1080", "entries": [
    #   {"label": "sea", "listen": 8900, "upstream": "<住宅代理IP>:443",
    #    "user": "...", "password": "..."}, ...]}
"""
import argparse
import base64
import json
import select
import socket
import struct
import threading


def read_until(sock, delim, limit=65536):
    buf = b""
    while delim not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("upstream closed")
        buf += chunk
        if len(buf) > limit:
            raise ConnectionError("header too large")
    head, _, rest = buf.partition(delim)
    return head, rest


def socks5_connect(dest_host, dest_port, socks_host, socks_port, timeout=25):
    s = socket.create_connection((socks_host, socks_port), timeout=timeout)
    s.sendall(b"\x05\x01\x00")
    if s.recv(2) != b"\x05\x00":
        raise ConnectionError("socks5 no-auth rejected")
    hb = dest_host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack("!H", dest_port))
    resp = s.recv(4)
    if len(resp) < 4 or resp[1] != 0:
        raise ConnectionError("socks5 connect failed: %r" % (resp,))
    atyp = resp[3]
    if atyp == 1:
        s.recv(4)
    elif atyp == 3:
        n = s.recv(1)[0]
        s.recv(n)
    elif atyp == 4:
        s.recv(16)
    s.recv(2)
    return s


def relay(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def handle(client, e):
    try:
        head, rest = read_until(client, b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode("latin1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client.sendall(b"HTTP/1.1 405 Only CONNECT Supported\r\n\r\n")
            client.close()
            return
        target = parts[1]
        thost, _, tport = target.rpartition(":")

        up = socks5_connect(e["upstream_host"], e["upstream_port"],
                            e["socks_host"], e["socks_port"])
        auth = base64.b64encode(f"{e['user']}:{e['password']}".encode()).decode()
        up.sendall((
            f"CONNECT {target} HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"Proxy-Authorization: Basic {auth}\r\n"
            f"Proxy-Connection: Keep-Alive\r\n\r\n"
        ).encode())
        h, leftover = read_until(up, b"\r\n\r\n")
        status = h.split(b"\r\n")[0].decode("latin1", "replace")
        if " 200 " not in status + " ":
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            client.close()
            up.close()
            return
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        if rest:
            up.sendall(rest)
        if leftover:
            client.sendall(leftover)
        relay(client, up)
    except Exception:
        try:
            client.close()
        except Exception:
            pass


def serve(entry):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", entry["listen"]))
    srv.listen(64)
    print(f"[{entry.get('label','?')}] 127.0.0.1:{entry['listen']} -> socks "
          f"{entry['socks_host']}:{entry['socks_port']} -> {entry['upstream_host']}:{entry['upstream_port']}",
          flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, entry), daemon=True).start()


def _finish(entry):
    entry["socks_host"], _, port = entry["socks"].rpartition(":")
    entry["socks_port"] = int(port)
    entry["upstream_host"], _, uport = entry["upstream"].rpartition(":")
    entry["upstream_port"] = int(uport)
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8899)
    ap.add_argument("--socks", default="127.0.0.1:1080")
    ap.add_argument("--upstream", help="host:port")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--config", help="JSON：{\"socks\":..., \"entries\":[{label,listen,upstream,user,password}]}")
    a = ap.parse_args()

    if a.config:
        cfg = json.load(open(a.config, encoding="utf-8"))
        socks = cfg.get("socks") or "127.0.0.1:1080"
        entries = [_finish({"socks": socks, **e}) for e in cfg["entries"]]
    else:
        if not (a.upstream and a.user and a.password):
            ap.error("要么给 --config，要么给 --upstream/--user/--password")
        entries = [_finish({"label": "single", "listen": a.listen, "socks": a.socks,
                            "upstream": a.upstream, "user": a.user, "password": a.password})]

    for e in entries[1:]:
        threading.Thread(target=serve, args=(e,), daemon=True).start()
    serve(entries[0])


if __name__ == "__main__":
    main()
