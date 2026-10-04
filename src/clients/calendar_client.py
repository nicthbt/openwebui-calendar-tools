import logging
from datetime import datetime, timedelta
from urllib.parse import urljoin

from caldav import Calendar, DAVClient, Event

from src.common.exceptions import CustomToolException

log = logging.getLogger(__name__)


class CalendarException(CustomToolException):
    pass


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
        except Exception:
            raise CalendarException("Invalid ISO 8601 format")

        if range_start is not None and range_end is not None:
            # Check time value
            time_start = any([range_start.hour, range_start.minute, range_start.second])
            time_end = any([range_end.hour, range_end.minute, range_end.second])

            # Convert datetime to date if time value is equal to zero
            if not time_start and not time_end:
                range_start = range_start.date()
                range_end = range_end.date()

            if range_start >= range_end:
                raise CalendarException("End date must be after start date")

        return range_start, range_end
