#! /usr/bin/env python

# This file is part of IVRE.
# Copyright 2011 - 2021 Pierre LALET <pierre@droids-corp.org>
#
# IVRE is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IVRE is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with IVRE. If not, see <http://www.gnu.org/licenses/>.

"""This sub-module contains functions to interact with another IVRE
instance via an HTTP server.

"""


from datetime import datetime
from functools import partial
from io import BytesIO
import json
import re
from urllib.parse import quote
from urllib.request import URLopener


try:
    import pycurl
except ImportError:
    HAS_CURL = False
else:
    HAS_CURL = True


from ivre.db import DB, DBActive, DBData, DBNmap, DBPassive, DBView
from ivre import utils


def serialize(obj):
    """Return a JSON-compatible representation for `obj`"""
    if isinstance(obj, utils.REGEXP_T):
        return {
            "f": "regexp",
            "a": [
                "/%s/%s"
                % (
                    obj.pattern,
                    "".join(x.lower() for x in "ILMSXU" if getattr(re, x) & obj.flags),
                ),
            ],
        }
    if isinstance(obj, datetime):
        return {"f": "datetime", "a": [obj.timestamp()]}
    if isinstance(obj, bytes):
        return {"f": "bytes", "a": [utils.encode_b64(obj).decode()]}
    raise TypeError("Don't know what to do with %r (%r)" % (obj, type(obj)))


class HttpFetcher:
    def __init__(self, url):
        self.baseurl = url._replace(fragment="").geturl()

    @staticmethod
    def from_url(url):
        if HAS_CURL and "@" in url.netloc:
            username, netloc = url.netloc.split("@", 1)
            if username == "GSSAPI":
                return HttpFetcherCurlGssapi(url._replace(netloc=netloc))
        return HttpFetcherBasic(url)


class HttpFetcherBasic(HttpFetcher):
    def __init__(self, url):
        super().__init__(url)
        self.urlop = URLopener()
        for hdr, val in (
            tuple(x.split("=", 1)) if "=" in x else (x, "")
            for x in url.fragment.split("&")
            if x
        ):
            self.urlop.addheader(hdr, val)

    def open(self, url):
        return self.urlop.open(url)


if HAS_CURL:

    class HttpFetcherCurl(HttpFetcher):
        def __init__(self, url):
            super().__init__(url)
            self.headers = [
                "%s: %s" % (tuple(x.split("=", 1)) if "=" in x else (x, ""))
                for x in url.fragment.split("&")
                if x
            ]

        def _set_opts(self, curl):
            curl.setopt(pycurl.HTTPHEADER, self.headers)

        def open(self, url):
            fdesc = BytesIO()
            curl = pycurl.Curl()
            curl.setopt(pycurl.URL, url)
            curl.setopt(pycurl.WRITEDATA, fdesc)
            self._set_opts(curl)
            curl.perform()
            status_code = curl.getinfo(pycurl.HTTP_CODE)
            if status_code != 200:
                raise Exception("HTTP Error %d" % status_code)
            fdesc.read = fdesc.getvalue
            return fdesc

    class HttpFetcherCurlGssapi(HttpFetcherCurl):
        def _set_opts(self, curl):
            super()._set_opts(curl)
            curl.setopt(pycurl.USERNAME, "")
            curl.setopt(pycurl.HTTPAUTH, pycurl.HTTPAUTH_GSSNEGOTIATE)


