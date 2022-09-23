from __future__ import annotations

import copy
import logging
import os
import time
from dataclasses import dataclass, field
from pprint import pprint
from typing import TypedDict, Literal, Any, Optional, cast

import pylast
import urllib3
from requests import Session

urllib3.disable_warnings()

REMOTE_PLAYER_NAME = "Salon (DLNA)"
DSM_HOSTNAME = os.getenv("DSM_HOSTNAME")

SIMILAR_ARTISTS: dict[str, set[str]] = {}


class MandatorySynoVersion(TypedDict):
    minVersion: int
    maxVersion: int
    path: str


class OptionalSynoVersion(TypedDict):
    requestFormat: Literal["JSON"]


class SynoVersion(MandatorySynoVersion, OptionalSynoVersion):
    """Item returned by SYNO.API.Info"""


class RemotePlayerStatus(TypedDict):
    index: int
    play_mode: PlayMode
    playlist_timestamp: int
    playlist_total: int
    position: int
    song: Song
    state: Literal["playing", "paused"]
    volume: int


class PlayMode(TypedDict):
    pass


class Song(TypedDict):
    additional: SongAdditionals
    id: str
    path: str
    title: str
    type: Literal["file", "radio"]


class SongAdditionals(TypedDict, total=False):
    song_tag: SongTag
    song_rating: float


class SongTag(TypedDict):
    album: str
    album_artist: str
    artist: str
    comment: str
    composer: str
    disc: int
    genre: str
    track: int
    year: int


class NowPlaying(SongTag):
    title: str


def get_similar(
    network: pylast.LastFMNetwork, artist: str, limit: int = 30
) -> list[str]:
    logging.info(f"Looking for similar artists to {artist}")
    try:
        artist = network.get_artist(artist)
        return [x.item.name for x in artist.get_similar(limit=limit)]
    except pylast.WSError:
        logging.info(f"Artist {artist} not found")
        return []


class RemotePlayerError(RuntimeError):
    pass


@dataclass
class AudioStationRemote:
    hostname: str
    port: str
    account: str
    password: str
    remote_player: str
    verify: bool = False
    last_fm_network: Optional[pylast.LastFMNetwork] = None

    version: int = field(default=1, init=False)
    endpoint: str = field(init=False)
    player_id: str = field(init=False)

    versions: dict[str, SynoVersion] = field(default_factory=dict, init=False)
    session: Session = field(init=False)

    def __post_init__(self):
        self.endpoint = f"{self.hostname}:{self.port}"
        self.session = Session()

        response = self.query_syno_api_info()
        self.versions = {k: v for k, v in response["data"].items() if "Audio" in k}

        self.login()
        self.get_remote_player_id()

    def request(
        self, verb: Literal["post", "get"], path: str, data: Optional[str] = None
    ) -> Any:
        full_path = f"https://{self.endpoint}/{path}"
        logging.debug("Requesting %s", full_path)
        logging.debug("Data: %s", data)

        request = self.session.request(
            verb, full_path, data=data.encode() if data is not None else None, verify=self.verify
        )
        if error_message := request.json().get("error"):
            raise RemotePlayerError(error_message)
        else:
            return request

    def __del__(self):
        self.session.close()

    def query_syno_api_info(self):
        return self.request(
            "get", "webapi/entry.cgi?api=SYNO.API.Info&version=1&method=query"
        ).json()

    def login(self):
        """Set cookies."""
        return self.request(
            "get",
            f'webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=login&account={self.account}&passwd={self.password}&session=AudioStation&format=cookie',
        ).json()

    def list_remote_players(self):
        return self.request(
            "post",
            f"webapi/AudioStation/remote_player.cgi",
            data="api=SYNO.AudioStation.RemotePlayer&version=3&method=list",
        ).json()

    def get_remote_player_id(self):
        self.player_id = next(
            x["id"]
            for x in self.list_remote_players()["data"]["players"]
            if x["name"] == REMOTE_PLAYER_NAME
        )
        return self.player_id

    def get_remote_player_status(self) -> RemotePlayerStatus:
        return (
            self.request(
                "post",
                f"webapi/AudioStation/remote_player.cgi",
                data=f"api=SYNO.AudioStation.RemotePlayerStatus&version=1&method=getstatus&id={self.player_id}&additional=song_tag",
            )
            .json()
            .get("data", {})
        )

    def get_now_playing(self) -> Optional[NowPlaying]:
        info = self.get_remote_player_status()
        if info["state"] != "playing":
            return None

        song = info["song"]["additional"].get("song_tag")
        if not song:
            raise RemotePlayerError("No info found")
        return cast(NowPlaying, {"title": info["song"]["title"], **song})

    def search_for_artist(self, artist: str) -> dict[str, Any]:
        logging.debug("Searching for artist %s", artist)
        try:
            return (
                self.request(
                    "post",
                    "webapi/AudioStation/artist.cgi",
                    data=f"api=SYNO.AudioStation.Artist&version=4&method=list&filter={artist}&library=all&limit=10&offset=0&additional=avg_rating",
                )
                .json()
                .get("data", {})
            )
        except UnicodeEncodeError:
            return {}

    def get_similar_artists(self, artist: str, limit: int = 30) -> set[str]:
        if not self.last_fm_network:
            return set()

        if similar_artists_set := SIMILAR_ARTISTS.get(artist, set()):
            return similar_artists_set

        similar_artists = get_similar(self.last_fm_network, artist, limit)

        for artist_ in similar_artists:
            for similar_artist in self.search_for_artist(artist_).get("artists", []):
                if set(artist_.replace(",", " ").split(" ")) & set(
                    similar_artist["name"].replace(",", " ").split(" ")
                ):
                    similar_artists_set.add(similar_artist["name"])

        SIMILAR_ARTISTS[artist] = similar_artists_set

        return similar_artists_set


