#!/usr/bin/env python3
"""Aggregate publications from RJF member journals into publications.json and feed.xml.

Each journal has its own fetcher. If a fetcher fails, the journal's entries from the
previous publications.json are kept, so one broken source never empties the list.
Uses only the Python standard library.
"""

import email.utils
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUBLICATIONS_FILE = ROOT / "publications.json"
FEED_FILE = ROOT / "feed.xml"
SITE_URL = "https://forrt.org/rjf/"
FEED_URL = SITE_URL + "feed.xml"
FEED_SIZE = 50
USER_AGENT = "RJF-publications-bot/1.0 (https://forrt.org/rjf/; mailto:lukas.wallrich@gmail.com)"

JOURNALS = {
    "jcre": "Journal of Comments and Replications in Economics",
    "jopd": "Journal of Open Psychology Data",
    "jrr": "Journal of Robustness Reports",
    "r2": "Replication Research",
    "rescience-c": "ReScience C",
    "rescience-x": "ReScience X",
}

OAI_NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/"}


def fetch(url, accept="*/*"):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def fetch_json(url):
    return json.loads(fetch(url, "application/json"))


def clean(text):
    """Strip markup and entities (Crossref titles can be double-escaped) and collapse whitespace."""
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def flip_name(name):
    """Turn 'Family, Given' into 'Given Family'."""
    if "," in name:
        family, given = (part.strip() for part in name.split(",", 1))
        return f"{given} {family}".strip()
    return name.strip()


def publication(journal, title, authors, date, doi=None, url=None, type_=None):
    doi = doi.lower() if doi else None
    return {
        "journal": journal,
        "title": clean(title),
        "authors": [clean(a) for a in authors if clean(a)],
        "date": date,
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else url,
        "type": type_,
    }


# --- OAI-PMH (OJS journals: Replication Research, ReScience X) ---------------------------