class HttpDB(DB):

    flt_empty = {}
    no_limit = None

    def __init__(self, url):
        super().__init__()
        self.db = HttpFetcher.from_url(url)

    @staticmethod
    def _output_filter(spec):
        return quote(
            json.dumps(spec, separators=(",", ":"), indent=None, default=serialize)
        )

    def _get(self, spec, limit=None, skip=None, sort=None, fields=None):
        url = "%s/%s?f=%s&q=skip:" % (
            self.db.baseurl,
            self.route,
            self._output_filter(spec),
        )
        if skip is None:
            skip = 0
        # TODO: sort
        while True:
            cururl = "%s%d" % (url, skip)
            if limit is not None:
                cururl += "%%20limit:%d" % limit
            req = self.db.open(cururl)
            data = json.loads(req.read().decode())
            if not data:
                break
            if limit is None:
                for rec in data:
                    yield rec
            else:
                for rec in data:
                    yield rec
                    limit -= 1
                    if limit == 0:
                        break
                if limit == 0:
                    break
            skip += len(data)

    def get(self, spec, limit=None, skip=None, sort=None, fields=None):
        for rec in self._get(spec, limit=limit, skip=skip, sort=sort, fields=fields):
            for fld in self.datetime_fields:
                if fld in rec:
                    rec[fld] = datetime.fromtimestamp(rec[fld])
            yield rec

    def distinct(self, field, flt=None, sort=None, limit=None, skip=None):
        url = "%s/%s/distinct/%s?f=%s&format=ndjson&q=limit:%d" % (
            self.db.baseurl,
            self.route,
            field,
            self._output_filter(flt or {}),
            limit or 0,
        )
        if skip is not None:
            url += "%20skip:%d" % skip
        # TODO: sort
        for line in self.db.open(url):
            yield json.loads(line)

    def count(self, spec, **kargs):
        url = "%s/%s/count?f=%s" % (
            self.db.baseurl,
            self.route,
            self._output_filter(spec),
        )
        req = self.db.open(url)
        return int(req.read().rstrip(b"\n"))

    def topvalues(
        self,
        field,
        flt=None,
        topnbr=10,
        sort=None,
        limit=None,
        skip=None,
        least=False,
    ):
        url = "%s/%s/top/%s%s:%d?f=%s" % (
            self.db.baseurl,
            self.route,
            "-" if least else "",
            quote(field),
            topnbr,
            self._output_filter(flt or self.flt_empty),
        )
        for param in ["sort", "limit", "skip"]:
            if locals()[param] is not None:
                raise ValueError(
                    "Parameter %s is not supported in HTTP backend" % param
                )

        def output(x):
            return {"_id": outputproc(x["label"]), "count": x["value"]}

        if (
            field
            in {
                "country",
                "city",
                "as",
                "port",
                "product",
                "version",
                "cpe",
                "ja3-server",
                "sshkey.bits",
                "ike.vendor_ids",
                "ike.transforms",
                "httphdr",
                "httpapp",
            }
            or any(
                field.startswith(x)
                for x in ["port:", "product:", "version:", "ja3-server:", "ja3-server."]
            )
            or (field.startswith("vulns.") and field != "vulns.id")
        ):

            def outputproc(x):
                return tuple(x)

        elif field.startswith("portlist:"):

            def outputproc(x):
                return [tuple(y) for y in x]

        else:

            def outputproc(x):
                return x

        req = self.db.open(url)
        return [output(elt) for elt in json.load(req)]

    @staticmethod
    def flt_and(*args):
        return {"f": "and", "a": list(a for a in args if a)}

    @classmethod
    def flt_or(cls, *args):
        return {"f": "or", "a": list(args)}

    @staticmethod
    def _search(func, *args, **kargs):
        return dict(
            f=func, **{"a": list(args)} if args else {}, **{"k": kargs} if kargs else {}
        )

    def __getattribute__(self, attr):
        if attr.startswith("search") and attr[6:]:
            return partial(self._search, attr[6:])
        return super().__getattribute__(attr)


class HttpDBActive(HttpDB, DBActive):

    pass


class HttpDBNmap(HttpDBActive, DBNmap):

    route = "scans"


class HttpDBView(HttpDBActive, DBView):

    route = "view"


class HttpDBPassive(HttpDB, DBPassive):

    route = "passive"


class HttpDBData(HttpDB, DBData):

    route = "ipdata"

    def infos_byip(self, addr):
        url = "%s/%s/%s" % (self.db.baseurl, self.route, addr)
        req = self.db.open(url)
        return {
            k: tuple(v) if isinstance(v, list) else v for k, v in json.load(req).items()
        }

    def _infos_byip(self, fields, addr):
        infos = self.infos_byip(addr)
        return {key: infos[key] for key in fields if key in infos}

    def as_byip(self, addr):
        return self._infos_byip(["as_num", "as_name"], addr)

    def location_byip(self, addr):
        return self._infos_byip(
            [
                "region_code",
                "region_name",
                "continent_code",
                "continent_name",
                "country_code",
                "country_name",
                "registered_country_code",
                "registered_country_name",
                "city",
                "postal_code",
                "coordinates",
                "coordinates_accuracy_radius",
            ],
            addr,
        )

    def country_byip(self, addr):
        return self._infos_byip(["country_code", "country_name"], addr)
