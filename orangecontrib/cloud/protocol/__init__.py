"""API v1: the contract between the add-on and the cloud machine.

Both sides import its modules: the HTTP client in the add-on and the FastAPI
server on the machine. They import nothing from the rest of the add-on, and
no Qt.

* :mod:`.wire`: the API version and path prefix, headers and content types;
* :mod:`.errors`: error codes and the error body;
* :mod:`.machine`: the machine's info, shutdown, and deleting;
* :mod:`.tables`: uploads, tables, rows, ``derive`` and ``stats``;
* :mod:`.jobs`: jobs and the objects they store;
* :mod:`.memory`: memory estimates of the ``distances`` and ``mds`` jobs;
* :mod:`.endpoints`: every endpoint, :data:`~.endpoints.ENDPOINTS`;
* :mod:`.pickles`: the pickler for every pickle payload
  (:func:`~.pickles.dumps`, :func:`~.pickles.loads`);
* :mod:`.recipes`: the add-on's own recipes, which the machine fits on all
  rows (the one module that needs Orange).

The models depend only on Pydantic and the standard library, so the add-on
can use them without the server's dependencies. The full description, with
every endpoint, is in ``docs/api-v1.md``. In short:

* Every request carries ``Authorization: Bearer <token>``.
* JSON bodies are the Pydantic models. Errors are
  :class:`~.errors.ErrorResponse`.
* Pickled Orange objects never go inside JSON. Each pickle travels in its own
  body with ``Content-Type: application/octet-stream`` and the header
  ``X-Orange-Cloud-Payload: pickle``:

  - **requests** that send pickles are ``multipart/form-data``: a ``spec``
    part with the JSON model, plus one file part (with a ``filename``) per
    pickled object, named as the endpoint describes;
  - **responses** that return a pickle return it as the raw body, one object
    per response.

* Slow operations run as **jobs**: the endpoint answers ``202`` with a
  :class:`~.jobs.JobInfo`, and the client polls ``GET /v1/jobs/{id}`` and
  then reads ``GET /v1/jobs/{id}/result``.
"""
