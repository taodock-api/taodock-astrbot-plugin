# TaoDock Media for AstrBot

Use a TaoDock API key to generate images and videos from AstrBot commands. The
plugin connects only to the official TaoDock API at
`https://taodock.com/v1`.

## Install

In AstrBot, open **Extensions → Plugins → Install Plugin** and install:

`https://github.com/taodock-api/taodock-astrbot-plugin`

Create an API key at [TaoDock API Keys](https://taodock.com/keys), then add it
to the plugin configuration. Select default image and video models from the
IDs returned by `/td_models`.

## Commands

| Command | Purpose |
| --- | --- |
| `/td_models image` | List image models available to the configured key |
| `/td_models video` | List video models available to the configured key |
| `/td_image <prompt>` | Generate and send one or more images |
| `/td_video <prompt>` | Start a video task, wait for completion, and send the video |
| `/td_video_status <task_id>` | Check an existing video task |
| `/td_video_download <task_id>` | Download and send a completed video |

The optional image and video parameter fields accept JSON objects. Model and
prompt are controlled by the plugin and cannot be replaced through those
objects. Video waiting is capped at 30 minutes; if a task is still running,
use its ID with the status or download command.

## Data and security

- The API key is sent only to `https://taodock.com/v1` using a Bearer
  authorization header. It is never included in plugin logs or chat replies.
- Prompts, model IDs, request parameters, and generated media are sent to
  TaoDock and may be retained in service logs. See the
  [TaoDock privacy policy](https://taodock.com/privacy-policy).
- Generated files are written to the operating system's temporary directory
  so AstrBot can deliver them. AstrBot and the connected chat platform may
  retain their own copies.
- AstrBot's `secret` setting masks the key in the dashboard; it does not
  encrypt the plugin configuration file. Restrict access to the AstrBot data
  directory.

For model IDs and supported protocols, see the
[TaoDock model guide](https://docs.taodock.com/en/guide/models.html). For
request failures, review [Common Logs](https://taodock.com/usage-logs/common)
and open an issue in this repository with the request ID, if one is shown.
Never include your API key in an issue.

## License

MIT
