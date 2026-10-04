"""
title: Calendar tools
author: Nicolas THIBAUT
git_url: https://github.com/nicthbt/openwebui-calendar-tools
description: Search on calendar for information and manage specific event content.
license: AGPL-3.0-only
version: 1.4.0
required_open_webui_version: 0.10.2
requirements: caldav
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Request
from pydantic import BaseModel, Field

from src.clients.calendar_client import CalendarClient
from src.clients.terminal_client import OpenTerminalClient
from src.common.custom_tool import CustomTool
from src.common.decorators import with_context
from src.common.exceptions import OpenTerminalException

log = logging.getLogger(__name__)


class Tools(CustomTool):
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
        super().__init__("tools.calendar")

    def _get_credentials(self, config: dict) -> dict:
        if config.username is None:
            raise ValueError("Please configure calendar username")

        if config.password is None:
            raise ValueError("Please configure calendar password")

        return config.username.strip(), config.password.strip()

    def _get_handlers(self) -> tuple:
        # Return handlers
        match self.valves.protocol:
            case "caldav":
                connect_handler = self._connect_caldav
                browse_handler = self._browse_caldav
                download_handler = self._download_caldav
                create_handler = self._create_caldav
                update_handler = self._update_caldav
                delete_handler = self._delete_caldav

            case _:
                raise ValueError("Unknown protocol")

        return (
            connect_handler,
            browse_handler,
            download_handler,
            create_handler,
            update_handler,
            delete_handler,
        )

    async def _connect_caldav(
        self,
        __user__: dict,
        __event_call__: callable = None,
    ) -> CalendarClient:
        username, password = self._get_credentials(__user__.get("valves"))
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
                            self._parse_attendees(component.attendees),
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
            self._parse_attendees(component.attendees),
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
            self._parse_attendees(component.attendees),
        )

    def _delete_caldav(self, session, path: str) -> None:
        session.delete(path)

    def _download_caldav(self, session, path: str) -> bytes:
        return session.download(path)

    def _parse_attendees(self, attendees: list):
        return [
            re.sub("^mailto:", "", attendee)
            for attendee in map(str, attendees)
            if attendee.startswith("mailto:")
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
        Best for quickly identifying relevant events.

        :param query: The search keywords to look up without special operators or wildcards (optional)
        :param start: Start of the time range as ISO 8601 timezone-aware date format (inclusive, defaults to now)
        :param end: End of the time range as ISO 8601 timezone-aware date format (exclusive, defaults to a year from now)
        :param calendars: A list of calendars to look into (optional, defaults to all)
        :return: JSON with results containing CalDAV path, start date, end date, calendar name, title, description, location, attendees and search score of each event
        """
        session, browse_handler, *_ = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Searching for events...",
            done=False,
        )

        # Browse events
        results = await asyncio.to_thread(
            browse_handler,
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

        return json.dumps(results, ensure_ascii=False)

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
        Fetch specific events from calendar and attach them to the conversation.
        Best for downloading events as ICS format and processing them with other tools.

        :param events: A list of CalDAV path for events to fetch
        :return: JSON with results containing file ID or filesystem path, filename, size in bytes and content type for each event
        """
        session, browse_handler, download_handler, *_ = self.context.get()

        await self._emit_status(
            __event_emitter__,
            f"Fetching {len(events)} events...",
            done=False,
        )

        # Init Open Terminal client
        terminal = None
        try:
            terminal = OpenTerminalClient(__request__, __metadata__)
        except OpenTerminalException as e:
            log.warning(e)

        results = []

        for path in events:
            filename = os.path.basename(path)
            mimetype, encoding = mimetypes.guess_type(filename)

            if mimetype and not mimetype.startswith("text/"):
                raise TypeError(f"Invalid mimetype '{mimetype}' for '{path}'")

            log.info(f"Downloading '{path}'")
            content = await asyncio.to_thread(download_handler, session, path)

            if terminal is not None:
                # Upload file to Open Terminal
                result = await asyncio.to_thread(
                    terminal.upload_file,
                    filename,
                    mimetype,
                    content,
                )
                results.append(result)
            else:
                # Upload file but do not process content
                file_id, file_collection = await self._upload_file(
                    path,
                    filename,
                    mimetype,
                    content,
                    process=False,
                    __user__=__user__,
                    __request__=__request__,
                )
                result = {
                    "id": file_id,
                    "name": filename,
                    "size": len(content),
                    "content_type": mimetype,
                }
                results.append(result)

                # Add files to chat metadata for Pyodide
                __metadata__["files"].append(result)

        await self._emit_files(
            __event_emitter__,
            results,
        )

        await self._emit_status(
            __event_emitter__,
            f"{len(results)} results found.",
            done=True,
        )

        return json.dumps(results, ensure_ascii=False)

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
        :return: JSON with result containing CalDAV path, start date, end date, calendar name, title, description, location and attendees of the event
        """
        session, *_, create_handler, update_handler, delete_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Creating event...",
            done=False,
        )

        # Create event
        result = await asyncio.to_thread(
            create_handler,
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
            "Event creation done.",
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

        :param path: The CalDAV path of the event
        :param calendar: The name of the calendar to update the event from
        :param start: Start of the event as ISO 8601 timezone-aware date format (inclusive, mandatory when end date is set)
        :param end: End of the event as ISO 8601 timezone-aware date format (exclusive, mandatory when start date is set)
        :param title: The summary of the event (optional)
        :param description: The description of the event (optional)
        :param location: The location of the event (optional)
        :param attendees: A list of attendees for the event (optional)
        :return: JSON with result containing CalDAV path, start date, end date, calendar name, title, description, location and attendees of the event
        """
        session, *_, create_handler, update_handler, delete_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Updating event...",
            done=False,
        )

        # Update event
        result = await asyncio.to_thread(
            update_handler,
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
            "Event update done.",
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

        :param path: The CalDAV path of the event
        :return: JSON with result
        """
        session, *_, create_handler, update_handler, delete_handler = self.context.get()

        await self._emit_status(
            __event_emitter__,
            "Deleting event...",
            done=False,
        )

        # Delete event
        await asyncio.to_thread(
            delete_handler,
            session,
            path,
        )

        await self._emit_status(
            __event_emitter__,
            "Event deletion done.",
            done=True,
        )

        return json.dumps({}, ensure_ascii=False)
