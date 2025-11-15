# Services Layer

This package contains singleton-style service classes that encapsulate business
logic and coordinate database access.

## Main Components

- `auto_judge.py` — registry for automatic submission scorers.
- `auto_judge_first_track.py` — sample scorer executed via Docker.
- `submission.py` — CRUD operations for submissions with caching.
- `submission_notifications.py` — pushes Telegram notifications on updates.
- `audit_log.py` — writes high-granularity audit entries for every action.
- `team.py`, `user.py`, `competition.py`, `page.py`, `language.py` — domain specific
  helpers used across routers and middlewares.

All services are lightweight singletons; prefer retrieving them via their module-level
constructors rather than instantiating new objects manually. This keeps caches, bot
bindings, and background registries consistent.
