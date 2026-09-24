# Open WebUI Calendar tools

Search on calendar for information and manage specific event content.

## Features

- Creates, updates and deletes events across calendars.
- Searches for events in specified time range and calendars.
- Ranks search results using keywords scoring and event date.
- Downloads events as ICS files.
- Secures identity and access management with isolated/user-based authentication.
- Supports CalDAV protocol.

## Available tools

### `search_calendar_events`

Searches for events into calendars and returns metadata.

Input parameters:

| Parameter | Description |
|---|---|
| `query` | Search query (optional). |
| `start` | Start of the time range (inclusive, defaults to now). |
| `end` | End of the time range (exclusive, defaults to a year from now). |
| `calendars` | Calendars to search (optional, defaults to all). |

The output contains for each result:

- CalDAV path
- Start date
- End date
- Calendar
- Title
- Description
- Location
- Attendees
- Search score

### `fetch_calendar_events`

Retrieves specific events from calendar and uses Open WebUI's file upload system to generate download links.

Input parameters:

| Parameter | Description |
|---|---|
| `events` | List of calendar events. |

The output contains for each result:

- Open WebUI file ID
- Filename
- Size in bytes
- Content type
- Download URL

### `create_calendar_event`

Creates a new event into calendar and returns metadata.

Input parameters:

| Parameter | Description |
|---|---|
| `calendar` | Calendar name. |
| `start` | Event start date (inclusive). |
| `end` | Event end date (exclusive). |
| `title` | Event summary. |
| `description` | Event description (optional). |
| `location` | Event location (optional). |
| `attendees` | List of attendees (optional). |

The output contains:

- CalDAV path
- Start date
- End date
- Calendar
- Title
- Description
- Location
- Attendees

### `update_calendar_event`

Modifies an event from calendar and returns metadata.

Input parameters:

| Parameter | Description |
|---|---|
| `path` | CalDAV path. |
| `calendar` | Calendar name. |
| `start` | Event start date (inclusive, mandatory when end date is set). |
| `end` | Event end date (exclusiv, mandatory when start date is set). |
| `title` | Event summary (optional). |
| `description` | Event description (optional). |
| `location` | Event location (optional). |
| `attendees` | List of attendees (optional). |

The output contains:

- CalDAV path
- Start date
- End date
- Calendar
- Title
- Description
- Location
- Attendees

### `delete_calendar_event`

Deletes an event from calendar.

Input parameters:

| Parameter | Description |
|---|---|
| `path` | CalDAV path. |

The output is empty.

## Installation

1. Go to `Workspace` in Open WebUI.
2. Create a new tool from the `Tools` tab.
3. Paste the content of `openwebui_calendar_tools.py` and save the tool.
4. Enable the tool in your custom model in `Models`.
5. Configure the tool valves to change default settings.
6. Configure the username and password for each user.

## Configuration

### User Valves

| Setting | Description |
|---|---|
| `username` | Calendar username. |
| `password` | Calendar password. |

### Tool Valves

| Setting | Default | Description |
|---|---:|---|
| `protocol` | `caldav` | Connection method. |
| `verify_ssl` | `true` | SSL certificates verification. |
| `host` | `host.docker.internal` | Server hostname or IP address reachable from the Open WebUI container. |
| `port` | Protocol default | Optional custom server port. |
| `path` | Server path | Optional custom path for CalDAV. |
| `search_count` | `100` | Maximum number of search results to return. |
| `search_timeout` | `60` | Maximum search task duration in seconds. |

When `port` is not set, the protocol default port (`443`) is used.

## CalDAV endpoints

| Provider | Host | Port | Path |
|---|---|---|---|
| Apple iCloud | `caldav.icloud.com` | `443` | `/`
| Google Calendar | `www.google.com` | `443` | `/calendar/dav/`
| Synology | `example.com` | `5001` | `/caldav/`
| Nextcloud | `example.com` | `443` | `/remote.php/dav/`
| SOGo | `example.com` | `443` | `/SOGo/dav/`

## Security

**Apple and Google require to setup an app password** to access your account over the CalDAV protocol:
- Apple: https://account.apple.com/account/manage
- Google: https://myaccount.google.com/apppasswords

**Enable encryption** to securely store credentials:
- Set `WEBUI_SECRET_KEY` (generate a secure key with `openssl rand -hex 32`).
- Set `ENABLE_VALVE_ENCRYPTION` to `true`.

Restrict network access between Open WebUI and the calendar server.

## Compatibility

Tested with **Open WebUI 0.10.2**.

The tool imports internal Open WebUI modules, so compatibility with earlier or later releases is not guaranteed.

## Requirements

Allow Open WebUI to install listed requirements:
- Set `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` to `true`.
- Set `OFFLINE_MODE` to `false`.

The tool relies on a 3rd party Python package:
- [caldav](https://github.com/python-caldav/caldav)

## License

[GNU AGPLv3](LICENSE)
