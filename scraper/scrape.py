#!/usr/bin/env python
# vim: set fileencoding=utf-8 :

# Copyright (c) 2015 Code for Karlsruhe (http://codefor.de/karlsruhe)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
Scraper for renting costs in German Cities.
"""

from __future__ import division, unicode_literals

import cgi
import codecs
import contextlib
import errno
import functools
import json
import os
import pickle
import re
import sqlite3
import time
import urllib2

from bs4 import BeautifulSoup
from geopy.geocoders import Nominatim

HERE = os.path.abspath(os.path.dirname(__file__))

# Immobilienscout24 URLs for listings in Karlsruhe
with open(HERE + '/../config.json') as config_json:
    config = json.load(config_json)
    BASE_URL = config['base-url']
    PAGE_URL = config['page-url']
    CITY = config['city']

@contextlib.contextmanager
def prepare_database(filename):
    """
    Context manager that provides a database.
    """
    db = sqlite3.connect(filename)
    db.execute('''
        CREATE TABLE IF NOT EXISTS listings (
            id TEXT PRIMARY KEY,
            street TEXT,
            number TEXT,
            suburb TEXT,
            rent REAL,
            area REAL,
            latitude REAL,
            longitude REAL,
            date DATE DEFAULT CURRENT_TIMESTAMP
        ) WITHOUT ROWID;
    ''')
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def store_listings(db, listings):
    """
    Store listings in database.

    Listings already contained in the database are ignored.

    Returns the number of listings that were stored.
    """
    cursor = db.cursor()
    tuples = [(x, y['street'], y['number'], y['suburb'], y['rent'],
              y['area']) for x, y in listings.iteritems()]
    sql = '''INSERT OR IGNORE INTO listings (id, street, number, suburb, rent,
             area) VALUES (?, ?, ?, ?, ?, ?);'''
    cursor.executemany(sql, tuples)
    db.commit()
    return cursor.rowcount


def download_as_unicode(url):
    """
    Download document at URL and return it as a Unicode string.
    """
    request = urllib2.urlopen(url)
    return unicode(request.read(), request.headers.getparam('charset'))


def get_page(number):
    """
    Get a result page.

    The return value is a ``BeautifulSoup`` instance.
    """
    if number == 1:
        url = BASE_URL
    else:
        url = PAGE_URL % number
    data = download_as_unicode(url)
    return BeautifulSoup(data, 'html.parser')


def parse_german_float(s):
    """
    Parse a German float string.

    German uses a dot for the thousands separator and a comma for the
    decimal mark.
    """
    return float(s.replace('.', '').replace(',', '.'))


def parse_address(address):
    """
    Parse an address string into street, house number, and suburb.
    """
    fields = [s.strip() for s in address.split(', ')]
    if len(fields) == 2:
        street = None
        number = None
        suburb = fields[0]
    else:
        street, number = fields[0].rsplit(' ', 1)
        street = re.sub(r'([Ss])(trasse|tr.)\Z', r'\1traße', street)
        suburb = fields[1]
    return (street, number, suburb)


def extract_listings(soup):
    """
    Extract individual listings from a page.

    Returns a dict that maps listing IDs to listing details.
    """
    listings = {}
    no_addresses = 0
    for entry in soup.find_all('article', class_="result-list-entry"):
        for a in entry.find_all('a'):
            if a.get('href', '').startswith('/expose/'):
                listing_id = a.get('href').split('/')[-1]
                break
        else:
            # Couldn't find listing's ID
            continue
        street_span = entry.find('div', class_='result-list-entry__address').find('span')
        if not street_span:
            entry.find('div', class_='result-list-entry__address').find('a')
        try:
            street_span = street_span.contents[0]
        except:
            pass
        if not street_span:
            no_addresses += 1
            street_span = ''
            street, number, suburb = '', '', ''
        else:
            street, number, suburb = parse_address(unicode(street_span))
        for dl in entry.find_all('dl', class_='result-list-entry__primary-criterion'):
            dd = dl.find('dd')
            content = unicode(dd.string).strip()
            if content.endswith(' €'):
                rent = parse_german_float(content.split()[0])
            elif content.endswith(' m²'):
                area = parse_german_float(content.split()[0])
        listings[listing_id] = {
            'street': street,
            'number': number,
            'suburb': suburb,
            'rent': rent,
            'area': area,
        }
        print(listings)
    return (listings, no_addresses)


def extract_number_of_pages(soup):
    """
    Extract the number of result pages from a result page.
    """
    pager_options = soup.find(id="pageSelection").find_all('option')
    return int(pager_options[-1].string.split()[0])


def rate_limited(calls=1, seconds=1):
    """
    Decorator for rate limiting function calls.

    Makes sure that the decorated function is executed at most ``calls``
    times in ``seconds`` seconds. Calls to the decorated function which
    exceed this limit are delayed as necessary.
    """
    def decorator(f):
        last_calls = []

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            now = time.time()
            last_calls[:] = [x for x in last_calls if now - x <= seconds]
            if len(last_calls) >= calls:
                if calls == 1:
                    delta = last_calls[-1] + seconds - now
                else:
                    delta = last_calls[1] + seconds - now
                time.sleep(delta)
            last_calls.append(time.time())
            return f(*args, **kwargs)

        return wrapper
    return decorator


def memoize_persistently(filename):
    """
    Persistently memoize a function's return values.

    This decorator memoizes a function's return values persistently
    over multiple runs of the program. The return values are stored
    in the given file using ``pickle``. If the decorated function is
    called again with arguments that it has already been called with
    then the return value is retrieved from the cache and returned
    without calling the function. If the function is called with
    previously unseen arguments then its return value is added to the
    cache and the cache file is updated.

    Both return values and arguments of the function must support the
    pickle protocol. The arguments must also be usable as dictionary
    keys.
    """
    filename = os.path.join(HERE, filename)
    try:
        with open(filename, 'rb') as cache_file:
            cache = pickle.load(cache_file)
    except IOError as e:
        if e.errno != errno.ENOENT:
            raise
        cache = {}

    def decorator(f):

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            key = args + tuple(sorted(kwargs.items()))
            try:
                return cache[key]
            except KeyError:
                value = cache[key] = f(*args, **kwargs)
                with open(filename, 'wb') as cache_file:
                    pickle.dump(cache, cache_file)
                return value

        return wrapper
    return decorator


_geolocator = Nominatim()

@memoize_persistently('address_location_cache.pickle')
@rate_limited()
def get_coordinates(address, timeout=5):
    """
    Geolocate an address.

    Returns the latitude and longitude of the given address using
    OpenStreetMap's Nominatim service. If the coordinates of the
    address cannot be found then ``(None, None)`` is returned.

    As per Nominatim's terms of service this function is rate limited
    to at most one call per second.

    ``timeout`` gives the timeout in seconds.
    """
    location = _geolocator.geocode(address, timeout=timeout)
    if not location:
        return None, None
    return location.latitude, location.longitude


def dump_json(data, filename):
    """
    Dump data as JSON to file.
    """
    with codecs.open(filename, 'w', encoding='utf8') as f:
        json.dump(data, f, separators=(',', ':'))


def mkdirs(path):
    """
    Recursively create directories.

    Like ``os.makedirs``, but does not raise an error if the directory
    already exists.
    """
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


if __name__ == '__main__':
    import argparse
    import logging
    import logging.handlers
    import os.path
    import sys

    DB_FILE = os.path.join(HERE, 'listings.sqlite')
    EXPORT_DIR = os.path.join(HERE, 'export')

    parser = argparse.ArgumentParser(description='Rent scraper')
    parser.add_argument('--database', help='Database file', default=DB_FILE)
    parser.add_argument('--export-dir', help='Output directory for ' +
                        'exported data', default=EXPORT_DIR)
    parser.add_argument('--verbose', '-v', help='Output log to STDOUT',
                        default=False, action='store_true')
    args = parser.parse_args()
    args.database = os.path.abspath(args.database)
    args.export_dir = os.path.abspath(args.export_dir)
    marker_filename = os.path.join(args.export_dir, 'markers.json')
    data_filename = os.path.join(args.export_dir, 'listings.json')

    LOG_FILE = os.path.join(HERE, 'scrape.log')
    logger = logging.getLogger()
    formatter = logging.Formatter('[%(asctime)s] <%(levelname)s> %(message)s')
    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when='W0', backupCount=4, encoding='utf8')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    if args.verbose:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.info('Started')
    logger.info('Using database "%s"' % args.database)

    def get_new_listings(db):
        num_pages = None
        page_index = 1
        non_addr_index = 0
        new_count_index = 0
        while (not num_pages) or (page_index <= num_pages):
            logger.info("Fetching page %d" % page_index)
            page = get_page(page_index)
            num_pages = extract_number_of_pages(page)
            listings = extract_listings(page)
            new_count = store_listings(db, listings[0])
            logger.info("Extracted %d listings (%d new)" % (len(listings[0]),
                        new_count))
            logger.info("%d listings without addresses" % listings[1])
            
            non_addr_index += listings[1]
            page_index += 1
            new_count_index += new_count

        logger.info("Overall %d listings without addresses" % non_addr_index)
        logger.info("Overall %d new listings" % new_count_index)

    def add_coordinates(db):
        logger.info('Looking up address coordinates (this might take a while)')
        c = db.cursor()
        c.execute('''SELECT id, street, number, suburb FROM listings
                  WHERE (latitude IS NULL) AND (suburb NOT NULL) AND
                  (street NOT NULL) AND (number NOT NULL);''')
        updates = []
        for row in c:
            id, street, number, suburb = row
            address = '%s %s, %s, %s' % (street, number, suburb, CITY)
            coordinates = get_coordinates(address)
            if coordinates[0] is not None:
                updates.append((coordinates[0], coordinates[1], id))
        c.executemany('''UPDATE listings SET latitude=?, longitude=? WHERE
                      id=?;''', updates)
        db.commit()
        rowcount = max(0, c.rowcount)
        logger.info('Updated %d listings with coordinates' % rowcount)

    def export_markers_to_json(db, filename):
        """
        Export data points with known geolocation to JSON.
        """
        logger.info('Exporting marker data to JSON file "%s"' % filename)
        c = db.cursor()
        c.execute('''SELECT latitude, longitude, area, rent FROM listings
                     WHERE (latitude NOT NULL) AND (number NOT NULL);''')
        data = [(round(row[0], 5), round(row[1], 5), round(row[3] / row[2], 1))
                for row in c]
        dump_json(data, filename)


    def row_to_dict(row):
        """
        Convert a ``sqlite3.Row`` instance to a dictionary.
        """
        return {k: row[k] for k in row.keys()}


    def export_data_to_json(db, filename):
        """
        Export raw data to JSON.
        """
        logger.info('Exporting raw data to JSON file "%s".' % filename)
        c = db.cursor()
        c.execute('SELECT * FROM listings;')
        data = [row_to_dict(row) for row in c]
        dump_json(data, filename)

    try:
        with prepare_database(args.database) as db:
            get_new_listings(db)
            add_coordinates(db)
            mkdirs(args.export_dir)
            export_markers_to_json(db, marker_filename)
            export_data_to_json(db, data_filename)
    except Exception as e:
        logger.exception(e)

    logger.info('Finished')