def setup():
    network = pylast.LastFMNetwork(
        api_key=os.getenv("API_KEY"),
        api_secret=os.getenv("API_SECRET"),
        username=os.getenv("SCROBBLE_USERNAME"),
        password_hash=pylast.md5(os.getenv("SCROBBLE_PASSWORD")),
    )

    remote = AudioStationRemote(
        DSM_HOSTNAME,
        5001,
        os.getenv("USERNAME"),
        os.getenv("PASSWORD"),
        REMOTE_PLAYER_NAME,
        last_fm_network=network,
    )

    return network, remote


def main():
    logging.basicConfig(level=logging.INFO)

    network: pylast.LastFMNetwork = None
    remote: AudioStationRemote = None
    current_track: dict = {}

    while True:
        if not (network or remote):
            network, remote = setup()

        try:
            info = remote.get_now_playing()
            if info and current_track != info:
                logging.info(
                    f"Now playing {info['title']} from artist {info['artist']} in album {info['album']}"
                )
                network.update_now_playing(
                    info["artist"],
                    info["title"],
                    info["album"],
                    info["album_artist"],
                    track_number=info["track"],
                )

                network.scrobble(
                    artist=info["artist"],
                    timestamp=time.time(),
                    title=info["title"],
                    album=info["album"],
                    album_artist=info["album_artist"],
                    track_number=info["track"],
                )
                current_track = copy.copy(info)

                similar_artists = remote.get_similar_artists(info["artist"])
                if similar_artists:
                    logging.info(f"Similar to {', '.join(similar_artists)}")
                else:
                    logging.info("Nothing similar")
        except pylast.NetworkError:
            logging.info("Reconnect")
            time.sleep(10)

        time.sleep(5)

    return

    # pprint(response.json())
    #
    # print("Get playlist")
    # response = session.post(
    #     f"https://{DSM_HOSTNAME}:5001/webapi/AudioStation/remote_player.cgi",
    #     data=f'api=SYNO.AudioStation.RemotePlayer&version=3&method=getplaylist&id={player["id"]}')
    # pprint(response.json())

    # print(f"Get info of song {song['id']}")
    # response = session.post(
    #     f"https://{DSM_HOSTNAME}:5001/webapi/AudioStation/song.cgi",
    #     data=f"api=SYNO.AudioStation.Song&version=2&method=getinfo&id={song['id']}&library=all&limit=1024&offset=0&additional=song_tag,song_audio,song_rating")
    # pprint(response.json())


"""

Step 3; queue stream:
wget -qO - --load-cookies cookies.txt --post-data "api=SYNO.AudioStation.RemotePlayer&method=updateplaylist&library=shared&id=[homepod]&offset=0&limit=1&play=true&version=3&songs=radio_[name] [url]&updated_index=-1" https://[fqdn]:5001/webapi/AudioStation/remote_player.cgi

Step 4; play stream:
wget -qO - --load-cookies cookies.txt --post-data "api=SYNO.AudioStation.RemotePlayer&method=control&id=[homepod]&version=2&action=play&value=0" https://[fqdn]:5001/webapi/AudioStation/remote_player.cgi

To pause the stream:
wget -qO - --load-cookies cookies.txt --post-data "api=SYNO.AudioStation.RemotePlayer&method=control&action=pause&id=[homepod]&version=3" https://[fqdn]:5001/webapi/AudioStation/remote_player.cgi

To stop the stream:
wget -qO - --load-cookies cookies.txt --post-data "api=SYNO.AudioStation.RemotePlayer&method=control&action=stop&id=[homepod]&version=3" https://[fqdn]:5001/webapi/AudioStation/remote_player.cgi
"""

if __name__ == "__main__":
    main()

