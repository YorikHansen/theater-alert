# Theater Alert

Queries the Theater Kiel website and notifies a webhook with a list of available seats in regular intervals. Webhooks can be configured to subscribe to (or ignore) certain plays and notify only for a given date range (i.e. 3 days in advance, as this matches the Kulturticket wonderfully).

## Set Up

### With docker

`docker compose up -d`

### With uv

`uv run theater-alert run`



