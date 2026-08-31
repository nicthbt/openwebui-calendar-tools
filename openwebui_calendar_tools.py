"""
title: Calendar tools
author: Nicolas THIBAUT
git_url: https://github.com/uppersafe/
description: Search calendar for information and fetch specific event .
license: AGPL-3.0-only
version: 1.0.0
required_open_webui_version: 0.10.2
requirements: caldav
"""

import os
import io
import re
import time
import json
import stat
import unicodedata
import mimetypes
import asyncio
import logging
import urllib
from hashlib import blake2b
from difflib import SequenceMatcher
from fastapi import Request
from pydantic import BaseModel, Field
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timedelta
from caldav import DAVClient

from open_webui.models.users import UserModel

log = logging.getLogger(__name__)


class CalendarException(Exception):
    def __init__(self, message, error=None):
        super().__init__(message)
        self.error = error


class CalendarClient:
    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        username: str,
        password: str,
        verify: bool = True,
    ):
        self.url = f'https://{host}:{port}/{path.lstrip("/")}'
        self.dav = DAVClient(
            url=self.url,
            username=username,
            password=password,
            timeout=10,
            ssl_verify_cert=verify,
        )
        self.calendars = {}

    def close(self) -> None:
        try:
            self.dav.close()
        except Exception as e:
            raise CalendarException("Unable to close session", e)

    def list(self) -> list:
        try:
            self.calendars = dict(
                [(calendar.name, calendar) for calendar in self.dav.get_calendars()]
            )
        except Exception as e:
            raise CalendarException("Unable to list calendars", e)

        return self.calendars.keys()

    def search(self, calendar: str, start: datetime, end: datetime) -> list:
        results = []

        if calendar not in self.list():
            raise CalendarException(f"Unable to find calendar '{calendar}'")

        try:
            results = self.dav.search_calendar(
                self.calendars.get(calendar, None),
                event=True,
                start=start,
                end=end,
                expand=bool(end),
            )
        except Exception as e:
            raise CalendarException(f"Unable to search events in '{calendar}'", e)

        return results


