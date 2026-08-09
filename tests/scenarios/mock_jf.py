"""Mock Jellyfin serving a generated world (movies AND TV) from world.json."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

W = json.loads(open(sys.argv[2], encoding="utf-8").read())

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); p = u.path
        if p == "/System/Info":
            return self._send({"ServerName": "fuzz", "Version": "10.9.0", "Id": "fz"})
        if p == "/Users":
            return self._send([{"Id": "u1", "Name": "a"}, {"Id": "u2", "Name": "b"}])
        # Delete time re-asks the server for the season's real files, per series.
        if p.startswith("/Shows/") and p.endswith("/Episodes"):
            sid = p.split("/")[2]
            return self._send({"Items": [e for e in W["episodes"] if e["SeriesId"] == sid]})
        if p == "/Items" and "Ids" in q:
            want = set(q["Ids"][0].split(","))
            pool = W["series"] + W["movies"] + W["episodes"]
            return self._send({"Items": [i for i in pool if i["Id"] in want]})
        if "/Items" in p:
            inc = (q.get("IncludeItemTypes") or [""])[0]
            filt = (q.get("Filters") or [""])[0]
            uid = p.split("/")[2] if p.startswith("/Users/") else "u1"
            if "IsFavorite" in filt:
                favs = {d["Id"] for d in W["userdata"].get(uid, []) if d["UserData"]["IsFavorite"]}
                return self._send({"Items": [m for m in W["movies"] if m["Id"] in favs]})
            if "BoxSet" in inc: return self._send({"Items": []})
            # Season watch data. The app asks a user's PLAYED episodes for
            # SeriesName + ParentIndexNumber, which is how plays are attributed
            # to a season. Falling through to the movie list here meant every
            # season read as unplayed, the unplayed rule held the whole TV side
            # back, and TV watch scoring was never exercised at all.
            if "Episode" in inc and "IsPlayed" in filt:
                names = {s["Id"]: s["Name"] for s in W["series"]}
                ud = {d["Id"]: d["UserData"] for d in W["userdata"].get(uid, [])}
                out = []
                for e in W["episodes"]:
                    u = ud.get(e["Id"])
                    if not u or not u.get("PlayCount"):
                        continue
                    out.append({"Id": e["Id"], "Name": e["Name"],
                                "SeriesName": names.get(e["SeriesId"], ""),
                                "SeriesId": e["SeriesId"],
                                "ParentIndexNumber": e["ParentIndexNumber"],
                                "UserData": u})
                return self._send({"Items": out})
            if "Series" in inc: return self._send({"Items": W["series"]})
            if "Episode" in inc: return self._send({"Items": W["episodes"]})
            if p.startswith("/Users/") and (q.get("EnableUserData") or [""])[0] == "true":
                return self._send({"Items": W["userdata"].get(uid, [])})
            if "ParentId" in q: return self._send({"Items": []})
            return self._send({"Items": W["movies"]})
        self._send({})

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
