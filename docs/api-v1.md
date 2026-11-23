# API v1

The add-on talks to its cloud machine only through this HTTP API. The machine runs it with FastAPI over HTTPS on port 8443; Dask and everything else on the machine stays private.

The models named below are Pydantic models in [`orangecontrib/cloud/protocol/`](../orangecontrib/cloud/protocol/), which both the add-on and the server import: errors in `errors.py`, the machine's info in `machine.py`, uploads, tables and statistics in `tables.py`, jobs and stored objects in `jobs.py`, and the version, headers and content types in `wire.py`. The list of endpoints in `endpoints.py`, `ENDPOINTS`, is the reference; this page explains it.

The table endpoints that replay recorded steps and compute statistics (`derive`, `stats`), and the inputs and options of each job kind, are subject to change before 1.0.

## Conventions

- Every path starts with `/v1` (`API_PREFIX`). An incompatible change gets a new prefix. `GET /v1/info` reports `api_version`; the add-on refuses to work with a machine whose API version or `orange3-cloud` version differs from its own (error `version_mismatch`).
- JSON bodies use `Content-Type: application/json`. Times are ISO 8601 in UTC. Sizes are in bytes and durations in seconds.
- IDs (tables, jobs, objects, uploads) are opaque strings chosen by the machine.

## Auth

Every request carries the machine's token:

```
Authorization: Bearer <token>
```

Each machine gets its own random token when it starts. A missing or wrong token gets `401` with the error code `unauthorized`. Requests go over HTTPS with a self-signed certificate that the add-on generates for the machine and trusts exclusively.

## Errors

Every error response, whatever the status, has a JSON body (`ErrorResponse`):

```json
{"code": "not_found", "message": "No table with ID 't-42'.", "remote_traceback": null}
```

`remote_traceback` holds the formatted Python traceback from the machine when an exception caused the error, so the add-on can show it.

| `code` | HTTP status | Meaning |
|---|---|---|
| `bad_request` | 400 | Malformed request or invalid parameters. |
| `unauthorized` | 401 | Missing or wrong token. |
| `not_found` | 404 | Unknown endpoint, table, job, object or upload. |
| `conflict` | 409 | The request doesn't fit the current state, e.g. the result of a job that hasn't finished. |
| `version_mismatch` | 409 | API or package versions of the add-on and the machine don't match. |
| `cancelled` | 409 | The job was cancelled. |
| `payload_too_large` | 413 | The request would need more than the machine can hold, e.g. a distance matrix bigger than its memory. |
| `job_failed` | 500 | The operation raised an exception. |
| `worker_crashed` | 500 | The process that runs jobs crashed for a reason other than memory, e.g. a segfault in a compiled library. The message names the signal or exit code; `remote_traceback` has the last lines of the process's log. Tables and objects in memory were lost. |
| `internal` | 500 | Anything else that went wrong on the machine. |
| `out_of_memory` | 507 | The machine ran out of memory. The message is "The machine ran out of memory. Choose a larger machine size." |
| `shutting_down` | 503 | The machine is shutting down. |
| `unsupported_step` | 422 | A recorded step can't be replayed on all rows, for example because it learned its settings from the add-on's placeholder row. The message names the column and what to do instead (see `derive`). |

A failed or cancelled job has the same object in its `error` field (see **Jobs**).

## Payloads

Orange objects (learners, domains, filters, models, evaluation results, pages of table rows) travel as Python pickles. The add-on and the machine run the same versions of Python and every package (Python can differ in the last part of its version, see `GET /v1/info`), so pickles load the same on both sides.

Pickles never go inside JSON. Each one has its own body:

- `Content-Type: application/octet-stream`
- `X-Orange-Cloud-Payload: pickle`

The receiver unpickles a body only if it carries that header. Pickles use protocol 5.

Both sides pickle with `dumps` and unpickle with `loads` (in `protocol/pickles.py`), never with plain `pickle`. Its pickler handles two kinds of Orange objects that plain `pickle` can't, and one it would copy:

- the operators of row filters (`FilterContinuous.Type`, `FilterString.Type`), Enums that Orange defines inside a class, travel as the class attribute that holds them,
- Feature Constructor's `FeatureFunc`, `MappingTransformCast` and `DateTimeCast`, Create Class's `ValueFromStringSubstring` and `ValueFromDiscreteSubstring`, and Preprocess's `_NoneDisc` ("Remove numeric features"), travel by name. Their modules are Orange's widget modules, which import Qt, and the machine has no Qt, so the machine rebuilds them as its own stand-ins (`server/expressions.py`, `server/preprocessors.py`) that do the same. The stand-ins pickle as Orange's classes again, so the add-on gets Orange's own objects back.
- Other classes of Orange's widget modules (`Orange.widgets.*`) that the receiver can't import unpickle as `protocol.pickles.Unavailable` objects that keep their state and the class name. The machine refuses a step that uses one, naming the column; they can't compute or be pickled again.
- Orange's `DistMatrix` travels as a plain array plus its `row_items`, `col_items` and `axis`, because `DistMatrix.__reduce__` copies the values (gigabytes for a large matrix, also when the machine measures an object's `size_bytes`).

