import gevent.monkey; gevent.monkey.patch_all()
import requests
import csv
import datetime
import os.path
import pickle
import grequests
from bs4 import BeautifulSoup
import json
import time
import math
import itertools


def scrape_map_pages(set_ids):
    # TODO: rewrite using info_dicts instead of set_ids
    """Scrape pages given by set_ids and return submitted dates.
    Makes requests asynchronously with grequests.
    https://github.com/ppy/osu-api/issues/195
    old.ppy.sh may be faster but gives less info about dates.
    """
    API_URL = "http://osu.ppy.sh/beatmapsets/"

    submitted_dates = []
    urls = [API_URL + str(set_id) for set_id in set_ids]
    rs = (grequests.get(u) for u in urls)
    for r in grequests.map(rs):
        assert r.status_code == 200  # set exists

        soup = BeautifulSoup(r.text, "html.parser")
        json_beatmapset = soup.find("script", id="json-beatmapset")
        submitted_date = json.loads(json_beatmapset.string)["submitted_date"]
        print(r.url, submitted_date)
        submitted_dates.append(submitted_date)

    assert len(submitted_dates) == len(set_ids)
    return submitted_dates


class Seen:
    '''Organize seen information to be dumped.'''
    def __init__(self):
        self.header_seen = False
        self.header_keys = None
        self.seen_beatmap_ids = set()
        self.set_id_dict = dict()  # (set_id: submitted_date) pairs
   
    

def download_map_info(api_key, outfile="maps.json", since_date_str="2007-10-07",
                      progress_file="maps_progress.pkl", scrape=True):
    """Main function to download and write data table from API (and scraping).
    Makes requests sequentially.
    Scraping mode adds what scrape_map_pages returns.
    If progress_file exists, tries to restart based on seen file (pickle)
    WARNING: Scraping is probably slow!
    Resume functionality appears stable, but no warranty(TM)
    """

    API_URL = "https://osu.ppy.sh/api/get_beatmaps"
    API_MAX_RESULTS = 500
    MYSQL_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

    # Load or init progress structures
    if os.path.isfile(progress_file):
        print("Loading progress file", progress_file)
        with open(progress_file, 'rb') as f:
            progress = pickle.load(f)


        since_date_str = progress["since"]  # Load since date
        print("Loaded since date", since_date_str)


    else:
        progress = dict()
        progress["json_list"] = []

    session = requests.Session()

    while True:
        payload = {"k": api_key, "since": since_date_str}
        r = session.get(API_URL, params=payload)

        info_dicts = r.json()
        assert type(info_dicts) == list
        if "error" in info_dicts:
            print(info_dicts)
            raise Exception("info_dict error")

        if not info_dicts: break  # Empty JSON, end of map search

        progress["json_list"].append(info_dicts)

        for info_dict in info_dicts:
            print("{} {} {} - {} [{}]".format(
                  info_dict["approved_date"],
                  info_dict["beatmap_id"],
                  info_dict["artist"],
                  info_dict["title"],
                  info_dict["version"]))


        if scrape:
            # Gather set_ids in info_dicts to batch request
            pass

        # Write out progress
        progress["since"] = since_date_str
        print("Writing progress to", progress_file)
        print("Maps:", len(list(itertools.chain(*progress["json_list"]))))
        with open(progress_file, 'wb') as f:
            pickle.dump(progress, f)

        # When the API returns 500 results, last mapset may have diffs cut off.
        # Therefore, the whole mapset needs to be read again. The API's "since"
        # parameter appears to be exclusive, so subtract 1 second to include
        # the map again.

        end_date_str = info_dicts[-1]["approved_date"]
        end_date = datetime.datetime.strptime(
            end_date_str, MYSQL_TIMESTAMP_FMT)

        if len(info_dicts) == API_MAX_RESULTS:
            end_date -= datetime.timedelta(seconds=1)
        since_date_str = end_date.strftime(MYSQL_TIMESTAMP_FMT)


    session.close()


    # Final write
    print("Writing final JSON", outfile)
    with open(outfile, 'w') as f:
        json.dump(progress["json_list"], f, indent=2)


def scrape_rankings(gamemode, country, max_page):
    '''Scrape a rankings page for user IDs.
    https://github.com/ppy/osu-api/issues/132 '''
    RANKINGS_URL = "https://old.ppy.sh/p/pp"

    user_ids = []

    session = requests.Session()

    for page in range(1, max_page+1):
        print("Scraping rankings page", page)
        payload = {'m': gamemode, "page": page}
        if country:
            payload['c'] = country

        r = session.get(RANKINGS_URL, params=payload)


        soup = BeautifulSoup(r.text, "html.parser")
        for href_tag in soup.find_all(href=True):
            link = href_tag['href']
            if link.startswith("/u"):
                user_ids.append(int(link.split("/u/")[1]))


    session.close()
    return user_ids


def exception_handler(request, exception):
    print(request, exception)


def download_rankings(api_key, outfile="rankings.json",
                      progress_file="rankings_progress.pkl",
                      gamemode=0, country=None,
                      top_scores=100, start_rank=0, end_rank=10000):
    '''Download top (100) scores of top (10k) users.
     Due to API rate limit, progress is stored.
     TODO: writing to progress file is slow so avoid doing too often.
     '''
    API_URL = "https://osu.ppy.sh/api/get_user_best"
    RANKS_PER_PAGE = 50
    BATCH_REQUESTS = 100
    BATCH_INTERVAL = 5  # seconds
    PROGRESS_FREQ = 10  # How often to save progress


    if os.path.exists(progress_file):
        print("Found progress file", progress_file)
        with open(progress_file, 'rb') as f:
            progress = pickle.load(f)
            start_rank = progress["start_rank"]
            print("Loaded start rank", start_rank)
            print("JSON list length", len(progress["json_list"]))


    else:
        progress = dict()  # Store progress to be dumped
        assert 0 <= start_rank < end_rank <= 10000

        max_page = math.ceil(end_rank / RANKS_PER_PAGE)
        progress["user_ids"] = \
            scrape_rankings(gamemode=gamemode, country=country,
                            max_page=max_page)

        progress["json_list"] = []

    progress_counter = 0
    for i in range(start_rank, end_rank, BATCH_REQUESTS):
        progress["start_rank"] = i  # Save start rank
        rs = []
        user_ids_range = progress["user_ids"][i:i+BATCH_REQUESTS]
        print(i, "User IDs", user_ids_range)
        for user_id in user_ids_range:
            payload = {'k': api_key, 'u': user_id, 'm': gamemode,
                       "limit": top_scores}
            rs.append(grequests.get(API_URL, params=payload))

        start_time = time.process_time()

        # Store downloaded JSON
        for r in grequests.map(rs, exception_handler=exception_handler):
            progress["json_list"].append(r.json())

        if progress_counter > 0 and progress_counter % PROGRESS_FREQ == 0:
            # Save progress
            print("Saving progress to", progress_file)
            with open(progress_file, 'wb') as f:
                pickle.dump(progress, f)

        progress_counter += 1

        # Don't exceed 1200 requests/min and make peppy angry
        # Dumb throttling
        time.sleep(max(0, start_time + BATCH_INTERVAL - time.process_time()))


    # Final write
    print("Writing JSON to", outfile)
    with open(outfile, 'w') as f:
        json.dump(progress["json_list"], f, indent=2)


def main():
    api_path = "api.key"
    API_KEY = open(api_path).read().strip()

    download_map_info(api_key=API_KEY, scrape=False)



if __name__ == "__main__":
    main()