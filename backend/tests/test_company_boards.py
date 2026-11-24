"""Company boards read as their vendors publish them: locations, remote flags, pages, types.

Every payload is synthetic and mirrors the public shape of its vendor's answer.
"""

import html
import json

import httpx
import pytest

from discovery_fixtures import mock_http
from openings.collectors import company
from openings.collectors.policy import ProviderDeferred, ProviderPatch, provider_for_url
from openings.discovery import Filters, SourceCreate, feeds
from openings.discovery.network import MAX_FEED_BYTES, MAX_LISTING_BYTES
from openings.discovery.resolution import detect_source
from openings.errors import DomainError

TEXT = "<p>Design, build and operate the synthetic platform services of Example.</p>"


def collect(kind, answers, *, board="example", locations=(), **config):
    """Collect ``kind`` with ``answers``: the first (url fragment, payload) a URL contains."""
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        for fragment, payload in answers:
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise AssertionError(url)

    source = {
        "name": "Example",
        "kind": kind,
        "config": {
            "board": board,
            **config,
            "filters": Filters(locations=list(locations)).model_dump(),
        },
    }
    items = company.collect(source, request, feeds.html_text)
    return [feeds.normalize_posting(item) for item in items], source, calls


def run(service, source):
    service.queue_collection(source["id"])
    task = service.claim_task("test", kind="collect")
    service.run_task(task, "test")
    return service.get_task(task["id"])


# #298 -----------------------------------------------------------------------


def test_a_remote_role_in_a_city_passes_a_remote_filter(monkeypatch):
    rows, _, _ = collect(
        "workable",
        [
            (
                "widget/accounts",
                {
                    "jobs": [
                        {
                            "shortcode": "AB12",
                            "title": "Machine Learning Engineer",
                            "telecommuting": True,
                            "city": "Paris",
                            "state": "Île-de-France",
                            "country": "France",
                            "url": "https://apply.workable.com/j/AB12",
                            "description": TEXT,
                        },
                        {
                            "shortcode": "CD34",
                            "title": "Office Manager",
                            "telecommuting": False,
                            "city": "Paris",
                            "country": "France",
                            "description": TEXT,
                        },
                    ]
                },
            )
        ],
        locations=["Remote"],
    )
    assert [row["metadata"]["external_id"] for row in rows] == ["AB12"]
    assert rows[0]["metadata"]["is_remote"] is True

    # Lever states it as a workplace type, read once the posting is normalized.
    lever = [
        {
            "id": "1",
            "text": "Engineer",
            "workplaceType": "remote",
            "categories": {"location": "Berlin"},
        },
        {
            "id": "2",
            "text": "Engineer",
            "workplaceType": "onsite",
            "categories": {"location": "Berlin"},
        },
    ]
    monkeypatch.setattr(feeds, "_json", lambda url, *names: lever)
    source = {
        "kind": "lever",
        "name": "Example",
        "config": {"board": "example", "filters": Filters(locations=["Remote"]).model_dump()},
    }
    assert [item["metadata"]["external_id"] for item in feeds.fetch_postings(source)] == ["1"]


# #299 -----------------------------------------------------------------------


def test_greenhouse_reads_the_cities_of_a_hybrid_role_from_its_offices():
    offices = {
        "offices": [
            {
                "id": 1,
                "name": "EMEA",
                "location": None,
                "parent_id": None,
                "child_ids": [2],
                "departments": [],
            },
            {
                "id": 2,
                "name": "London",
                "location": "London, United Kingdom",
                "parent_id": 1,
                "child_ids": [],
                "departments": [{"id": 7, "name": "Engineering", "jobs": [{"id": 101}]}],
            },
        ]
    }
    rows, _, calls = collect(
        "greenhouse",
        [
            (
                "/jobs/101",
                {"content": TEXT, "departments": [{"id": 7, "name": "Engineering"}]},
            ),
            (
                "/jobs",
                {
                    "jobs": [
                        {
                            "id": 101,
                            "title": "Engineer",
                            "location": {"name": "Hybrid"},
                            "absolute_url": "https://boards.greenhouse.io/example/jobs/101",
                            "first_published": "2026-03-09T16:05:19-04:00",
                        },
                        {"id": 102, "title": "Engineer", "location": {"name": "Hybrid"}},
                    ]
                },
            ),
            ("/offices", offices),
        ],
        locations=["London"],
    )
    assert len(rows) == 1 and rows[0]["location"] == "Hybrid · London, United Kingdom"
    assert rows[0]["metadata"]["departments"] == ["Engineering"]
    assert rows[0]["metadata"]["date_posted"] == "2026-03-09"
    # One offices request a run; the unmatched role costs no detail request.
    assert sum(url.endswith("/offices") for url in calls) == 1
    assert not any(url.endswith("/102") for url in calls)


