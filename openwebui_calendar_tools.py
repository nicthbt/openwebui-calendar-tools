"""
title: Calendar tools
author: Nicolas THIBAUT
git_url: https://github.com/uppersafe/
description: Search on calendar for information and manage specific event content.
license: AGPL-3.0-only
version: 1.2.1
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
from urllib.parse import urljoin, urlsplit
from hashlib import blake2b
from difflib import SequenceMatcher
from fastapi import Request, UploadFile
from pydantic import BaseModel, Field
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timedelta
from caldav import DAVClient, Calendar, Event

from open_webui.models.users import UserModel
from open_webui.models.config import Config
from open_webui.models.files import Files
from open_webui.internal.db import get_async_db_context
from open_webui.routers.files import upload_file_handler
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT
from open_webui.routers.retrieval import (
    ProcessFileForm,
    process_file,
)

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
        self.server = f"https://{host}:{port}"
        self.dav = DAVClient(
            url=urljoin(self.server, path),
            username=username,
            password=password,
            timeout=10,
            ssl_verify_cert=verify,
        )
        self.calendars = None

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

        return list(self.calendars)

    def select(self, calendar: str) -> Calendar:
        calendars = self.list() if self.calendars is None else list(self.calendars)
        if calendar not in calendars:
            raise CalendarException(f"Unable to find '{calendar}' among {calendars}")

        return self.calendars.get(calendar)

    def search(self, calendar: str, start: str, end: str) -> list:
        events = []

        # Convert to time range
        range_start, range_end = self.get_time_range(start, end, default=True)

        # Get calendar handler
        handler = self.select(calendar)

        try:
            events = handler.search(
                event=True,
                start=range_start,
                end=range_end,
                expand=True,
            )
        except Exception as e:
            raise CalendarException(f"Unable to search events in '{calendar}'", e)

        return [(event.get_icalendar_component(), event.url) for event in events]

    def create(
        self,
        calendar: str,
        start: str,
        end: str,
        title: str,
        description: str,
        location: str,
        attendees: list,
    ) -> tuple:
        event = None

        # Convert to time range
        event_start, event_end = self.get_time_range(start, end)

        # Get calendar handler
        handler = self.select(calendar)

        try:
            event = handler.add_event(
                dtstart=event_start,
                dtend=event_end,
                summary=title,
                description=description,
                location=location,
            )
            with event.edit_icalendar_component() as component:
                if attendees is not None:
                    for attendee in attendees:
                        component.add("attendee", f"mailto:{attendee}")
            event.save()
        except Exception as e:
            raise CalendarException(f"Unable to create event in '{calendar}'", e)

        return event.get_icalendar_component(), event.url

    def update(
        self,
        path: str,
        calendar: str,
        start: str,
        end: str,
        title: str,
        description: str,
        location: str,
        attendees: list,
    ) -> tuple:
        event = None

        # Convert to time range
        event_start, event_end = self.get_time_range(start, end)

        # Get calendar handler
        handler = self.select(calendar)

        try:
            event = handler.event_by_url(urljoin(self.server, path))
            with event.edit_icalendar_component() as component:
                if event_start is not None:
                    component.pop("dtstart", None)
                    component.add("dtstart", event_start)
                if event_end is not None:
                    component.pop("dtend", None)
                    component.add("dtend", event_end)
                if title is not None:
                    component.pop("summary", None)
                    component.add("summary", title)
                if description is not None:
                    component.pop("description", None)
                    component.add("description", description)
                if location is not None:
                    component.pop("location", None)
                    component.add("location", location)
                if attendees is not None:
                    component.pop("attendee", None)
                    for attendee in attendees:
                        component.add("attendee", f"mailto:{attendee}")
            event.save()
        except Exception as e:
            raise CalendarException(f"Unable to update event '{path}'", e)

        return event.get_icalendar_component(), event.url

    def delete(self, path: str) -> None:
        try:
            event = Event(client=self.dav, url=urljoin(self.server, path))
            event.delete()
        except Exception as e:
            raise CalendarException(f"Unable to delete event '{path}'", e)

    def download(self, path: str) -> bytes:
        event = None

        try:
            event = Event(client=self.dav, url=urljoin(self.server, path))
            event.load()
        except Exception as e:
            raise CalendarException(f"Unable to download event '{path}'", e)

        return event.get_data().encode()

    def get_time_range(self, start: str, end: str, default: bool = False) -> tuple:
        range_start = None
        range_end = None

        if start is not None and end is None:
            raise CalendarException("End date must be set when start date is set")
        if end is not None and start is None:
            raise CalendarException("Start date must be set when end date is set")

        try:
            if start is not None:
                range_start = datetime.fromisoformat(start.strip())
            elif default:
                range_start = datetime.now().replace(microsecond=0).astimezone()
            else:
                range_start = start
            if end is not None:
                range_end = datetime.fromisoformat(end.strip())
            elif default:
                range_end = range_start + timedelta(days=365)
            else:
                range_end = end
        except Exception as e:
            raise CalendarException("Invalid ISO 8601 format")

        time_start = any([range_start.hour, range_start.minute, range_start.second])
        time_end = any([range_end.hour, range_end.minute, range_end.second])

        if not time_start and not time_end:
            range_start = range_start.date()
            range_end = range_end.date()

        if range_start and range_end and range_start >= range_end:
            raise CalendarException("End date must be after start date")

        return range_start, range_end


def with_context(func):
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        session = None
        token = None

        try:
            __request__ = kwargs.get("__request__", None)
            __user__ = kwargs.get("__user__", None)
            __metadata__ = kwargs.get("__metadata__", None)
            __event_emitter__ = kwargs.get("__event_emitter__", None)
            __event_call__ = kwargs.get("__event_call__", None)

            if __request__ is None:
                raise ValueError("Request context not available")
            if __user__ is None:
                raise ValueError("User context not available")
            if __metadata__ is None:
                raise ValueError("Metadata context not available")
            else:
                if __metadata__.get("files", None) is None:
                    __metadata__["files"] = []

            user = UserModel(**__user__)
            username, password = self._get_credentials(__user__.get("valves"))

            await self._emit_status(
                __event_emitter__,
                "Connecting to calendar server...",
                done=False,
            )

            # Connect to server
            session = await self._connect_caldav(username, password)

            # Set context for this call
            token = self.context.set((user, session))

            return await func(self, *args, **kwargs)

        except CalendarException as e:
            log.error(f"{e} ({e.error})" if e.error else str(e))
            return json.dumps({"error": str(e)})

        except Exception as e:
            log.exception(e)
            return json.dumps({"error": str(e)})

        finally:
            # Reset context for this call
            if token is not None:
                self.context.reset(token)

            # Disconnect from server
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
                for component, url in session.search(calendar, start, end):
                    results.append(
                        self._score_message(
                            url,
                            calendar,
                            component.start,
                            component.end,
                            component.summary,
                            component.description,
                            component.location,
                            self._get_attendees(component.attendees),
                            keywords,
                        )
                    )

        except TimeoutError as e:
            log.warning(e)

        # Sort results and return best matches
        return self._sort_results(results, [("score", True), ("start", False)])

    def _create_caldav(
        self,
        session,
        calendar: str,
        start: str,
        end: str,
        title: str,
        description: str,
        location: str,
        attendees: list,
    ) -> dict:
        component, url = session.create(
            calendar,
            start,
            end,
            title,
            description,
            location,
            attendees,
        )
        return self._format_result(
            url,
            calendar,
            component.start,
            component.end,
            component.summary,
            component.description,
            component.location,
            self._get_attendees(component.attendees),
        )

    def _update_caldav(
        self,
        session,
        path: str,
        calendar: str,
        start: str,
        end: str,
        title: str,
        description: str,
        location: str,
        attendees: list,
    ) -> dict:
        component, url = session.update(
            path,
            calendar,
            start,
            end,
            title,
            description,
            location,
            attendees,
        )
        return self._format_result(
            url,
            calendar,
            component.start,
            component.end,
            component.summary,
            component.description,
            component.location,
            self._get_attendees(component.attendees),
        )

    def _delete_caldav(self, session, path: str) -> None:
        session.delete(path)

    def _download_caldav(self, session, path: str) -> bytes:
        return session.download(path)

    def _get_attendees(self, attendees: list):
        return [
            re.sub("^mailto:", "", attendee)
            for attendee in map(str, attendees)
            if attendee.startswith("mailto:")
        ]

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
        url: str,
        calendar: str,
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

        return self._format_result(
            url,
            calendar,
            start,
            end,
            title,
            description,
            location,
            attendees,
            score,
        )

    def _format_result(
        self,
        url: str,
        calendar: str,
        start: datetime,
        end: datetime,
        title: str,
        description: str,
        location: str,
        attendees: list,
        score: float = None,
    ) -> dict:
        result = {
            "path": urlsplit(str(url)).path,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar": calendar,
            "title": title,
            "description": description,
            "location": location,
            "attendees": attendees,
        }
        if score is not None:
            result.update({"score": score})
        return result

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

    async def _get_cache_file(
        self,
        file_hash: str,
        user: UserModel,
    ) -> tuple:
        cache_key = f"{self.namespace}.{user.id}.{file_hash}"
        cache_value = await Config.get(cache_key, {})

        file_id = cache_value.get("id", None)
        file_collection = cache_value.get("collection", None)

        cleanup = False

        if file_id is not None:
            file = await Files.get_file_by_id(file_id)
            if not file:
                cleanup = True

        if file_collection is not None:
            collection = await ASYNC_VECTOR_DB_CLIENT.has_collection(file_collection)
            if not collection:
                cleanup = True

        # Delete cache if file or collection no longer exist
        if cleanup:
            log.warning(f"Deleting cache for {cache_key}")
            await Config.delete(cache_key)
            return None, None

        return file_id, file_collection

    async def _set_cache_file(
        self,
        file_hash: str,
        file_id: str,
        file_collection: str,
        user: UserModel,
    ) -> None:
        cache_key = f"{self.namespace}.{user.id}.{file_hash}"
        cache_value = {
            "id": file_id,
            "collection": file_collection,
        }
        await Config.upsert({cache_key: cache_value})

    async def _upload_file(
        self,
        filename: str,
        mimetype: str,
        content: bytes,
        process: bool,
        user: UserModel,
        __request__: Request,
    ) -> tuple:
        async with get_async_db_context() as db:
            # Search for file in cache
            file_hash = blake2b(content).hexdigest()
            file_id, file_collection = await self._get_cache_file(
                file_hash,
                user=user,
            )

            # Upload file if not in cache
            if file_id is None:
                log.info(f"Uploading '{filename}'")
                file = await upload_file_handler(
                    __request__,
                    UploadFile(
                        file=io.BytesIO(content),
                        filename=filename,
                        headers={"content-type": mimetype},
                    ),
                    metadata={},
                    process=False,
                    user=user,
                    db=db,
                )
                file_id = file.id

            # Process file if not in cache
            if file_collection is None and process is True:
                log.info(f"Processing '{filename}'")
                result = await process_file(
                    __request__,
                    ProcessFileForm(file_id=file_id),
                    user=user,
                    db=db,
                )
                file_collection = result.get("collection_name")

            await self._set_cache_file(
                file_hash,
                file_id,
                file_collection,
                user=user,
            )

            return file_id, file_collection

    async def _emit_files(
        self,
        event_emitter,
        files: list,
    ) -> None:
        if event_emitter:
            await event_emitter(
                {
                    "type": "files",
                    "data": {
                        "files": [{**file, "type": "file"} for file in files],
                    },
                }
            )

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
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Search for events on calendar.
        Best to quickly identify relevant events.

        :param query: The search keywords to look up without special operators or wildcards (optional)
        :param start: Start of the time range as ISO 8601 timezone-aware date format (inclusive, defaults to now)
        :param end: End of the time range as ISO 8601 timezone-aware date format (exclusive, defaults to a year from now)
        :param calendars: A list of calendars to look into (optional, defaults to all)
        :return: JSON with results containing caldav path, start date, end date, calendar name, title, description, location, attendees and search score of each event
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Searching for events...",
            done=False,
        )

        # Browse events
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

    @with_context
    async def fetch_calendar_events(
        self,
        events: list,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Fetch specific events from calendar and generate download URL.
        Best for content retrieval as ICS format.

        :param events: A list of caldav path for events to fetch
        :return: JSON with results containing file ID, filename and download URL for each event
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            f"Fetching {len(events)} events...",
            done=False,
        )

        results = {}

        for path in events:
            filename = os.path.basename(path)
            mimetype, encoding = mimetypes.guess_type(filename)

            if not mimetype.startswith("text/"):
                raise TypeError(f"Invalid mimetype '{mimetype}' for '{path}'")

            log.info(f"Downloading '{path}'")
            content = await asyncio.to_thread(self._download_caldav, session, path)

            # Upload file but do not process content
            file_id, file_collection = await self._upload_file(
                filename,
                mimetype,
                content,
                process=False,
                user=user,
                __request__=__request__,
            )

            # Build download link
            results.update(
                {
                    file_id: {
                        "id": file_id,
                        "name": filename,
                        "size": len(content),
                        "content_type": mimetype,
                        "url": __request__.app.url_path_for(
                            "get_file_content_by_id",
                            id=file_id,
                            file_name=filename,
                        ),
                    }
                }
            )

        # Add files to chat metadata
        __metadata__["files"].extend(list(results.values()))

        await self._emit_files(
            __event_emitter__,
            list(results.values()),
        )

        await self._emit_status(
            __event_emitter__,
            f"{len(results)} results found.",
            done=True,
        )

        return json.dumps(list(results.values()), ensure_ascii=False)

    @with_context
    async def create_calendar_event(
        self,
        calendar: str,
        start: str,
        end: str,
        title: str,
        description: str = None,
        location: str = None,
        attendees: list = None,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Create a new event into calendar.

        :param calendar: The name of the calendar to add the event into
        :param start: Start of the event as ISO 8601 timezone-aware date format (inclusive)
        :param end: End of the event as ISO 8601 timezone-aware date format (exclusive)
        :param title: The summary of the event
        :param description: The description of the event (optional)
        :param location: The location of the event (optional)
        :param attendees: A list of attendees for the event (optional)
        :return: JSON with result containing caldav path, start date, end date, calendar name, title, description, location and attendees of the event
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Creating event...",
            done=False,
        )

        # Create event
        result = await asyncio.to_thread(
            self._create_caldav,
            session,
            calendar,
            start,
            end,
            title,
            description,
            location,
            attendees,
        )

        await self._emit_status(
            __event_emitter__,
            f"Event creation done.",
            done=True,
        )

        return json.dumps(result, ensure_ascii=False)

    @with_context
    async def update_calendar_event(
        self,
        path: str,
        calendar: str,
        start: str = None,
        end: str = None,
        title: str = None,
        description: str = None,
        location: str = None,
        attendees: list = None,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Update an event from calendar.

        :param path: The caldav path of the event
        :param calendar: The name of the calendar to update the event from
        :param start: Start of the event as ISO 8601 timezone-aware date format (inclusive, mandatory when end date is set)
        :param end: End of the event as ISO 8601 timezone-aware date format (exclusive, mandatory when start date is set)
        :param title: The summary of the event (optional)
        :param description: The description of the event (optional)
        :param location: The location of the event (optional)
        :param attendees: A list of attendees for the event (optional)
        :return: JSON with result containing caldav path, start date, end date, calendar name, title, description, location and attendees of the event
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Updating event...",
            done=False,
        )

        # Update event
        result = await asyncio.to_thread(
            self._update_caldav,
            session,
            path,
            calendar,
            start,
            end,
            title,
            description,
            location,
            attendees,
        )

        await self._emit_status(
            __event_emitter__,
            f"Event update done.",
            done=True,
        )

        return json.dumps(result, ensure_ascii=False)

    @with_context
    async def delete_calendar_event(
        self,
        path: str,
        __request__: Request = None,
        __user__: dict = None,
        __metadata__: dict = None,
        __event_emitter__: callable = None,
        __event_call__: callable = None,
    ) -> str:
        """
        Delete an event from calendar.

        :param path: The caldav path of the event
        :return: JSON with result
        """
        user, session = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Deleting event...",
            done=False,
        )

        # Delete event
        await asyncio.to_thread(
            self._delete_caldav,
            session,
            path,
        )

        await self._emit_status(
            __event_emitter__,
            f"Event deletion done.",
            done=True,
        )

        return json.dumps({}, ensure_ascii=False)
