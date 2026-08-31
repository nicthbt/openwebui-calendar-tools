# Open WebUI Calendar tools

Search on calendar for information and fetch specific event content.

## Features

- Extracts keywords from search query.
- Searches for events in specified time range and calendars.
- Ranks results using similarity scoring and event date.
- Stores credentials in User Valves settings.
- Supports CalDAV protocol

## Available tools

### `search_calendar_events`

Searches for events on the calendar and returns metadata.

Parameters:

| Parameter | Description |
|---|---|
| `query` | Search query (optional). |
| `start` | Start of the time range (inclusive, defaults to now). |
| `end` | End of the time range (exclusive, defaults to none). |
| `calendars` | Calendars to search (optional, defaults to all). |

Each result contains:

- CalDAV path
- Start date
- End date
- Calendar
- Title
- Description
- Location
- Attendees
- Search score

## Installation

1. Go to Workspace in Open WebUI.
2. Create a new tool from the Tools tab.
3. Paste the content of `openwebui_calendar_tools.py` and save the tool.
4. Configure the username and password for each user.
5. Configure the tool valves to change default settings.
6. Enable the tool in your custom model.

## Configuration

### User Valves

| Setting | Description |
|---|---|
| `username` | Calendar username. |
| `password` | Calendar password. |

### Tool Valves

| Setting | Default | Description |
|---|---:|---|
| `protocol` | `caldav` | Connection method: `caldav`. |
| `verify_ssl` | `true` | SSL certificates verification. |
| `host` | `host.docker.internal` | Server hostname or IP address reachable from the Open WebUI container. |
| `port` | Protocol default | Optional custom server port. |
| `path` | Server path | Optional custom path for CalDAV. |
| `search_count` | `100` | Maximum number of search results to return. |
| `search_timeout` | `60` | Maximum search task duration in seconds. |

When `port` is not set, the protocol default port (`443`) is used.

## Security

- Enable encryption to store credentials (set a strong `WEBUI_SECRET_KEY` and set `ENABLE_VALVE_ENCRYPTION` to `true`).
- Restrict network access between Open WebUI and the calendar server.

## Compatibility

Tested with **Open WebUI 0.10.2**.

The tool imports internal Open WebUI modules, so compatibility with earlier or later releases is not guaranteed.

## Requirements

Allow Open WebUI to install listed requirements (set `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` to `true` and `OFFLINE_MODE` to `false`).

The tool relies on a 3rd party Python package:
- [caldav](https://github.com/python-caldav/caldav)

## License

[GNU AGPLv3](LICENSE)