# #300 -----------------------------------------------------------------------

PERSONIO = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>7001</id>
    <office>Berlin</office>
    <additionalOffices><office>Hamburg</office><office>Remote</office></additionalOffices>
    <name>Werkstudent Marketing (m/w/d)</name>
    <jobDescriptions>
      <jobDescription><name>Deine Aufgaben</name><value>&lt;p&gt;Du planst Kampagnen.&lt;/p&gt;</value></jobDescription>
    </jobDescriptions>
    <employmentType>working_student</employmentType>
    <schedule>part-time</schedule>
    <createdAt>2026-09-17T07:57:12+00:00</createdAt>
  </position>
  <position>
    <id>7002</id>
    <office>Munich</office>
    <name>Praktikum Data (m/w/d)</name>
    <jobDescriptions>
      <jobDescription><name>Aufgaben</name><value>Daten auswerten.</value></jobDescription>
    </jobDescriptions>
    <employmentType>intern</employmentType>
    <schedule>full-time</schedule>
  </position>
</workzag-jobs>
"""


def test_personio_reads_german_text_other_offices_and_the_com_host():
    rows, _, calls = collect("personio", [("/xml", PERSONIO)], locations=["Hamburg"])
    # Without ?language=en: a position written only in German keeps its text.
    assert calls == ["https://example.jobs.personio.de/xml"]
    assert len(rows) == 1 and "Du planst Kampagnen." in rows[0]["posting"]
    assert rows[0]["location"] == "Berlin · Hamburg · Remote"
    assert rows[0]["metadata"]["job_type"] == "parttime"
    assert rows[0]["metadata"]["date_posted"] == "2026-09-17"
    rows, _, _ = collect("personio", [("/xml", PERSONIO)])
    assert rows[1]["metadata"]["job_type"] == "internship"
    assert rows[1]["metadata"]["job_types"] == ["internship", "fulltime"]
    assert rows[1]["metadata"]["job_type_raw"] == ["intern", "full-time"]

    detected = detect_source("https://example.jobs.personio.com/job/7001")
    assert (detected["kind"], detected["config"]["board"]) == ("personio", "example")
    assert provider_for_url("https://example.jobs.personio.com/xml") == "personio"


# #301 -----------------------------------------------------------------------


def smartrecruiters_row(number, **place):
    return {
        "id": str(number),
        "name": f"Engineer {number}",
        "releasedDate": "2026-11-15T12:18:34.522Z",
        "location": {"city": "Stuttgart", "region": "Baden-Württemberg", "country": "de", **place},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
    }


def test_smartrecruiters_matches_country_names_and_knows_its_total():
    listing = {
        "offset": 0,
        "limit": 100,
        "totalFound": 2,
        "content": [
            smartrecruiters_row(
                1, fullLocation="Stuttgart, Baden-Württemberg, Germany", remote=False, hybrid=True
            ),
            smartrecruiters_row(
                2, city="Lyon", country="fr", fullLocation="Lyon, France", remote=True
            ),
        ],
    }
    detail = {"jobAd": {"sections": {"jobDescription": {"text": TEXT}}}}
    rows, _, _ = collect(
        "smartrecruiters", [("postings?", listing), ("postings/", detail)], locations=["Germany"]
    )
    assert [row["location"] for row in rows] == ["Stuttgart, Baden-Württemberg, Germany"]
    assert rows[0]["metadata"]["workplace_type"] == "hybrid"
    assert rows[0]["metadata"]["job_type"] == "fulltime"
    rows, _, _ = collect(
        "smartrecruiters", [("postings?", listing), ("postings/", detail)], locations=["Remote"]
    )
    assert [row["metadata"]["external_id"] for row in rows] == ["2"]

    # Exactly max_candidates roles: the stated total says the board was read whole.
    pages = {
        offset: {
            "totalFound": 200,
            "content": [smartrecruiters_row(offset + number) for number in range(100)],
        }
        for offset in (0, 100)
    }
    _, source, calls = collect(
        "smartrecruiters",
        [("offset=0", pages[0]), ("offset=100", pages[100]), ("postings/", detail)],
        max_results=1,
        max_candidates=200,
    )
    assert not source.get("candidate_capped")
    assert sum("offset=" in url for url in calls) == 2


# #302 -----------------------------------------------------------------------


def join_page(state):
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"pageData": state}}})
        + "</script>"
    ).encode()


def join_job(number):
    return {
        "id": number,
        "idParam": f"{number}-engineer",
        "title": f"Engineer {number}",
        "city": {"cityName": "Berlin", "countryName": "Germany"},
        "createdAt": "2026-05-04T14:49:10.217Z",
        "employmentType": {"name": "Employee"},
        "workplaceType": "HYBRID",
    }


def test_joincom_reads_every_page_once_and_keeps_the_listing_location():
    pagination = {"pageCount": 2, "pageSize": 5, "total": 6}
    first = {"jobs": {"items": [join_job(n) for n in range(1, 6)], "pagination": pagination}}
    # Pages overlap: job 5 is listed again on page two.
    second = {"jobs": {"items": [join_job(5), join_job(6)], "pagination": pagination}}
    detail = {
        "job": {
            "description": TEXT,
            "city": {"cityName": "Berlin", "countryName": "Deutschland"},
            "employmentType": {"googleType": "FULL_TIME", "name": "Angestellte/r"},
        }
    }
    rows, source, calls = collect(
        "joincom",
        [("?page=2", join_page(second)), ("-engineer", join_page(detail)), ("", join_page(first))],
        locations=["Germany"],
    )
    assert sorted(row["metadata"]["external_id"] for row in rows) == [str(n) for n in range(1, 7)]
    assert {row["location"] for row in rows} == {"Berlin, Germany"}
    assert rows[0]["metadata"]["job_type"] == "fulltime"
    assert rows[0]["metadata"]["workplace_type"] == "hybrid"
    assert rows[0]["metadata"]["date_posted"] == "2026-05-04"
    assert not source.get("candidate_capped")
    assert calls.count("https://join.com/companies/example?page=2") == 1


# #303 -----------------------------------------------------------------------


def test_rippling_gives_one_item_per_role_with_every_location():
    listing = [
        {
            "uuid": "a1",
            "name": "Account Executive",
            "url": "https://ats.rippling.com/example/jobs/a1",
            "workLocation": {"label": "Boston, MA", "id": "Boston, MA"},
        },
        {
            "uuid": "a1",
            "name": "Account Executive",
            "url": "https://ats.rippling.com/example/jobs/a1",
            "workLocation": {"label": "Austin, TX", "id": "Austin, TX"},
        },
        {"uuid": "b2", "name": "Analyst", "workLocation": {"label": "Remote", "id": "Remote"}},
    ]
    detail = {
        "description": {"role": TEXT},
        "employmentType": {"label": "SALARIED_FT", "id": "Salaried, full-time"},
        "createdOn": "2026-10-06T16:36:07.947000-07:00",
    }
    rows, _, calls = collect("rippling", [("/jobs/", detail), ("/jobs", listing)])
    assert [(row["source_key"], row["location"]) for row in rows] == [
        ("rippling:example:a1", "Boston, MA · Austin, TX"),
        ("rippling:example:b2", "Remote"),
    ]
    assert rows[0]["metadata"]["job_type"] == "fulltime"
    assert len(calls) == 3


# #304 -----------------------------------------------------------------------


def oracle_listing(rows, total):
    return {"items": [{"TotalJobsCount": total, "requisitionList": rows}]}


def oracle_row(number, *secondary):
    return {
        "Id": str(number),
        "Title": f"Engineer {number}",
        "PrimaryLocation": "New York, NY, United States",
        "PostedDate": "2026-11-13",
        "secondaryLocations": [{"Name": name, "CountryCode": "US"} for name in secondary],
    }


def test_oracle_matches_secondary_locations_and_checks_its_site():
    site = ("recruitingCESites/CX", {"SiteNumber": "CX", "SiteName": "Careers"})
    detail = ("Details?", {"items": [{"ExternalDescriptionStr": TEXT}]})
    listing = oracle_listing([oracle_row(1, "Plano, TX, United States"), oracle_row(2)], 2)
    rows, _, calls = collect(
        "oracle",
        [site, detail, ("JobRequisitions?", listing)],
        board="example.fa.oraclecloud.com/CX",
        locations=["Plano"],
    )
    assert [row["location"] for row in rows] == [
        "New York, NY, United States · Plano, TX, United States"
    ]
    assert calls[0].endswith("/recruitingCESites/CX?onlyData=true")

    # Exactly max_candidates roles with a known total: not capped.
    page = oracle_listing([oracle_row(n) for n in range(50)], 50)
    _, source, _ = collect(
        "oracle",
        [site, detail, ("JobRequisitions?", page)],
        board="example.fa.oraclecloud.com/CX",
        max_results=1,
        max_candidates=50,
    )
    assert not source.get("candidate_capped")

    # An unknown site would answer with the default site's jobs: it is refused instead.
    missing = DomainError(502, "source_http", "HTTP 404", {"http_status": 404})
    with pytest.raises(DomainError) as caught:
        collect(
            "oracle",
            [("recruitingCESites/", missing), ("JobRequisitions?", listing)],
            board="example.fa.oraclecloud.com/NOPE",
        )
    assert caught.value.code == "board_unavailable"


# #305 -----------------------------------------------------------------------


def test_recruitee_joins_its_locations_and_drops_inline_images():
    image = "data:image/png;base64," + "iVBORw0KGgo" * 50000
    offer = {
        "id": 1,
        "title": "Support Lead",
        "careers_url": "https://example.recruitee.com/o/support-lead",
        "location": "Sofia, Sofia (stolitsa), Bulgaria",
        "city": "Sofia",
        "country": "Bulgaria",
        "locations": [
            {"name": "Sofia", "city": "Sofia", "state": "Sofia (stolitsa)", "country": "Bulgaria"},
            {"name": "Istanbul", "city": "İstanbul", "state": "İstanbul", "country": "Türkiye"},
        ],
        "description": f'{TEXT}<img src="{image}">',
        "requirements": "",
        "remote": False,
        "hybrid": True,
        "employment_type_code": "fulltime_fixed_term",
        "published_at": "2026-10-27 12:11:34 UTC",
    }
    # Escaped as JSON escapes a slash, too.
    body = json.dumps({"offers": [offer]}).replace("/", "\\/").encode()
    checkpoint = {}
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        return body

    source = {
        "name": "Example",
        "kind": "recruitee",
        "config": {"board": "example", "filters": Filters(locations=["Türkiye"]).model_dump()},
        "checkpoint": checkpoint,
        "on_checkpoint": lambda value: None,
    }
    items = [
        feeds.normalize_posting(item) for item in company.collect(source, request, feeds.html_text)
    ]
    assert len(items) == 1
    assert items[0]["location"] == "Sofia, Sofia (stolitsa), Bulgaria · İstanbul, İstanbul, Türkiye"
    assert items[0]["metadata"]["workplace_type"] == "hybrid"
    assert items[0]["metadata"]["job_types"] == ["fulltime", "temporary"]
    assert items[0]["metadata"]["job_type_raw"] == "fulltime_fixed_term"
    assert "base64" not in items[0]["posting"] and "Design, build" in items[0]["posting"]


@pytest.mark.parametrize(
    ("titles", "exclude_titles", "maximum", "expected"),
    [
        (["Platform"], [], 10, ["Platform Engineer", "Platform Engineer II"]),
        (["Engineer"], ["Sales"], 10, ["Platform Engineer", "Platform Engineer II"]),
        (["Engineer"], ["Sales"], 1, ["Platform Engineer"]),
    ],
)
def test_teamtailor_reads_inline_text_and_all_locations(titles, exclude_titles, maximum, expected):
    feed = b"""<rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
      <item><title>Platform Engineer</title><guid>role-1</guid>
        <link>https://example.teamtailor.com/jobs/1-platform-engineer</link>
        <description>&lt;p&gt;Design, build synthetic services.&lt;/p&gt;</description>
        <remoteStatus>hybrid</remoteStatus>
        <tt:locations>
          <tt:location><tt:name>Headquarters</tt:name><tt:city>Paris</tt:city>
            <tt:country>France</tt:country></tt:location>
          <tt:location><tt:name>Berlin</tt:name><tt:city>Berlin</tt:city>
            <tt:country>Germany</tt:country></tt:location>
        </tt:locations>
      </item>
      <item><title>Sales Engineer</title><guid>role-2</guid>
        <link>https://example.teamtailor.com/jobs/2-sales-engineer</link>
        <description>&lt;p&gt;Sell synthetic services.&lt;/p&gt;</description></item>
      <item><title>Platform Engineer II</title><guid>role-3</guid>
        <link>https://example.teamtailor.com/jobs/3-platform-engineer</link>
        <description>&lt;p&gt;Build more synthetic services.&lt;/p&gt;</description></item>
    </channel></rss>"""
    calls = []

    def request(url, **kwargs):
        calls.append(url)
        return feed

    source = {
        "name": "Example",
        "kind": "teamtailor",
        "config": {
            "board": "example",
            "max_results": maximum,
            "filters": Filters(
                titles=titles, exclude_titles=exclude_titles, locations=["Germany"]
            ).model_dump(),
        },
    }
    rows = company.collect(source, request, feeds.html_text)
    assert calls == ["https://example.teamtailor.com/jobs.rss"]
    assert [row["title"] for row in rows] == expected
    assert rows[0]["url"] == "https://example.teamtailor.com/jobs/1-platform-engineer"
    assert rows[0]["location"] == "Paris, France · Berlin, Germany"
    assert rows[0]["posting"] == "Design, build synthetic services."
    assert rows[0]["source_key"] == "teamtailor:example:role-1"
    assert rows[0]["metadata"]["description_complete"] is True
    assert rows[0]["metadata"]["workplace_type"] == "hybrid"


def test_teamtailor_saved_source_collects_through_the_provider_gate(collection, monkeypatch):
    workspace, service = collection
    calls = []
    feed = b"""<rss><channel><item><title>Platform Engineer</title><guid>role-1</guid>
    <link>https://example.teamtailor.com/jobs/1-engineer</link>
    <description>Build and operate synthetic services.</description>
    </item></channel></rss>"""

    def send(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=feed)

    mock_http(monkeypatch, send)
    source = service.create_source(
        SourceCreate(name="Example", url="https://example.teamtailor.com/jobs")
    )
    assert source["kind"] == "teamtailor"
    assert run(service, source)["state"] == "succeeded"
    assert calls == ["https://example.teamtailor.com/jobs.rss"]
    jobs = workspace.list_jobs()
    assert jobs["total"] == 1
    assert jobs["items"][0]["title"] == "Platform Engineer"
    assert service.providers.get("teamtailor")["runtime"]["total_requests"] == 1


@pytest.mark.parametrize("body", [b"<html/>", b"<rss/>", b"not xml"])
def test_teamtailor_refuses_a_page_that_is_not_its_feed(body):
    with pytest.raises(DomainError) as invalid:
        collect("teamtailor", [("/jobs.rss", body)])
    assert invalid.value.code == "source_format"


def test_teamtailor_detects_board_and_provider_without_lookalike_hosts():
    url = "https://example.teamtailor.com/jobs/1-platform-engineer"
    assert detect_source(url) == {"kind": "teamtailor", "config": {"board": "example"}, "url": url}
    assert provider_for_url(url) == "teamtailor"
    assert provider_for_url("https://example.teamtailor.com.evil.test/jobs.rss") == "manual"
    with pytest.raises(DomainError, match="supported ATS"):
        detect_source("https://example.teamtailor.com.evil.test/jobs")


# #306 -----------------------------------------------------------------------


def test_bamboohr_waits_for_the_country_and_reads_type_and_remote():
    listing = {
        "result": [
            {
                "id": "15",
                "jobOpeningName": "Security Engineer",
                "employmentStatusLabel": "Full-Time Netherlands",
                "location": {"city": "Mayfair", "state": "London, City of"},
                "atsLocation": {"country": None, "state": None, "province": None, "city": None},
                "isRemote": None,
                "locationType": "1",
            },
            {
                "id": "16",
                "jobOpeningName": "Analyst",
                "employmentStatusLabel": "Part-Time",
                "location": {"city": None, "state": None},
                "atsLocation": {"country": "Germany", "state": None, "city": "Berlin"},
                "isRemote": None,
                "locationType": "0",
            },
        ]
    }

    def detail(country):
        return {
            "result": {
                "jobOpening": {
                    "description": TEXT,
                    "location": {
                        "city": "Mayfair",
                        "state": "London, City of",
                        "postalCode": "W1S 2BX",
                        "addressCountry": country,
                    },
                    "datePosted": "2026-06-04",
                }
            }
        }

    rows, _, calls = collect(
        "bamboohr",
        [("/careers/15/", detail("United Kingdom")), ("/careers/list", listing)],
        locations=["United Kingdom"],
    )
    assert [row["location"] for row in rows] == ["Mayfair, London, City of, United Kingdom"]
    assert rows[0]["metadata"]["job_type"] == "fulltime"
    assert rows[0]["metadata"]["is_remote"] is True
    assert rows[0]["metadata"]["workplace_type"] == "remote"
    assert rows[0]["metadata"]["country"] == "United Kingdom"
    # The Berlin role names its country in the listing: filtered out without a detail.
    assert not any("/careers/16/" in url for url in calls)


# #307 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "board"),
    [
        ("greenhouse", "example"),
        ("workable", "example"),
        ("recruitee", "example"),
        ("teamtailor", "example"),
        ("personio", "example"),
        ("rippling", "example"),
        ("joincom", "example"),
        ("ashby", "example"),
        ("lever", "example"),
    ],
)
def test_a_missing_board_is_named_and_not_retried(collection, monkeypatch, kind, board):
    _, service = collection
    calls = []

    def send(request):
        calls.append(str(request.url))
        return httpx.Response(404, text="Not found")

    mock_http(monkeypatch, send)
    source = service.create_source(SourceCreate(name="Example", kind=kind, config={"board": board}))
    task = run(service, source)
    assert task["state"] == "failed" and len(calls) == 1
    assert task["result"]["attempt_history"][-1]["reason"] == "board_unavailable"
    assert f"'{board}'" in task["error"]


def test_a_redirect_to_the_vendor_home_page_is_a_missing_board(collection, monkeypatch):
    _, service = collection
    calls = []

    def send(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://www.bamboohr.com/"})

    mock_http(monkeypatch, send)
    source = service.create_source(
        SourceCreate(name="Example", kind="bamboohr", config={"board": "example"})
    )
    task = run(service, source)
    assert task["state"] == "failed" and calls == ["https://example.bamboohr.com/careers/list"]
    assert task["result"]["attempt_history"][-1]["reason"] == "board_unavailable"


def test_an_empty_board_unlists_its_jobs_only_when_the_next_run_confirms_it(
    collection, monkeypatch
):
    workspace, service = collection
    listing = {"value": [smartrecruiters_row(1), smartrecruiters_row(2)]}

    def send(request):
        url = str(request.url)
        if "postings?" in url:
            return httpx.Response(
                200, json={"totalFound": len(listing["value"]), "content": listing["value"]}
            )
        return httpx.Response(200, json={"jobAd": {"sections": {"jobDescription": {"text": TEXT}}}})

    mock_http(monkeypatch, send)
    service.update_provider(
        "smartrecruiters",
        ProviderPatch(
            expected_version=service.providers.get("smartrecruiters")["version"],
            min_interval_seconds=0,
        ),
    )
    source = service.create_source(
        SourceCreate(name="Example", kind="smartrecruiters", config={"board": "Example"})
    )
    assert run(service, source)["state"] == "succeeded"
    assert workspace.list_jobs()["total"] == 2
    listing["value"] = []
    first = run(service, source)
    assert first["state"] == "succeeded" and not first["result"].get("unlisted")
    assert first["result"]["listing_empty"]
    assert workspace.list_jobs(signal="no_longer_listed")["total"] == 0
    second = run(service, source)
    assert second["result"]["unlisted"] == 2
    assert workspace.list_jobs(signal="no_longer_listed")["total"] == 2


# #308 -----------------------------------------------------------------------


def test_rss_dates_and_job_types_share_one_vocabulary(monkeypatch):
    feed = (
        b"<rss><channel><item><guid>1</guid><title>Engineer</title>"
        b"<link>https://example.org/1</link><description>Build APIs</description>"
        b"<pubDate>Wed, 21 Oct 2026 12:54:56 +0000</pubDate></item></channel></rss>"
    )
    monkeypatch.setattr(feeds, "fetch_bytes", lambda *args, **kwargs: feed)
    rows = feeds.fetch_postings(
        {"kind": "rss", "name": "Example", "config": {"url": "https://example.org/feed"}}
    )
    assert rows[0]["metadata"]["date_posted"] == "2026-10-21"

    table = {
        "fulltime_fixed_term": ["fulltime", "temporary"],  # Recruitee
        "fulltime_permanent": ["fulltime"],
        "full-or-part-time": ["fulltime", "parttime"],  # Personio
        "working_student": ["parttime"],
        "Full-Time Netherlands": ["fulltime"],  # BambooHR
        "Part-Time": ["parttime"],
        "SALARIED_FT": ["fulltime"],  # Rippling
        "FULL_TIME": ["fulltime"],  # JOIN
        "FullTime": ["fulltime"],  # Ashby
        "Contractor": ["contract"],
        "Internship": ["internship"],
        "International Sales": [],
        "Employee": [],
        "fulltime": ["fulltime"],  # JobSpy's own values are kept
        "internship": ["internship"],
        "perdiem": [],
    }
    assert {value: feeds.job_types([value]) for value in table} == table
    first = feeds.normalize_posting({"metadata": {"job_type": "Full-Time Netherlands"}})
    assert first["metadata"]["job_type"] == "fulltime"
    assert first["metadata"]["job_type_raw"] == "Full-Time Netherlands"
    # Normalizing again keeps the board's value.
    assert feeds.normalize_posting(first)["metadata"] == first["metadata"]


# #309 -----------------------------------------------------------------------


def test_a_large_listing_is_read_and_a_larger_one_is_named(collection, monkeypatch):
    _, service = collection
    size = {"value": MAX_FEED_BYTES + 1024}

    def send(request):
        padding = "x" * (size["value"] - 200)
        return httpx.Response(
            200,
            json={"jobs": [{"id": "1", "title": "Engineer", "descriptionPlain": padding}]},
        )

    mock_http(monkeypatch, send)
    source = service.create_source(
        SourceCreate(name="Example", kind="ashby", config={"board": "example"})
    )
    assert run(service, source)["state"] == "succeeded"
    size["value"] = MAX_LISTING_BYTES + 1024
    task = run(service, source)
    assert task["state"] == "failed"
    assert task["result"]["attempt_history"][-1]["reason"] == "source_too_large"
    assert "'example'" in task["error"] and "64 MB" in task["error"]


# #355 -----------------------------------------------------------------------


def greenhouse_jobs(count, content=True):
    """A synthetic board; the listing escapes each description as Greenhouse does."""
    return {
        "jobs": [
            {
                "id": 9000 + index,
                "title": f"Engineer {index}",
                "location": {"name": "Remote"},
                "absolute_url": f"https://boards.greenhouse.io/example/jobs/{9000 + index}",
                **(
                    {
                        "content": html.escape(f"<p>Build service {index} &amp; run it.</p>"),
                        "departments": [{"id": 7, "name": "Engineering"}],
                    }
                    if content
                    else {}
                ),
            }
            for index in range(count)
        ]
    }


def test_a_greenhouse_board_reads_every_description_in_one_listing(collection, monkeypatch):
    workspace, service = collection
    calls = []

    def send(request):
        calls.append(str(request.url))
        if request.url.path.endswith("/offices"):
            return httpx.Response(200, json={"offices": []})
        return httpx.Response(200, json=greenhouse_jobs(254))

    mock_http(monkeypatch, send)
    source = service.create_source(
        SourceCreate(
            name="Example", kind="greenhouse", config={"board": "example", "max_results": 300}
        )
    )
    assert run(service, source)["state"] == "succeeded"
    assert len(calls) <= 3 and not any("/jobs/" in url for url in calls)
    assert workspace.list_jobs()["total"] == 254

    # The text equals what one detail request a job reads.
    bulk, _, _ = collect("greenhouse", [("/jobs", greenhouse_jobs(3)), ("/offices", {})])
    details = [
        (f"/jobs/{row['id']}", {key: row[key] for key in ("content", "departments")})
        for row in greenhouse_jobs(3)["jobs"]
    ]
    single, _, calls = collect(
        "greenhouse", [*details, ("/jobs", greenhouse_jobs(3, content=False)), ("/offices", {})]
    )
    assert sum("/jobs/" in url for url in calls) == 3
    assert [row["posting"] for row in bulk] == [row["posting"] for row in single]
    assert bulk[0]["posting"] == "Build service 0 & run it."
    assert bulk[0]["metadata"]["departments"] == ["Engineering"]


def test_a_greenhouse_row_without_text_stays_pending_in_the_checkpoint():
    listing = greenhouse_jobs(3)
    listing["jobs"][1]["content"] = ""
    checkpoint, calls = {}, []

    def request(url, **kwargs):
        calls.append(url)
        if "/jobs/" in url:
            raise ProviderDeferred("greenhouse", "collection_batch")
        return json.dumps(listing if "/jobs" in url else {}).encode()

    source = {
        "name": "Example",
        "kind": "greenhouse",
        "config": {"board": "example"},
        "checkpoint": checkpoint,
    }
    rows = company.collect(source, request, feeds.html_text)
    assert calls[-1].endswith("/jobs/9001") and len(calls) == 3
    assert checkpoint["pending_details"] == 1 and not checkpoint["complete"]
    assert [row["metadata"]["description_status"] for row in rows] == [
        "complete",
        "pending",
        "complete",
    ]


def test_a_greenhouse_board_too_large_for_one_listing_reads_details():
    too_large = DomainError(422, "source_too_large", "Too large.", {"maximum_bytes": 1})
    rows, _, calls = collect(
        "greenhouse",
        [
            ("/jobs/9000", {"content": TEXT}),
            ("/jobs?content=true", too_large),
            ("/jobs", greenhouse_jobs(1, content=False)),
            ("/offices", {}),
        ],
    )
    assert calls[:2] == [
        "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true",
        "https://boards-api.greenhouse.io/v1/boards/example/jobs",
    ]
    assert rows[0]["posting"] and calls[-1].endswith("/jobs/9000")


# #388 -----------------------------------------------------------------------


def test_greenhouse_labels_drop_placeholder_and_repeated_offices():
    def office(id, name, location, job, parent=None):
        return {
            "id": id,
            "name": name,
            "location": location,
            "parent_id": parent,
            "departments": [{"id": 7, "name": "Engineering", "jobs": [{"id": job}]}],
        }

    listing = greenhouse_jobs(3)
    listing["jobs"][0]["location"] = {"name": "Remote, Poland"}
    listing["jobs"][1]["location"] = {"name": "No Office"}
    listing["jobs"][2]["location"] = {"name": "Hybrid"}
    offices = {
        "offices": [
            office(1, "No Office", None, 9000),
            office(2, "Poland", "Poland", 9000),
            office(3, "Turkey", "Istanbul, Turkey", 9000),
            office(4, "Berlin", "Berlin, Germany", 9001),
            # The region and its city both list the role: the city names it.
            office(5, "EMEA", None, 9002),
            office(6, "Zurich", "Zurich, Switzerland", 9002, parent=5),
            office(7, "Zurich HQ", "Zurich", 9002, parent=5),
        ]
    }
    rows, _, _ = collect("greenhouse", [("/jobs", listing), ("/offices", offices)])
    assert [row["location"] for row in rows] == [
        "Remote, Poland · Istanbul, Turkey",
        "Berlin, Germany",
        "Hybrid · Zurich, Switzerland",
    ]
    assert company.places(" Paris ", "N/A", "Paris", "New York", "York") == (
        "Paris · New York · York"
    )


# #367 -----------------------------------------------------------------------

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<rss><channel><item><guid>1</guid><title>&lol4;</title></item></channel></rss>"""
EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE workzag-jobs [<!ENTITY secret SYSTEM "file:///etc/hostname">]>
<workzag-jobs><position><id>1</id><name>&secret;</name></position></workzag-jobs>"""


@pytest.mark.parametrize(
    "body",
    [
        BILLION_LAUGHS,
        EXTERNAL_ENTITY,
        b'<?xml version="1.0"?><!DOCTYPE rss SYSTEM "https://example.org/rss.dtd"><rss/>',
    ],
)
@pytest.mark.parametrize("kind", ["rss", "personio", "teamtailor"])
def test_xml_declarations_are_refused_with_a_typed_error(monkeypatch, kind, body):
    monkeypatch.setattr(feeds, "fetch_bytes", lambda *args, **kwargs: body)
    with pytest.raises(DomainError) as refused:
        feeds.fetch_postings(
            {
                "kind": kind,
                "name": "Example",
                "config": {"url": "https://example.org/feed", "board": "example"},
            }
        )
    assert refused.value.code == "source_xml_declarations" and refused.value.status == 422


def test_plain_xml_with_character_references_still_parses():
    # A declaration in another encoding is refused as well.
    utf16 = BILLION_LAUGHS.replace(b'"1.0"?>', b'"1.0" encoding="UTF-16"?>')
    with pytest.raises(DomainError) as refused:
        company.xml(utf16.decode().encode("utf-16"))
    assert refused.value.code == "source_xml_declarations"
    root = company.xml(b"<rss><title>R&amp;D &#8212; &lt;Example&gt;</title></rss>")
    assert root.findtext("title") == "R&D — <Example>"
    with pytest.raises(DomainError) as invalid:
        company.xml(b"<rss><title>&undefined;</title></rss>")
    assert invalid.value.code == "source_format"


@pytest.mark.parametrize(
    ("status", "workplace", "remote"),
    [
        ("fully", "remote", True),
        ("hybrid", "hybrid", None),
        ("none", "onsite", False),
        ("onsite", "onsite", False),
        ("temporary", "hybrid", None),
        ("unexpected", None, None),
    ],
)
def test_teamtailor_normalizes_remote_status(status, workplace, remote):
    feed = f"""<rss xmlns:tt="https://teamtailor.com/locations"><channel><item>
    <title>Engineer</title><guid>1</guid><link>https://example.teamtailor.com/jobs/1</link>
    <description>Build synthetic services.</description><remoteStatus>{status}</remoteStatus>
    <tt:locations><tt:location><tt:city>Paris</tt:city><tt:country>France</tt:country>
    </tt:location></tt:locations></item></channel></rss>""".encode()
    rows, _, _ = collect("teamtailor", [("/jobs.rss", feed)])
    assert rows[0]["metadata"].get("workplace_type") == workplace
    assert rows[0]["metadata"]["is_remote"] is remote
    filtered, _, _ = collect("teamtailor", [("/jobs.rss", feed)], locations=["Remote"])
    if status == "fully":
        assert len(filtered) == 1
        assert filtered[0]["location"] == "Paris, France"


@pytest.mark.parametrize("count,capped", [(99, False), (100, True)])
def test_teamtailor_reports_the_default_feed_cap(count, capped):
    item = "<item><title>Engineer</title><guid>{}</guid><description>Build services.</description></item>"
    feed = (
        "<rss><channel>" + "".join(item.format(i) for i in range(count)) + "</channel></rss>"
    ).encode()
    _, source, calls = collect("teamtailor", [("/jobs.rss", feed)], max_results=200)
    assert source.get("result_capped", False) is capped
    assert calls == ["https://example.teamtailor.com/jobs.rss"]
