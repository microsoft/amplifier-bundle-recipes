# Todo API Specification

Build a REST API for managing a todo list.

## Overview

A simple HTTP JSON API server that lets users create, read, update, delete, and
manage todo items. The server uses file-based JSON storage (no external database
required).

## Technical Requirements

- **Language:** Python 3.10+
- **Framework:** None (use only the standard library `http.server`)
- **Storage:** JSON file on disk (`todos.json`)
- **Port:** 8080 (configurable via `PORT` environment variable)
- **No external dependencies** — standard library only

## API Endpoints

### `GET /todos`
Return all todos as a JSON array.
- Response: `200 OK` with `[{"id": "...", "title": "...", "done": false}, ...]`
- Empty list returns `[]`

### `POST /todos`
Create a new todo.
- Request body: `{"title": "Buy groceries"}`
- Response: `201 Created` with the created todo including a generated `id`
- The `done` field defaults to `false`
- If `title` is missing or empty, return `400 Bad Request` with `{"error": "title is required"}`

### `GET /todos/{id}`
Get a single todo by ID.
- Response: `200 OK` with the todo object
- If not found: `404 Not Found` with `{"error": "not found"}`

### `PUT /todos/{id}`
Update a todo.
- Request body: `{"title": "...", "done": true}` (both fields optional)
- Response: `200 OK` with the updated todo
- If not found: `404 Not Found` with `{"error": "not found"}`

### `DELETE /todos/{id}`
Delete a todo.
- Response: `204 No Content`
- If not found: `404 Not Found` with `{"error": "not found"}`

### `POST /todos/{id}/toggle`
Toggle the `done` status of a todo.
- Response: `200 OK` with the updated todo
- If not found: `404 Not Found` with `{"error": "not found"}`

## Data Model

```json
{
  "id": "unique-string",
  "title": "non-empty string",
  "done": false
}
```

## Behavior

- IDs should be unique strings (UUIDs or incrementing strings are fine)
- The server should persist data across requests (write to `todos.json`)
- The server should handle malformed JSON gracefully (return 400)
- The `Content-Type` for all responses should be `application/json`
- The server should start with: `python server.py`
