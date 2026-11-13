#!/usr/bin/env python3
"""Record real Jellyfin responses as test fixtures, trimmed to the fields the bot reads.

Run inside the bot's container (it has JELLYFIN_URL and JELLYFIN_API_KEY), then
copy the folder back over tests/fixtures/jellyfin:

    python record_jellyfin.py /tmp/fixtures [jellyfin user]

Standard library only, so it runs anywhere. User names and ids are left out;
item ids and titles stay, since the tests need them. Re-recording changes the
values tests/test_recorded_jellyfin.py expects: update them to match.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ITEM_FIELDS = {"Id", "Name", "Type", "ProductionYear", "RunTimeTicks", "Genres", "ChildCount",
               "RecursiveItemCount", "Status", "SeriesName", "SeriesId", "ParentIndexNumber",
               "IndexNumber", "IndexNumberEnd", "DateCreated", "UserData"}
USER_FIELDS = {"PlaybackPositionTicks", "Played", "UnplayedItemCount", "PlayedPercentage"}
DETAIL = "Fields=Overview,Genres,ProductionYear,ChildCount,RecursiveItemCount"


def get(path: str):
    request = urllib.request.Request(os.environ["JELLYFIN_URL"].rstrip("/") + path, headers={
        "Authorization": f'MediaBrowser Token="{os.environ["JELLYFIN_API_KEY"]}"'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def trim(item: dict | None) -> dict | None:
    if item is None:
        return None
    out = {k: v for k, v in item.items() if k in ITEM_FIELDS}
    if "UserData" in out:
        out["UserData"] = {k: v for k, v in out["UserData"].items() if k in USER_FIELDS}
    if item.get("Overview"):
        out["Overview"] = "Overview trimmed from the recording."
    return out


def detail(item_id: str, user_id: str | None = None) -> dict:
    user = f"&userId={user_id}" if user_id else ""
    return get(f"/Items?Ids={item_id}&{DETAIL}{user}")["Items"][0]


def main(out_dir: str, user_name: str = "eddy"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    user_id = next(u["Id"] for u in get("/Users") if u["Name"].lower() == user_name.lower())

    def save(name: str, data):
        (out / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        print("recorded", name)

    resume = get(f"/UserItems/Resume?userId={user_id}&Limit=20")["Items"]
    film = next((i for i in resume if i["Type"] == "Movie"), None)
    if film:
        save("film_in_progress", {"item": trim(detail(film["Id"], user_id))})

    next_up = get(f"/Shows/NextUp?userId={user_id}&Limit=20&enableResumable=true")["Items"]
    half = next((e for e in next_up if (e.get("UserData") or {}).get("PlaybackPositionTicks")), None)
    fresh = next((e for e in next_up if not (e.get("UserData") or {}).get("PlaybackPositionTicks")), None)
    for name, episode in (("series_resume", half), ("series_up_next", fresh)):
        if episode:
            save(name, {"item": trim(detail(episode["SeriesId"], user_id)), "next": trim(episode)})

    watching = {e["SeriesId"] for e in next_up}
    shows = get("/Items?IncludeItemTypes=Series&Recursive=true&SortBy=SortName&Limit=50")["Items"]
    other = next((s for s in shows if s["Id"] not in watching), None)
    if other:
        count = get(f"/Items?ParentId={other['Id']}&IncludeItemTypes=Episode&Recursive=true&Limit=1")
        save("series_no_user", {"item": trim(detail(other["Id"])), "episode_count": count["TotalRecordCount"]})

    page = get("/Items?IncludeItemTypes=Episode&Recursive=true&SortBy=DateCreated&SortOrder=Descending"
               "&StartIndex=0&Limit=60&Fields=DateCreated&EnableImages=false&EnableUserData=false")
    save("recent_episodes", [trim(e) for e in page["Items"]])


if __name__ == "__main__":
    main(*sys.argv[1:])