def with_context(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        session = None
        token = None

        try:
            __request__ = kwargs.get("__request__", None)
            __user__ = kwargs.get("__user__", None)
            __event_emitter__ = kwargs.get("__event_emitter__", None)
            __event_call__ = kwargs.get("__event_call__", None)

            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")

            user = UserModel(**__user__)
            username, password = self._get_credentials(__user__.get("valves"))

            await self._emit_status(
                __event_emitter__,
                "Connecting to calendar server...",
                done=False,
            )

            # Connect to caldav server
            session = await self._connect_caldav(username, password)

            # Set context for this call
            token = self.context.set((user, session))

            return await func(self, *args, **kwargs)

        except CalendarException as e:
            log.error(f"{e} = {e.error}")
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Reset context for this call
            if token is not None:
                self.context.reset(token)

            # Disconnect from caldav server
            if session is not None:
                self._disconnect(session)

    return wrapper


class Tools:
    class UserValves(BaseModel):
        username: str = Field(
            title="Calendar username",
            default=None,
        )
        password: str = Field(
            title="Calendar password",
            default=None,
            json_schema_extra={"input": {"type": "password"}},
        )

    class Valves(BaseModel):
        protocol: str = Field(
            title="Protocol",
            default="caldav",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [
                        {"value": "caldav", "label": "CalDAV"},
                    ],
                }
            },
        )
        verify_ssl: bool = Field(
            title="SSL verification",
            default=True,
        )
        host: str = Field(
            title="Server hostname or IP address",
            default="host.docker.internal",
        )
        port: int | None = Field(
            title="Server port",
            default=None,
            ge=1,
            le=65535,
        )
        path: str | None = Field(
            title="Server path",
            default="/",
        )
        search_count: int = Field(
            title="Search result count",
            default=100,
        )
        search_timeout: int = Field(
            title="Search timeout",
            default=60,
        )

    def __init__(self):
        self.valves = self.Valves()
        self.context = ContextVar("tools.calendar")
        self.namespace = "tools.calendar.files"

    async def _connect_caldav(
        self,
        username: str,
        password: str,
        __event_call__=None,
    ) -> CalendarClient:
        session = CalendarClient(
            host=self.valves.host,
            port=self.valves.port or 443,
            path=self.valves.path,
            username=username,
            password=password,
            verify=self.valves.verify_ssl,
        )
        return session

    def _disconnect(self, session) -> None:
        if hasattr(session, "close"):
            session.close()

    def _browse_caldav(
        self,
        session,
        query: str,
        start: str,
        end: str,
        calendars: list,
        timeout: int = None,
    ) -> list:
        results = []
        timeout = timeout or int(time.monotonic() + self.valves.search_timeout)

        # Extract search keywords
        keywords = self._extract_keywords(query)

        # Convert time range
        range_start, range_end = self._get_time_range(start, end)

        # List calendars
        if len(calendars) == 0:
            calendars = session.list()

        try:
            for calendar in calendars:
                if int(time.monotonic()) >= timeout:
                    raise TimeoutError(
                        f"Timeout of search task after {self.valves.search_timeout} secs"
                    )

                # Search for events
                events = session.search(calendar, range_start, range_end)

                for event in events:
                    path = urllib.parse.urlsplit(str(event.url)).path
                    component = event.get_icalendar_component()
                    attendees = [
                        re.sub("mailto:", "", attendee)
                        for attendee in map(str, component.attendees)
                        if attendee.startswith("mailto:")
                    ]
                    results.append(
                        self._score_message(
                            calendar,
                            path,
                            component.start,
                            component.end,
                            component.summary,
                            component.description,
                            component.location,
                            attendees,
                            keywords,
                        )
                    )

        except TimeoutError as e:
            log.warning(e)

        # Sort results and return best matches
        return self._sort_results(results, [("score", True), ("start", False)])

    def _get_time_range(self, start: str, end: str) -> tuple:
        range_start = None
        range_end = None

        try:
            if start is not None:
                range_start = datetime.fromisoformat(start.strip())
            else:
                range_start = datetime.now().astimezone()
            if end is not None:
                range_end = datetime.fromisoformat(end.strip())
            else:
                range_end = end
        except Exception as e:
            raise ValueError("Invalid ISO 8601 format")

        if range_end is not None and range_end <= range_start:
            raise ValueError("End date must be after start date")

        return range_start, range_end

    def _get_credentials(self, config: dict) -> dict:
        if config.username is None:
            raise ValueError("Please configure calendar username")

        if config.password is None:
            raise ValueError("Please configure calendar password")

        return config.username.strip(), config.password.strip()

    def _seq_match(self, text: str, keywords: list) -> list:
        # Normalize text in ascii characters
        nfkd_text = (
            unicodedata.normalize("NFKD", text.lower())
            .encode("ascii", "ignore")
            .decode()
        )
        # Normalize keywords in ascii characters
        nfkd_keywords = [
            unicodedata.normalize("NFKD", keyword.lower())
            .encode("ascii", "ignore")
            .decode()
            for keyword in keywords
        ]

        # Get the match length for each keyword
        return [
            SequenceMatcher(None, nfkd_keyword, nfkd_text).find_longest_match().size
            for nfkd_keyword in nfkd_keywords
        ]

    def _score_message(
        self,
        calendar: str,
        path: str,
        start: datetime,
        end: datetime,
        title: str,
        description: str,
        location: str,
        attendees: list,
        keywords: list,
    ) -> dict:
        # Initialize score to zero
        score = 0

        # Calculate the keywords total length
        total_length = sum(len(keyword) for keyword in keywords)

        # Calculate the weight of one character
        match_weight = 1.0 / max(1.0, total_length)

        if title is not None:
            # Calculate match with title
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(title, keywords)
            )

        if description is not None:
            # Calculate match with description
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(description, keywords)
            )

        if location is not None:
            # Calculate match with location
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(location, keywords)
            )

        for attendee in attendees:
            # Calculate match with attendee
            score = score + sum(
                match_size * match_weight
                for match_size in self._seq_match(attendee, keywords)
            )

        return {
            "path": path,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar": calendar,
            "title": title,
            "description": description,
            "location": location,
            "attendees": attendees,
            "score": score,
        }

    def _sort_results(self, results: list, keys: list) -> list:
        # Sort by keys from lowest to highest priority
        for key, reverse in reversed(keys):
            results.sort(key=lambda result: result[key], reverse=reverse)
        return results[: self.valves.search_count]

    def _extract_keywords(self, query: str) -> list:
        if query is None or len(query.strip()) == 0:
            return []

        # Split query on special characters (space, tab, comma, etc) and remove linking words
        keywords = set(
            keyword.strip()
            for keyword in re.split(r"[';,\s\t\r\n]+", query)
            if len(keyword.strip()) > 1
        )

        # Check that keywords are not empty
        if len(keywords) == 0:
            raise ValueError(f"Cannot build keywords from query string '{query}'")

        return list(keywords)

    async def _emit_status(
        self,
        event_emitter,
        desc: str,
        done: bool = False,
        hidden: bool = False,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "status",
                    "data": {
                        "description": desc,
                        "done": done,
                        "hidden": hidden,
                    },
                }
            )

    @with_context
    async def search_calendar_events(
        self,
        query: str = None,
        start: str = None,
        end: str = None,
        calendars: list = [],
        __request__: Request = None,
        __user__: dict = None,
        __event_emitter__=None,
        __event_call__=None,
    ) -> str:
        """
        Search for events on calendar.
        Best to quickly identify relevant events.

        :param query: The search keywords to look up without special operators or wildcards (optional)
        :param start: Start of the time range as ISO 8601 timezone-aware date format (inclusive, defaults to now)
        :param end: End of the time range as ISO 8601 timezone-aware date format (exclusive, defaults to none)
        :param calendars: A list of calendars to look into (optional, defaults to all)
        :return: JSON with results containing caldav path, start date, end date, calendar name, title, description, location, attendees and search score of each event
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Searching on calendar...",
            done=False,
        )

        # Browse events on caldav server
        results = await asyncio.to_thread(
            self._browse_caldav,
            session,
            query,
            start,
            end,
            calendars,
        )

        await self._emit_status(
            __event_emitter__,
            f"{len(results)} events found.",
            done=True,
        )

        return json.dumps(list(results), ensure_ascii=False)
