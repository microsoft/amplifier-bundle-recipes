# Todo API Scenarios

These are the end-to-end scenarios that define success. The software is correct
if and only if all scenarios pass. Internal code structure is irrelevant.

---

## Scenario 1: Empty list on fresh start

**Description:** A fresh server with no prior data returns an empty todo list.

**Preconditions:**
- Remove `todos.json` if it exists
- Start the server

**Steps:**
1. Send `GET /todos`

**Assertions:**
- Response status is `200`
- Response body is `[]`
- Content-Type header contains `application/json`

**Type:** deterministic

---

## Scenario 2: Create a todo

**Description:** Creating a todo returns it with an ID and done=false.

**Preconditions:**
- Server is running with no existing todos

**Steps:**
1. Send `POST /todos` with body `{"title": "Buy groceries"}`

**Assertions:**
- Response status is `201`
- Response body has an `id` field (non-empty string)
- Response body has `title` equal to `"Buy groceries"`
- Response body has `done` equal to `false`

**Type:** deterministic

---

## Scenario 3: Retrieve a created todo

**Description:** A created todo can be retrieved by its ID.

**Preconditions:**
- Server is running

**Steps:**
1. Send `POST /todos` with body `{"title": "Walk the dog"}`
2. Extract `id` from the response
3. Send `GET /todos/{id}`

**Assertions:**
- GET response status is `200`
- Response body `title` is `"Walk the dog"`
- Response body `done` is `false`
- Response body `id` matches the created todo's ID

**Type:** deterministic

---

## Scenario 4: List includes created todos

**Description:** The list endpoint returns all created todos.

**Preconditions:**
- Server is running with no existing todos

**Steps:**
1. Send `POST /todos` with body `{"title": "Item A"}`
2. Send `POST /todos` with body `{"title": "Item B"}`
3. Send `POST /todos` with body `{"title": "Item C"}`
4. Send `GET /todos`

**Assertions:**
- Response status is `200`
- Response body is an array with exactly 3 items
- The array contains todos with titles "Item A", "Item B", and "Item C"

**Type:** deterministic

---

## Scenario 5: Update a todo

**Description:** A todo's title and done status can be updated.

**Preconditions:**
- Server is running

**Steps:**
1. Send `POST /todos` with body `{"title": "Original title"}`
2. Extract `id` from the response
3. Send `PUT /todos/{id}` with body `{"title": "Updated title", "done": true}`
4. Send `GET /todos/{id}`

**Assertions:**
- PUT response status is `200`
- GET response shows `title` is `"Updated title"`
- GET response shows `done` is `true`

**Type:** deterministic

---

## Scenario 6: Delete a todo

**Description:** A deleted todo is no longer retrievable.

**Preconditions:**
- Server is running

**Steps:**
1. Send `POST /todos` with body `{"title": "To be deleted"}`
2. Extract `id` from the response
3. Send `DELETE /todos/{id}`
4. Send `GET /todos/{id}`

**Assertions:**
- DELETE response status is `204`
- GET response status is `404`

**Type:** deterministic

---

## Scenario 7: Toggle done status

**Description:** Toggling flips done from false to true and back.

**Preconditions:**
- Server is running

**Steps:**
1. Send `POST /todos` with body `{"title": "Toggle me"}`
2. Extract `id` from the response
3. Send `POST /todos/{id}/toggle`
4. Send `POST /todos/{id}/toggle`
5. Send `GET /todos/{id}`

**Assertions:**
- First toggle response has `done` equal to `true`
- Second toggle response has `done` equal to `false`
- Final GET shows `done` is `false`

**Type:** deterministic

---

## Scenario 8: 404 for non-existent todo

**Description:** Accessing a non-existent ID returns 404.

**Preconditions:**
- Server is running

**Steps:**
1. Send `GET /todos/nonexistent-id-999`
2. Send `PUT /todos/nonexistent-id-999` with body `{"title": "x"}`
3. Send `DELETE /todos/nonexistent-id-999`

**Assertions:**
- All three responses have status `404`
- All three response bodies contain an `error` field

**Type:** deterministic

---

## Scenario 9: Validation - missing title

**Description:** Creating a todo without a title is rejected.

**Preconditions:**
- Server is running

**Steps:**
1. Send `POST /todos` with body `{}`
2. Send `POST /todos` with body `{"title": ""}`

**Assertions:**
- Both responses have status `400`
- Both response bodies contain an `error` field

**Type:** deterministic

---

## Scenario 10: Persistence across requests

**Description:** Data survives across multiple request cycles (written to disk).

**Preconditions:**
- Server is running with no existing todos

**Steps:**
1. Send `POST /todos` with body `{"title": "Persistent item"}`
2. Extract `id` from the response
3. Send `GET /todos`

**Assertions:**
- GET response contains the created todo
- The todo's `id` matches what was returned on creation

**Type:** deterministic