def oai_records(base_url, set_spec):
    set_names = {}
    sets = ET.fromstring(fetch(f"{base_url}?verb=ListSets"))
    for s in sets.iterfind(".//oai:set", OAI_NS):
        set_names[s.findtext("oai:setSpec", namespaces=OAI_NS)] = s.findtext("oai:setName", namespaces=OAI_NS)

    params = {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": set_spec}
    while True:
        root = ET.fromstring(fetch(f"{base_url}?{urllib.parse.urlencode(params)}"))
        for record in root.iterfind(".//oai:record", OAI_NS):
            header = record.find("oai:header", OAI_NS)
            if header.get("status") == "deleted":
                continue
            section = next((set_names.get(s.text) for s in header.iterfind("oai:setSpec", OAI_NS)
                            if s.text.startswith(set_spec + ":")), None)
            yield record.find(".//{http://www.openarchives.org/OAI/2.0/oai_dc/}dc"), section
        token = root.findtext(".//oai:resumptionToken", namespaces=OAI_NS)
        if not token:
            break
        params = {"verb": "ListRecords", "resumptionToken": token}


def fetch_oai_journal(journal, base_url, set_spec):
    pubs = []
    for dc, section in oai_records(base_url, set_spec):
        identifiers = [i.text.strip() for i in dc.iterfind("dc:identifier", OAI_NS) if i.text]
        doi = next((i for i in identifiers if i.startswith("10.")), None)
        url = next((i for i in identifiers if i.startswith("http")), None)
        pubs.append(publication(
            journal,
            dc.findtext("dc:title", namespaces=OAI_NS),
            [flip_name(c.text) for c in dc.iterfind("dc:creator", OAI_NS) if c.text],
            dc.findtext("dc:date", namespaces=OAI_NS)[:10],
            doi, url, section,
        ))
    return pubs


def fetch_r2():
    return fetch_oai_journal("r2", "https://ejournals.uni-muenster.de/index.php/replicationresearch/oai",
                             "replicationresearch")


def fetch_rescience_x():
    return fetch_oai_journal("rescience-x", "http://rescience.org/x/oai", "x")


# --- ReScience C (BibTeX file that generates rescience.github.io/read) ---------------------

LATEX_ACCENTS = {"'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303", "v": "\u030c", "c": "\u0327"}


def latex_to_text(text):
    text = re.sub(r"\[~?Re\]", "[Re]", text)
    text = re.sub(r"\\([v'`^\"~c])\{?\\?([A-Za-z])\}?",
                  lambda m: m.group(2) + LATEX_ACCENTS[m.group(1)], text)
    text = re.sub(r"\$_(\w)\$", r"\1", text)
    text = text.replace("~", " ").replace("{", "").replace("}", "").replace("\\", "")
    return unicodedata.normalize("NFC", text)


def parse_bibtex(source):
    entries = []
    for match in re.finditer(r"@\w+\s*\{\s*[^,]+,", source):
        position, depth, fields = match.end(), 1, {}
        while depth > 0 and position < len(source):
            field = re.compile(r"\s*(\w+)\s*=\s*").match(source, position)
            if not field:
                if source[position] == "}":
                    depth = 0
                position += 1
                continue
            position = field.end()
            if source[position] == "{":
                level, start = 0, position
                while True:
                    level += {"{": 1, "}": -1}.get(source[position], 0)
                    position += 1
                    if level == 0:
                        break
                value = source[start + 1:position - 1]
            else:
                value = re.compile(r"[^,}\n]*").match(source, position).group(0)
                position += len(value)
            fields[field.group(1).lower()] = value.strip()
        entries.append(fields)
    return entries


def fetch_rescience_c():
    source = fetch("https://raw.githubusercontent.com/rescience/rescience.github.io/sources/_bibliography/published.bib")
    pubs = []
    for entry in parse_bibtex(source):
        date = entry["year"]
        if entry.get("month", "").isdigit():
            date += f"-{int(entry['month']):02d}"
        authors = [latex_to_text(a) for a in re.split(r"\s+and\s+", entry.get("author", ""))]
        pubs.append(publication("rescience-c", latex_to_text(entry["title"]), authors, date,
                                entry.get("doi"), entry.get("url"), entry.get("type")))
    return pubs


# --- Journal of Comments and Replications in Economics (DataCite) -------------------------

def fetch_jcre():
    url = "https://api.datacite.org/dois?prefix=10.18718&query=id:10.18718%2F81781*&page[size]=1000"
    pubs = []
    for item in fetch_json(url)["data"]:
        attributes = item["attributes"]
        # Volume-level records (resourceTypeGeneral "Collection") are not articles
        if not re.fullmatch(r"10\.18718/81781\.\d+", item["id"]) or attributes["types"].get("resourceTypeGeneral") == "Collection":
            continue
        year = str(attributes["publicationYear"])
        registered = (attributes.get("registered") or "")[:10]
        # DataCite only stores the year of issue; the registration date is a close proxy when it matches
        date = registered if registered.startswith(year) else year
        authors = [c.get("givenName", "") + " " + c.get("familyName", "") if c.get("familyName") else flip_name(c["name"])
                   for c in attributes["creators"]]
        pubs.append(publication("jcre", attributes["titles"][0]["title"], authors, date, item["id"], attributes.get("url")))
    return pubs


# --- Crossref journals (JRR, JOPD) ----------------------------------------------------------

def crossref_works(issn):
    cursor, items = "*", []
    while True:
        query = urllib.parse.urlencode({"rows": 1000, "cursor": cursor, "select": "DOI,title,author,issued,type,URL"})
        message = fetch_json(f"https://api.crossref.org/journals/{issn}/works?{query}")["message"]
        items += message["items"]
        if len(message["items"]) < 1000:
            return [i for i in items if i.get("type") == "journal-article"]
        cursor = message["next-cursor"]


def crossref_publication(journal, item, type_=None):
    parts = item["issued"]["date-parts"][0]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    authors = [f"{a.get('given', '')} {a.get('family', '')}".strip() or a.get("name", "") for a in item.get("author", [])]
    return publication(journal, (item.get("title") or [""])[0], authors, date, item["DOI"], item.get("URL"), type_)


def fetch_jrr():
    return [crossref_publication("jrr", item) for item in crossref_works("3051-3200")]


def fetch_jopd(previous):
    """Only JOPD's verification reports belong to the RJF. Crossref has no section data, so the
    section is read from each article page once and cached in publications.json."""
    sections = dict(previous.get("jopd_sections", {}))
    pubs = []
    for item in crossref_works("2050-9863"):
        doi = item["DOI"].lower()
        if doi not in sections:
            page = fetch(f"https://openpsychologydata.metajnl.com/articles/{doi}")
            match = re.search(r'\\?"section\\?":\\?"([^"\\]+)', page)
            if not match:
                raise ValueError(f"No section found on JOPD article page for {doi}")
            sections[doi] = match.group(1)
        if "verification" in sections[doi].lower():
            pubs.append(crossref_publication("jopd", item, "Verification Report"))
    return pubs, sections


# --- Output ---------------------------------------------------------------------------------

def rfc822(date):
    parts = [int(p) for p in date.split("-")] + [1, 1]
    return email.utils.format_datetime(datetime(parts[0], parts[1], parts[2], 12, tzinfo=timezone.utc))


def write_feed(pubs):
    rss = ET.Element("rss", version="2.0", attrib={"xmlns:atom": "http://www.w3.org/2005/Atom"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Replication Journal Federation – New Publications"
    ET.SubElement(channel, "link").text = SITE_URL
    ET.SubElement(channel, "description").text = "New articles from the member journals of the Replication Journal Federation."
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "atom:link", href=FEED_URL, rel="self", type="application/rss+xml")
    if pubs:
        ET.SubElement(channel, "lastBuildDate").text = rfc822(pubs[0]["date"])
    for pub in pubs[:FEED_SIZE]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = pub["title"]
        ET.SubElement(item, "link").text = pub["url"]
        ET.SubElement(item, "guid", isPermaLink="false").text = pub["doi"] or pub["url"]
        ET.SubElement(item, "pubDate").text = rfc822(pub["date"])
        ET.SubElement(item, "category").text = JOURNALS[pub["journal"]]
        ET.SubElement(item, "description").text = " · ".join(
            filter(None, [", ".join(pub["authors"]), JOURNALS[pub["journal"]], pub["type"]]))
    ET.indent(rss)
    FEED_FILE.write_text('<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n")


def main():
    previous = json.loads(PUBLICATIONS_FILE.read_text()) if PUBLICATIONS_FILE.exists() else {}
    previous_pubs = previous.get("publications", [])
    fetchers = {
        "jcre": fetch_jcre,
        "jrr": fetch_jrr,
        "r2": fetch_r2,
        "rescience-c": fetch_rescience_c,
        "rescience-x": fetch_rescience_x,
    }
    pubs, failures = [], []
    jopd_sections = previous.get("jopd_sections", {})
    for journal in JOURNALS:
        try:
            if journal == "jopd":
                result, jopd_sections = fetch_jopd(previous)
            else:
                result = fetchers[journal]()
            print(f"{journal}: {len(result)} publications")
            pubs += result
        except Exception as error:
            kept = [p for p in previous_pubs if p["journal"] == journal]
            print(f"::warning::{journal} failed ({error!r}); keeping {len(kept)} previous entries")
            failures.append(journal)
            pubs += kept

    seen, unique = set(), []
    for pub in pubs:
        key = pub["doi"] or pub["url"]
        if key not in seen:
            seen.add(key)
            unique.append(pub)
    unique.sort(key=lambda p: (p["date"], p["title"]), reverse=True)

    PUBLICATIONS_FILE.write_text(json.dumps({
        "journals": JOURNALS,
        "publications": unique,
        "jopd_sections": dict(sorted(jopd_sections.items())),
    }, ensure_ascii=False, indent=1) + "\n")
    write_feed(unique)
    print(f"Wrote {len(unique)} publications")
    if len(failures) == len(JOURNALS):
        sys.exit("All sources failed")


if __name__ == "__main__":
    main()