A pickle part of a job may also refer to the columns of the job's input tables instead of carrying copies: the persistent ID `("ref", <input name>, <position>)` stands for column `<position>` of `domain.variables + domain.metas` of the input table named `<input name>` (`refs` in `dumps`/`loads`). An unknown reference fails the job.

**Requests** that send pickles are `multipart/form-data`:

- a part named `spec`, with `Content-Type: application/json`, holding the endpoint's JSON model,
- one part per pickled object, with the two headers above, named as the endpoint says (for example `steps`, or the names listed in a job's `parts`). Pickle parts are file parts: their `Content-Disposition` has a `filename` (any, e.g. the part name), because only file parts keep their own headers.

A list of objects, such as several learners, is sent as one pickled list in one part.

**Responses** that return a pickle return it as the whole body, with the two headers above. One response carries one object.

**File uploads** (`POST /v1/uploads`) are raw `application/octet-stream` bodies without the payload header: they are data files, never unpickled.

Unpickling runs code, so the machine accepts pickles only from requests with its token, and the add-on only from its own machine over the pinned certificate.

## Jobs

Anything slow runs as a job: loading a table, replaying recorded steps, and everything started with `POST /v1/jobs`. An endpoint that starts a job answers `202 Accepted` with a `JobInfo`:

```json
{
  "id": "j-7", "kind": "evaluate", "state": "running",
  "progress": 0.3, "status": "Fold 3 of 10", "error": null,
  "created_at": "2026-11-19T10:00:00Z", "started_at": "2026-11-19T10:00:01Z", "finished_at": null
}
```

- `state` (`JobState`): `queued` → `running` → one of `done`, `failed`, `cancelled`.
- `progress`: from 0 to 1.
- `status`: a short text for the widget's status line.
- `error`: an `ErrorResponse` when the job failed (`job_failed`, `out_of_memory`, `worker_crashed`) or was cancelled (`cancelled`).

The add-on polls `GET /v1/jobs/{id}` until the state is final, then reads `GET /v1/jobs/{id}/result` (`JobResult`):

- `job_id` and `kind`: the job's ID and kind,
- `tables`: new tables on the machine, by output name, as `TableInfo`,
- `objects`: stored objects (models, distance matrices, evaluation results), by output name, as `ObjectInfo`, downloadable with `GET /v1/objects/{id}`,
- `values`: small JSON results such as scores,
- `runtime_seconds`.

Large results stay on the machine; the result only references them. Asking for the result of a job that hasn't finished gives `409 conflict`; of a failed or cancelled job, the job's error.

**Jobs nobody waits for.** With an idle timeout, the machine cancels its queued and running jobs when it hasn't had any request (`GET /v1/info` included) for that long, for example after Orange crashed or while the laptop slept. Each job's `error` is then `cancelled`, with the message "The machine cancelled the job: Orange hadn't been in touch for 30 minutes." The add-on reads `/v1/info` every 30 seconds while it is connected and polls the jobs it waits for, so this doesn't happen while Orange runs. Without an idle timeout, jobs aren't cancelled this way.

**Out of memory and crashes.** The machine runs jobs in one worker process with a memory limit, and keeps tables and stored objects in that process's memory. A job that allocates more than the machine can give fails with `out_of_memory`. If the worker as a whole goes over its limit, the machine restarts it: every job that was running fails with `out_of_memory`, and the tables and objects in memory are lost (their IDs then give `404`). The same happens when the system's out-of-memory killer stops the worker. If the worker dies for any other reason, such as a crash in a compiled library, running jobs fail with `worker_crashed` instead, with the same loss of tables and objects. The API server itself keeps running and restarts the worker.

## Endpoints

| Endpoint | Request | Response | Job |
|---|---|---|---|
| `GET /v1/info` | | `InfoResponse` | |
| `POST /v1/shutdown` | | `ShutdownResponse` | |
| `POST /v1/uploads` | raw file, query `UploadParams` | `UploadInfo` | |
| `POST /v1/tables` | `TableCreateRequest` | `JobInfo` | yes |
| `GET /v1/tables` | | `TableList` | |
| `GET /v1/tables/{id}` | | `TableInfo` | |
| `GET /v1/tables/{id}/domain` | | pickled `Domain` | |
| `GET /v1/tables/{id}/rows` | query `RowsParams` | pickled `Table` | |
| `POST /v1/tables/{id}/derive` | multipart: `DeriveRequest` + `steps` | `JobInfo` | yes |
| `POST /v1/tables/{id}/stats` | `StatsRequest` | pickle | |
| `DELETE /v1/tables/{id}` | | `DeleteResponse` | |
| `POST /v1/jobs` | multipart: `JobRequest` + pickles | `JobInfo` | yes |
| `GET /v1/jobs/{id}` | | `JobInfo` | |
| `GET /v1/jobs/{id}/result` | | `JobResult` | |
| `DELETE /v1/jobs/{id}` | | `JobInfo` | |
| `GET /v1/objects/{id}` | | pickle | |
| `DELETE /v1/objects/{id}` | | `DeleteResponse` | |

### Machine

#### `GET /v1/info`

Returns `InfoResponse`:

- `api_version`, `server_version` (the machine's `orange3-cloud`),
- `python_version` (installed on the machine) and `python_version_requested` (the user's, from the EnvironmentSpec; `null` if unknown). They differ when uv has no build of the user's exact version: the machine then installs the newest release of the same minor version, so a request for 3.11.2 gets the newest 3.11. Releases within a minor version only fix bugs, so pickles still load the same,
- `packages`: installed packages and their versions,
- `install_failures`: optional packages from the user's environment that failed to install at boot (`name`, `version`, `error`),
- `machine`, `cpu_count`,
- memory: `memory_total_bytes`, `memory_free_bytes` (available for new work), `memory_tables_bytes` (held by stored tables); disk: `disk_total_bytes`, `disk_free_bytes`,
- `uptime_seconds`, `idle_seconds` (since the last request or running job; `GET /v1/info` itself doesn't count, so polling it doesn't keep the machine alive, though it keeps running jobs from being cancelled),
- limits: `idle_timeout_seconds` (also how long the machine waits for any request before it cancels its jobs) and `max_lifetime_seconds` (`null`: off); the time left until the idle shutdown, `idle_time_left_seconds` (`null` while jobs are queued or running), and until the end of the lifetime, `lifetime_left_seconds`; and `time_left_seconds`, the smaller of the two, until the machine shuts down by itself,
- `jobs_running`, `jobs_queued`.

#### `POST /v1/shutdown`

Shuts the machine down after answering. Returns `ShutdownResponse`. Locally the container exits; on a cloud the VM powers off (Google Cloud, AWS) or deallocates itself (Azure).

The machine also shuts down by itself, the same way, after the idle timeout (no requests and no queued or running jobs; default 30 minutes) or at the end of its maximum lifetime (default 12 hours, even with jobs running). Running jobs are cancelled. Jobs keep the machine running only while the add-on is in touch: after an idle timeout without any request, the machine cancels them (see "Jobs nobody waits for"), and the idle shutdown follows one idle timeout later.

### Uploads

#### `POST /v1/uploads`

Streams a file to the machine's disk. Used in local mode; on a cloud, Cloud File uploads local files to the cloud's storage instead, and they are loaded from there (from `gs://` on Google Cloud, through a signed `https://` link on AWS and Azure).

- Query: `UploadParams`: `filename`, whose extension tells the format (`.csv`, `.csv.gz`, `.parquet`, ...).
- Body: the raw file, `application/octet-stream`, no payload header.
- Returns `UploadInfo`: `id`, `filename`, `size_bytes`, `created_at`.
- The file stays on the machine's disk until the machine stops, so it can be loaded again (for example with other column roles). A body bigger than the free disk space is refused with `payload_too_large`.

### Tables

A table is an Orange `Table` held in the machine's memory. The add-on refers to it by ID.

#### `POST /v1/tables`

Loads a CSV or Parquet file (optionally `.gz`), or several files with the same columns, into a table. Runs as a job of kind `load_table`; its result has the new table under `tables["table"]`.

`TableCreateRequest`:

- exactly one of `uri`, `uris` and `upload_id` (from `POST /v1/uploads`). `uri` is `gs://bucket/path` (read with the machine's Google credentials), `https://...` (or `http://...`), or an absolute path on the machine's own disk (`/path` or `file:///path`). `uris` is a list of 1 to 1,000 such locations, all CSV or all Parquet, loaded one after the other into one table (see "Several files" below),
- `format`: `csv` or `parquet`, or `null` to use the file extension (`.csv`, `.tsv`, `.tab`, `.txt`, `.parquet`, `.pq`, each optionally followed by `.gz`),
- `name`: defaults to the file name without its extensions (for `uris`, the first file's, followed by "and N more"),
- `columns`: optional `ColumnSpec` overrides of the inferred domain: `name`, `role` (`feature`, `target`, `meta`, `skip`) and `type` (`numeric`, `categorical`, `text`, `datetime`),
- `copy_first` (default `false`): copy an `http(s)://` file to the machine's disk with one request, then read it from there. Cloud File sets it for a signed link that the provider made for a file in its storage (e.g. `s3://`): such a link may stop working before a long load ends, while a download that has started keeps going. `http(s)://` Parquet files are always copied first, since they are read with many requests (`.parquet.gz` files are decompressed to the disk anyway). A file bigger than the free disk space fails the job with `payload_too_large`.

Unknown uploads and missing local files answer `404` at once; other problems with the source (an unreadable URL, a broken file) fail the job. The table's `source` is the `uri` without its query for `http(s)://` URLs, so the machine keeps no link's signature; for `uris`, the first one's, followed by how many more files there were, e.g. `https://example.com/2025-01.parquet (+11 more files)`.

**Several files.** The files of `uris` become one table, their rows in the order of the list, e.g. the twelve monthly files of a year. The first file decides the table's columns, with their roles and types (from `columns`, or inferred from the first file; categories collect the values of all files). The other files' columns are matched to them by name, or, when no name is the same, by a unique name that differs only in letter case (e.g. `Airport_fee` and `airport_fee`). Files whose columns changed a little over time load as follows:

- a column's type may differ between the files as long as it is read the same way: numbers of any width (`int32`, `int64`, `double`), text (`string`, `large_string`, dictionary-encoded), dates and times in any unit,
- a column that a later file doesn't have is missing in that file's rows, and a column that only later files have is skipped; each gives one warning that names the files,
- in CSV files, a column that holds numbers in the first file and text in a later one is read as text, with the same warning as text further down one file,
- in Parquet files, such a column fails the job with `bad_request`, unless `columns` sets its type, which says how to read it in every file,
- a later file with none of the first file's columns fails the job with `bad_request`, and so do a missing or unreadable file (the message names it) and a mix of CSV and Parquet files (answered at once).

With `copy_first`, every `http(s)://` file is copied at the start of the load, before any is read (so each link is used soon after it was made), and the copies stay until the load ends or fails; the disk needs room for all of them together (checked before copying, `payload_too_large` otherwise). Other `http(s)://` Parquet files are copied, read and deleted one at a time, so the disk needs room for the largest one. The progress text says which file is being copied or read ("Copying file 3 of 12: …", "File 3 of 12: Read 9,123,456 rows").

CSV files are UTF-8 with a header row; the delimiter (comma, tab, semicolon or `|`) is detected from the header (in `.tab` and `.tsv` files a tab, if the header has one). Empty cells, `?`, `NA`, `NaN`, `null`, `N/A` and similar are missing values. Spaces around text values are dropped, as Orange's reader does.

**Orange's header rows.** A CSV or `.tab` file may start with Orange's three header rows: names; types (`continuous`/`c`, `discrete`/`d` or a list of values such as `low mid high`, `string`/`s`, `time`/`t`, or empty); and flags (`class`/`c`, `meta`/`m`, `ignore`/`i`, `weight`/`w`, `key=value` attributes, or empty). The file has them when its second row holds only types and its third only flags, the same test as Orange's. Then the file is read as Orange's File widget reads it: the types and roles come from these rows, an empty type is inferred as below, text columns are metas (also when flagged as the class, and a text class column without a type is categorical only with few values), ignored columns are skipped, weight columns too (with a warning: tables have no weights), listed values keep their order (other values are missing), `key=value` flags become the variable's `attributes`, and `.` and `~` are missing values too. `columns` overrides still win over these rows.

The domain is inferred like Orange's File widget does:

| Column | Orange variable |
|---|---|
| numbers whose values are all in {0, 1} or all in {1, 2} | `DiscreteVariable` |
| other numbers | `ContinuousVariable` |
| text (and true/false) with at most 100 distinct values, and at most `round(n_rows ** 0.7)` | `DiscreteVariable` (values in natural order) |
| other text | `StringVariable`, always a meta column |
| dates, timestamps, times | `TimeVariable` (seconds since 1970-01-01 UTC) |
| no values at all | `ContinuousVariable` |

Every column is a feature unless Orange's header rows or `columns` say otherwise. A text column that is the target is always categorical. Values that don't fit a requested type (e.g. text in a `numeric` column) become missing. Columns of unsupported Parquet types (lists, structs, binary) are skipped with a warning.

The job's result `values` has `load_seconds` and `warnings` (a list of notes, e.g. skipped columns, or a column read as text because some of its values weren't numbers).

Loading reads the file in batches and frees each batch's file data once it is converted, so it needs about the table's final size plus a little working memory, not two or three times the table.

#### `GET /v1/tables`

Returns `TableList`: `tables`, a list of `TableInfo`.

#### `GET /v1/tables/{id}`

Returns `TableInfo`: `id`, `name`, `n_rows`, `n_attributes`, `n_class_vars`, `n_metas`, `memory_bytes` (the table's arrays, including an estimate of the text in meta columns), `peak_load_memory_bytes` (the highest extra memory of the machine's worker measured while loading), `source` (a URI, `upload:<id>`, or `table:<id>` for derived tables), `format`, `parent_id` (the table it was derived from), `steps` and `steps_key` (for tables made by `derive`: one short line per replayed step, e.g. `transform: 4 columns`, and the request's key), `created_at`.

#### `GET /v1/tables/{id}/domain`

Returns the table's Orange `Domain` as a pickle. It is separate from `GET /v1/tables/{id}` because pickles never go inside JSON.

#### `GET /v1/tables/{id}/rows`

Returns a page of the table's rows as a pickled Orange `Table` with the table's domain. Viewers such as Cloud Data Table use it to show real rows; nothing is computed from them.

- Query: `RowsParams`:
  - `start` (default 0) and `stop` (exclusive; default `start` + 1000): row positions counted from 0. At most 100,000 rows per request; past the end of the table, fewer rows (or none) come back,
  - `sort`: the name of a column (feature, target or meta) to sort all rows by on the machine before the page is taken; by default the table's own order. Categorical columns sort by the order of their values, text alphabetically, missing values always last,
  - `descending` (default `false`).
- An unknown column, `stop` smaller than `start` or too many rows give `400 bad_request`. The machine keeps the last few sort orders, so paging through a sorted table sorts it only once.

#### `POST /v1/tables/{id}/derive`

Replays the steps that Orange's own data-changing widgets (Select Columns, Select Rows, Feature Constructor, Continuize, ...) recorded on the add-on's placeholder table, on every row of the table, and keeps the result as a new table. Runs as a job of kind `derive`; the result has the new table under `tables["table"]` and the value `rows`.

- Multipart: `spec` part `DeriveRequest` (`name`, `steps_key`, `prefix_keys`), and a pickle part `steps`: a tuple of steps in order, each `("transform", Domain)`, `("filter", Filter)` or `("preprocess", Preprocess)`.
- `("preprocess", recipe)` comes from Cloud Preprocess (a recipe from Orange's Preprocess widget: an `Orange.preprocess` `Preprocess`, often a `PreprocessorList`), from Cloud Purge Domain (Orange's `Remove`), from Cloud Discretize (`protocol.recipes.DiscretizeColumns`: Orange's Discretize widget's settings, a method per numeric or time column, with Orange's own discretizers), from Cloud Categories (`protocol.recipes.ColumnCategories`: one column made categorical with the values of all rows and its values merged by their counts on all rows, as Edit Domain does) or from Cloud Create Class (`protocol.recipes.CreateClass`: Create Class's rules, kept as Create Class keeps them on all rows, and its `ValueFromStringSubstring` or `ValueFromDiscreteSubstring`; on the machine, which has no Qt, stand-ins that pickle back as Orange's classes). The machine fits it on all rows of the table at that point with Orange's own code (`recipe(table)`), so its new columns hold numbers learned from all rows. Before fitting, it checks that a table as large as the arrays of the table at that point (without the text in meta columns) fits in the memory the worker may still use, and fails with `413 payload_too_large` if not. `DiscretizeColumns` reports progress per column and can be cancelled between columns.
- The `steps` pickle may refer to the columns of the table (`domain.variables + domain.metas`) by position instead of carrying copies (see "Pickles"; the input's name is `data`). The add-on does this for the steps after a `preprocess` step: it sends them in a second request that starts from the fitted table, so they use the machine's own fitted columns.
- `steps_key` is a hash of the pickled steps (the add-on sends the SHA-256 of the pickle). The machine keeps the result for each table and key: when it already has one, it answers with a job that is already `done` and has that table as its result.
- `prefix_keys` (optional): the same hashes of the first 1, 2, … n−1 steps, each pickled as a tuple of its own. Widgets in different branches of a workflow share their first steps (e.g. Feature Constructor and Select Rows, then another Select Rows in one branch and Select Columns in another), and replaying a long first step on many rows twice wastes time and memory. So `derive` jobs run one at a time on the machine (a job waits with the status "Waiting for another table to be derived"), and each starts from the table it derived from the same table with the longest of its first steps, if it still has it. While replaying, a job keeps the table after a step as an extra output (`steps-<k>` in `tables`, with that step's key as `steps_key`) when a waiting job starts with the same steps and then goes on with other ones.
- Transforms run with Orange's `Table.transform`, filters with Orange's own filters (`Orange.data.filter`). The tables between the steps are dropped as soon as the next step is done, unless a waiting job needs them (above).
- The result's `values`: `rows` (of the new table) and `replayed_steps` (the steps this job replayed; fewer than all when it started from a table derived before, 0 when another job had just derived the same steps).
- **Checks.** Steps that learned something from the data learned it from the placeholder row, whose values are all missing. The machine refuses them with `422 unsupported_step` and a message that names the column:
  - Every new column (one that isn't a column of the table at that step), and every new column it is computed from, must come from `Identity`, `Indicator`, `Indicator1` (one-hot coding from a column's own categories), Feature Constructor's `FeatureFunc` or `Lookup` (a mapping of a categorical column's own values: Edit Domain's renaming and merging of categories, and categories turned into numbers or text). Otherwise:
    - `Discretizer` (Orange's Discretize or Preprocess on the placeholder row): "'x1' was discretized by a widget that sees only the laptop's empty placeholder row. On cloud data, use Cloud Discretize, or Discretize in Cloud Preprocess."
    - Orange's own transformations that learn from data, such as `Normalizer` or `ReplaceUnknowns`: "'x1' was made by a step that learns its settings from the data. On cloud data, use Cloud Preprocess for this step."
    - `MappingTransform` (Edit Domain's "as categorical", with the values it saw on the placeholder row): "'x1' took its categories from the laptop's empty placeholder row, not from the data. On cloud data, use Cloud Categories to make a column categorical."
    - Create Class's `ValueFromStringSubstring` and `ValueFromDiscreteSubstring`: Create Class keeps rules only until the rows it sees are all matched, so on the placeholder only its first rule; the message says so and points to Cloud Create Class.
    - Anything else, for example classes of Orange's widget modules that the machine can't import: "'id' was made by a step that isn't supported on cloud data (ToContinuousTransform)."
  - A `Lookup` to a categorical column must keep some of its source's values: "'color' keeps none of the values of 'color': the step chose them from the laptop's empty placeholder row, which has no values. On cloud data, use Cloud Purge Domain to remove unused values." (Purge Domain's "remove unused values" on the placeholder gives such a mapping. A `preprocess` step's columns aren't checked: Cloud Purge Domain's `Remove`, fitted on all rows, may keep none of a column's values when no row uses them.)
  - A categorical column made by Feature Constructor must give, on all rows, only values in its list of values. Without a list, Feature Constructor took the values from the placeholder row. Otherwise: "'big' gives values that aren't in its list (no). List all values in Feature Constructor."
- A malformed `steps` part gives `400 bad_request`.

Subject to change before 1.0.

#### `POST /v1/tables/{id}/stats`

Computes statistics on all rows of the table, for the Cloud viewers, and returns them as a pickled `dict`. Each kind computes what one of Orange's widgets computes, in the same way. Quick: it doesn't run as a job. The machine keeps the last answers per table and request, so asking again costs nothing.

- Body: `StatsRequest`:
  - `kind`: `basic`, `distribution`, `quantiles`, `histogram2d` or `values` (below),
  - `columns`: indices into the table's Orange `Domain`: attributes and class variables from 0, metas from -1 down (`domain.index(var)`); `null` for all columns (`basic`, `distribution`),
  - `group_by`: the index of a categorical column to split by (`distribution`, `quantiles`, `histogram2d`),
  - `binning`: for the `distribution` of a numeric column, which of the binnings Orange's Distributions widget offers (`decimal_binnings` or `time_binnings` of all rows; 0 is the finest; too large means the coarsest); `null` gives 10 bins of equal width,
  - `fit`: for `distribution`, a curve to fit to each group's values: `norm`, `beta`, `gamma`, `rayleigh`, `pareto`, `expon` (SciPy's `fit`) or `kde` (Distributions' "Kernel density", with `smoothing` from 1 to 20),
  - `grid`: for `histogram2d`, bins on each axis (2 to 1000, default 100).
- Every answer has `n_rows` and, except `histogram2d`, `columns`: a `dict` from column index to that column's numbers:
  - `basic` (Feature Statistics): `missing`, `min`, `max`, `mean`, `median`, `mode`, `dispersion` (coefficient of variation of a numeric column, entropy of a categorical one). NaN where Feature Statistics shows nothing; text columns have only `missing`.
  - `distribution` (Distributions): `counts` per value of a categorical column, or per bin of a numeric one; with `group_by`, a 2D array, one column per group value (rows whose group is missing aren't counted; for categorical columns `group_missing` counts them). `n_valid` is the number of rows counted and `missing` the rows where the column is missing. A numeric column also has `thresholds` (the bin edges) and, with `binning`, `binnings` (Orange's `BinDefinition`s) and `binning` (the index used). With `fit`: `curve_x` (100 points) and `curves`, one per group: `{"params": ..., "y": ...}`, or `None` for an empty group. Without `columns`, text columns are left out.
  - `quantiles` (Box Plot, numeric columns): `groups`, one per group value (one without `group_by`): `n` and, when `n > 0`, `min`, `max`, `mean`, `var` (population variance) and `q25`, `median`, `q75` (NumPy's `midpoint` quantiles).
  - `histogram2d` (two numeric columns, x then y): `n_valid` (rows with both defined), `x_edges`, `y_edges`, `counts` (x first; `None` without rows) and, with `group_by`, `group_counts` (one grid per group value).
  - `values` (Cloud Categories, Cloud Create Class; any column): `values` (a categorical column's values; the distinct values of a numeric or text column, sorted, as Edit Domain's "as categorical" orders them), `counts` (rows with each value), `first_seen` (indices into `values` in the order the values first appear in the rows; values no row has are left out), `missing` (rows where the column is missing) and `n_values`. A column with more than 100,000 values has `n_values` and `missing`, and `None` for the others.
- A column that doesn't exist, isn't numeric where it must be, or a `group_by` that isn't categorical gives `400 bad_request` with a message.

Subject to change before 1.0.

#### `DELETE /v1/tables/{id}`

Frees the table. Returns `DeleteResponse` (`id`, `deleted`).

### Jobs

#### `POST /v1/jobs`

Starts a computation on the machine. Returns `JobInfo` with `202`.

- Multipart: `spec` part `JobRequest`, plus one pickle part for each name in its `parts`.
- `JobRequest`:
  - `kind`: one of the kinds below,
  - `inputs`: named references to things on the machine: table IDs, object IDs, or lists of them,
  - `params`: the operation's options, as JSON,
  - `parts`: names of the pickle parts sent with the request.

Example `spec` for cross-validation of two learners, with a pickle part `learners` holding a list of two learners:

```json
{"kind": "evaluate", "inputs": {"data": "t-1"}, "params": {"method": "cross_validation", "folds": 10, "stratified": true}, "parts": ["learners"]}
```

Job kinds (`JobKind`). Inputs, options and outputs are indicative and subject to change before 1.0:

| Kind | Inputs and parts | Result |
|---|---|---|
| `fit` | input `data`, part `learner` (an Orange learner, with its preprocessors) | object `model` (kind `model`; its `size_bytes` is the download size), values `rows`, `learner` (its name), `model_type` and `training_seconds` |
| `evaluate` | input `data` (and `test_data` with `method` `test_on_test_data`), part `learners` (a list of Orange learners); params `method` (`cross_validation`, the default, or `test_on_test_data`), `folds` (default 10), `stratified` (default true), `max_parallel` (optional: most parts at a time; default as many as the machine's threads and memory allow). The folds are those of Orange's Test & Score (`CrossValidation(k, stratified, random_state=42)`); every learner × fold runs as its own task (in a forked process for learners that aren't scikit-learn's) | object `results` (kind `results`: Orange's `Results` for all rows, without the learners that failed; none if all failed; its `size_bytes` is the download size), values `rows`, `method`, `folds`, `learners` (`name`, `failed` (error text or `null`), `train_time`, `test_time` per learner), `scorers` (`id`, `name`, `long_name`, Orange's scorers for the target), `scores` (per target: `""` is the average over classes, then each class value one against the rest; rows are learners, columns scorers, `null` where a score can't be computed), `warnings`, `parallel` (most parts run at a time), `threads_per_part`, `part_memory_bytes` (memory counted per part, per learner), `processes` (per learner: its parts ran in forked processes) and `parts_run_again` (parts stopped for memory and run again) |
| `predict` | inputs `data` and `models` (a list of model object IDs, e.g. from `fit`); params `names` (optional: a column name per model; default the model's name). Every model predicts every row, on the data without its target, as in Orange's Predictions | table `table` (none if every model failed): the data plus, as meta attributes, a prediction column per model and, for a categorical target, a probability column per class value ("name (value)"). Values `rows`, `models` (`name`, `failed` (error text or `null`), `target` (the model's target), `scored`, `seconds` per model), `scorers`, `scores` (as for `evaluate`, but a target class is the scorers' target, as in Orange's Predictions, not one against the rest; `null` rows for models without scores; empty without a target), `scored_rows` (rows with a known target), `missing_targets` and `predict_seconds`. A deleted model gives `not_found` |
| `kmeans` | input `data`; params `ks` (a list of numbers of clusters, each at least 2), `init` (`k-means++`, the default, or `random`), `n_init` (default 10), `max_iter` (default 300), `normalize` (default true), `silhouette_rows` (default 100000). Clusters as Orange's k-Means widget does (its preprocessing and seed) | for each k that didn't fail, table `k<k>` (e.g. `k3`): the data plus meta columns "Cluster" and "Silhouette" (scaled `arctan(s) / pi + 0.5` as in Orange; named "Simplified silhouette" above `silhouette_rows`); object `outputs` (kind `other`: `{k: {"domain": the table's domain, "centroids": the centroids table}}`). Values `rows`, `silhouette` (`exact`: scikit-learn's `silhouette_samples`, up to `silhouette_rows` rows; `simplified`: centroid-based, above), `silhouette_rows`, `normalized`, `clusterings` (`k`, `score` (mean silhouette), `failed` (error text or `null`), `iterations`, `seconds` per k), `fit_seconds` and `silhouette_seconds` |
| `tsne` | input `data`; params `perplexity` (1 to 500, default 30), `multiscale` (default false: "Preserve global structure"), `exaggeration` (1 to 4, default 1), `initialization` (`pca`, the default, or `spectral`), `metric` (`l2`, the default, `l1` or `cosine`), `normalize` (default true), `pca` (default true) and `pca_components` (2 to 50, default 20, at most the number of columns). Embeds as Orange's t-SNE widget does, with `Orange.projection.TSNE` and its seed, using all the worker's threads | table `table`: the data plus meta columns "t-SNE-x" and "t-SNE-y". Values `rows`, `n_jobs` (threads), `iterations` (750), `kl_divergence` and `seconds` (`prepare`, `affinities`, `optimize`). Fails with `bad_request` and Orange's message for fewer than 2 rows or columns, or constant data |
| `distances` | input `data`; params `metric` (`euclidean`, the default, `manhattan`, `cosine`, `mahalanobis`, `hamming`, `pearson`, `pearson_absolute`, `spearman`, `spearman_absolute` or `jaccard`: `Orange.distance`'s metrics), `normalize` (default false; Euclidean and Manhattan only) and `axis` (`rows`, the default, or `columns`). Checks and fixes the data as Orange's Distances widget does (categorical columns ignored where the metric can't use them, missing values imputed), without its limit of 20,000 rows. First compares the memory it needs (`distances_memory_bytes`: n × n × 8 bytes times the metric's working matrices, from 1.2 for Manhattan to 5.1 for Spearman, plus 4 copies of the data) with what the worker may still use, and fails with `payload_too_large` (413) if it doesn't fit | object `distances` (kind `distances`: Orange's `DistMatrix` as the widget outputs it, `shape` [n, n]). Values `items`, `axis`, `metric` (Orange's name, e.g. "Euclidean (normalized)"), `warnings` (the widget's, e.g. "Missing values were imputed"), `labels` (the column names, between columns; else `null`), `memory_needed_bytes`, `memory_budget_bytes` and `seconds`. Fails with `bad_request` and Orange's message where the widget refuses the data (e.g. "No binary features") |
| `hclust` | inputs `distances` (a `distances` object), `data` (between rows: the table the distances were computed from) and, optionally, `linkage` (the `linkage` object of an earlier `hclust` job on the same distances and linkage: only cuts again); params `linkage` (`single`, `average`, `weighted`, `complete` or `ward`, the default), `cut` (`clusters`, the default, or `height`), `clusters` (1 to 1000, default 3), `height_ratio` (0 to 100, percent of the root's height, default 75) and `top` (leaves of the dendrogram's tree, 1 to 200, default 50). Clusters as Orange's Hierarchical Clustering widget does: SciPy's `linkage` on the matrix's upper triangle, Orange's tree, its top N clusters or the clusters below the height, numbered in the dendrogram's order. First compares the memory the linkage needs (the upper triangle, n (n - 1) / 2 × 8 bytes, 1.2 times for single linkage and 2.2 times for the others) with what the worker may still use, and fails with `payload_too_large` (413) if it doesn't fit | object `linkage` (kind `other`: SciPy's linkage matrix; not when given), between rows table `table`: the data plus a meta column "Cluster" (C1, C2, ...); object `outputs` (kind `other`: `domain` (the table's; `null` between columns), `labels` (each column's cluster index, between columns), `sizes` (items per cluster), `cut` (each cluster's leaf range) and `tree` (the top of Orange's tree, its top clusters as leaves)). Values `items`, `axis`, `linkage`, `clusters` (0 when the height is below every merge: then no table), `height` (the root's), `cut_height`, `memory_needed_bytes`, `linkage_reused`, `linkage_seconds` and `seconds`. Fails with `bad_request` and Orange's message where the widget refuses the matrix (e.g. "Distance matrix is not symmetric.") |
| `mds` | input `distances` (a `distances` object) and/or `data` (the output's table; without `distances`, the table to compute Euclidean distances from, after `Orange.projection.MDS`'s preprocessors); params `initialization` (`pca`, the default: Torgerson's classical MDS, or `random`, seeded), `max_iter` (1 to 10000, default 300) and `step` (iterations per step, default 25). Optimizes as Orange's MDS widget does: `Orange.projection.MDS` (SMACOF) in steps, each from the last positions, until `max_iter` or until the stress stops improving. First compares the memory it needs (`mds_memory_bytes`: 6 matrices of n × n × 8 bytes besides the distances; 7 and 4 copies of the data from a table) with what the worker may still use, and fails with `payload_too_large` (413) if it doesn't fit | with `data`, table `table`: the data plus meta columns "mds-x" and "mds-y"; otherwise object `embedding` (kind `other`: the n × 2 positions). Values `items`, `axis`, `iterations` (SMACOF's), `steps`, `converged` (stopped before `max_iter`), `stress` (Kruskal's, as the widget shows it), `memory_needed_bytes`, `memory_budget_bytes` and `seconds` (`distances`, `initialization`, `optimize`). Fails with `bad_request` and Orange's message where the widget refuses the input (e.g. "Data and distances dimensions do not match.") |
| `rank` | input `data`; params `methods` (a list of `Orange.preprocess.score` classes by name: `InfoGain`, `GainRatio`, `Gini`, `ANOVA`, `Chi2`, `ReliefF`, `FCBF`, `UnivariateLinearRegression`, `RReliefF`); part `scorers` (optional: a list of learners that score features, e.g. random forest or logistic regression). Scores the features on all rows as Orange's Rank widget does, with its fallbacks (a method that refuses the table is called for each feature; one that still fails gives no scores). First compares 12 copies of the features and target (n × columns × 8 bytes; measured up to 10 for the methods, 14 for a random forest) with what the worker may still use, and fails with `payload_too_large` (413) if they don't fit | values `methods` (per method, a score per feature), `scorers` (per scorer, a list of rows of scores per feature: one row, or one per class value; `null` if it failed), `failed` (names of the methods and scorers without scores), `has_missing` (the features or target have missing values), `rows` and `seconds`. A missing score is `null`, an infinite one `"inf"` or `"-inf"` |
| `sleep` | none; params `seconds` (default 1), `steps` (default 10), `fail` (default false) | values `seconds` and `steps`; a test operation that reports progress once per step, can be cancelled, and with `fail` raises an error at the end |

The table endpoints start jobs of kinds `load_table` and `derive`; they can't be started through `POST /v1/jobs`.

#### `GET /v1/jobs/{id}`

Returns `JobInfo`: state, progress, status text and error.

#### `GET /v1/jobs/{id}/result`

Returns `JobResult` for a job in state `done`. See **Jobs** above.

#### `DELETE /v1/jobs/{id}`

Cancels a queued or running job. Returns its `JobInfo`, already in state `cancelled`; a running operation stops at its next cancellation check. Cancelling a finished job changes nothing.

### Objects

Objects are results kept on the machine: models, distance matrices, evaluation results and other Python objects. They are listed in job results as `ObjectInfo`: `id`, `kind` (`model`, `distances`, `results`, `table`, `stats`, `other`), `type` (the qualified class name), `size_bytes` (size of the pickle, so the add-on can warn before large downloads), `shape` (for distance matrices), `description`, `created_at`.

#### `GET /v1/objects/{id}`

Returns the object as a pickle, for example to download a trained model.

The machine pickles the object once into a file and serves that file, with `Content-Length`, an `ETag` and `Accept-Ranges: bytes`. A download that broke off continues with `Range: bytes=<received>-` and `If-Range: <ETag>`: the answer is `206` with the rest of the same file, or `200` with the whole file if it changed. The machine keeps the files of the 4 most recently downloaded objects; deleting an object deletes its file.

#### `DELETE /v1/objects/{id}`

Frees the object. Returns `DeleteResponse`.
